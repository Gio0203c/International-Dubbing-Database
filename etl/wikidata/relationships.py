"""Transform and idempotent PostgreSQL loader for public.speaker_relationships."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

from etl.wikidata.transform import is_blank_node, parse_qid, parse_year

logger = logging.getLogger(__name__)

DEFAULT_RELATIONSHIPS_QUERY_PATH = (
    Path(__file__).resolve().parent
    / "queries"
    / "italian_speaker_relationships.sparql"
)

PARENT_FROM_CHILD_PROPS = frozenset({"P22", "P25"})
SYMMETRIC_PROP_MAP: dict[str, str] = {
    "P3373": "sibling",
    "P26": "spouse",
    "P451": "partner",
    "P1038": "relative",
}


@dataclass(frozen=True)
class RelationshipRecord:
    """Normalized kinship edge ready for resolution against public.speakers."""

    subject_qid: str
    related_qid: str
    relationship_type: str
    is_ended: bool | None = None
    start_year: int | None = None
    end_year: int | None = None
    source: str = "wikidata"


@dataclass(frozen=True)
class RelationshipTransformResult:
    """Aggregated outcome of transforming raw WDQS relationship bindings."""

    records: list[RelationshipRecord]
    discarded_count: int
    duplicate_count: int


@dataclass(frozen=True)
class RelationshipLoadResult:
    """Row-level counts returned after loading edges into public.speaker_relationships."""

    inserted: int
    updated: int
    unchanged: int
    unmatched_speakers: int


def _cell_raw_value(binding: dict[str, Any], key: str) -> tuple[str | None, bool]:
    """Return (clean_text_or_none, has_unknown_bnode_value) for a binding cell."""
    cell = binding.get(key)
    if not isinstance(cell, dict):
        return None, False
    if cell.get("type") == "bnode":
        return None, True
    raw = cell.get("value")
    if raw is None:
        return None, False
    text = str(raw).strip()
    if not text:
        return None, False
    if is_blank_node(text):
        return None, True
    return text, False


def _merge_ended_flag(a: bool | None, b: bool | None) -> bool | None:
    if a is True or b is True:
        return True
    if a is False:
        return False
    if b is False:
        return False
    return None


def _merge_lifecycle(
    a_ended: bool | None,
    a_start: int | None,
    a_end: int | None,
    b_ended: bool | None,
    b_start: int | None,
    b_end: int | None,
) -> tuple[bool | None, int | None, int | None]:
    """Merge lifecycle metadata deterministically across duplicate edge observations."""
    start_candidates = [y for y in (a_start, b_start) if y is not None]
    end_candidates = [y for y in (a_end, b_end) if y is not None]
    merged_start = min(start_candidates) if start_candidates else None
    merged_end = max(end_candidates) if end_candidates else None
    merged_ended = _merge_ended_flag(a_ended, b_ended)

    if merged_start is not None and merged_end is not None and merged_end < merged_start:
        merged_start = a_start
        merged_end = a_end

    if merged_end is not None and merged_ended is not True:
        merged_ended = True

    return merged_ended, merged_start, merged_end


def transform_relationship_binding(
    binding: dict[str, Any],
) -> RelationshipRecord | None:
    """Transform one raw WDQS relationship binding into a RelationshipRecord."""
    person_raw, _ = _cell_raw_value(binding, "person")
    related_raw, _ = _cell_raw_value(binding, "related")
    prop_raw, _ = _cell_raw_value(binding, "prop")

    person_qid = parse_qid(person_raw)
    related_qid = parse_qid(related_raw)
    if not person_qid or not related_qid or not prop_raw:
        return None

    if person_qid == related_qid:
        return None

    prop_code = prop_raw.rsplit("/", 1)[-1].upper()
    if prop_code in PARENT_FROM_CHILD_PROPS:
        subject_qid = related_qid
        object_qid = person_qid
        rel_type = "parent_of"
    elif prop_code == "P40":
        subject_qid = person_qid
        object_qid = related_qid
        rel_type = "parent_of"
    elif prop_code in SYMMETRIC_PROP_MAP:
        subject_qid = person_qid
        object_qid = related_qid
        rel_type = SYMMETRIC_PROP_MAP[prop_code]
    else:
        return None

    start_raw, _ = _cell_raw_value(binding, "startDate")
    end_raw, end_is_bnode = _cell_raw_value(binding, "endDate")
    end_cause_raw, end_cause_is_bnode = _cell_raw_value(binding, "endCause")

    start_year = parse_year(start_raw)
    end_year = parse_year(end_raw)
    end_cause_qid = parse_qid(end_cause_raw)

    is_ended: bool | None = None
    if (
        end_year is not None
        or end_is_bnode
        or end_cause_qid is not None
        or end_cause_is_bnode
    ):
        is_ended = True

    if start_year is not None and end_year is not None and end_year < start_year:
        logger.warning(
            "Clearing contradictory relationship years for %s -> %s (%s): start=%d end=%d",
            subject_qid,
            object_qid,
            rel_type,
            start_year,
            end_year,
        )
        start_year = None
        end_year = None

    return RelationshipRecord(
        subject_qid=subject_qid,
        related_qid=object_qid,
        relationship_type=rel_type,
        is_ended=is_ended,
        start_year=start_year,
        end_year=end_year,
    )


def _canonical_qid_edge_key(record: RelationshipRecord) -> tuple[str, str, str]:
    if record.relationship_type == "parent_of":
        return (record.subject_qid, record.related_qid, record.relationship_type)
    low, high = sorted((record.subject_qid, record.related_qid))
    return (low, high, record.relationship_type)


def transform_relationship_bindings(
    bindings: Iterable[dict[str, Any]],
) -> RelationshipTransformResult:
    """Transform and deduplicate raw WDQS kinship bindings."""
    by_key: dict[tuple[str, str, str], RelationshipRecord] = {}
    discarded_count = 0
    duplicate_count = 0

    for binding in bindings:
        rec = transform_relationship_binding(binding)
        if rec is None:
            discarded_count += 1
            continue

        key = _canonical_qid_edge_key(rec)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = RelationshipRecord(
                subject_qid=key[0],
                related_qid=key[1],
                relationship_type=key[2],
                is_ended=rec.is_ended,
                start_year=rec.start_year,
                end_year=rec.end_year,
                source=rec.source,
            )
        else:
            duplicate_count += 1
            merged_ended, merged_start, merged_end = _merge_lifecycle(
                existing.is_ended,
                existing.start_year,
                existing.end_year,
                rec.is_ended,
                rec.start_year,
                rec.end_year,
            )
            by_key[key] = RelationshipRecord(
                subject_qid=key[0],
                related_qid=key[1],
                relationship_type=key[2],
                is_ended=merged_ended,
                start_year=merged_start,
                end_year=merged_end,
                source=existing.source,
            )

    ordered = [by_key[k] for k in sorted(by_key)]
    return RelationshipTransformResult(
        records=ordered,
        discarded_count=discarded_count,
        duplicate_count=duplicate_count,
    )


def _merge_db_lifecycle(
    existing: dict[str, Any],
    incoming: tuple[bool | None, int | None, int | None],
) -> tuple[bool | None, int | None, int | None]:
    """Compute desired lifecycle columns respecting is_verified and DB constraints."""
    ex_ended = existing["is_ended"]
    ex_start = existing["start_year"]
    ex_end = existing["end_year"]
    inc_ended, inc_start, inc_end = incoming

    if existing["is_verified"]:
        ended = ex_ended if ex_ended is not None else inc_ended
        start = ex_start if ex_start is not None else inc_start
        end = ex_end if ex_end is not None else inc_end
        if ended is False and end is not None:
            end = ex_end
        if start is not None and end is not None and end < start:
            return ex_ended, ex_start, ex_end
        if end is not None and ended is not True:
            ended = True
        return ended, start, end

    ended = inc_ended if inc_ended is not None else ex_ended
    start = inc_start if inc_start is not None else ex_start
    end = inc_end if inc_end is not None else ex_end
    if start is not None and end is not None and end < start:
        return ex_ended, ex_start, ex_end
    if end is not None and ended is not True:
        ended = True
    return ended, start, end


def load_relationships(
    conn: psycopg.Connection[dict[str, Any]],
    records: Sequence[RelationshipRecord],
    *,
    seen_at: datetime | None = None,
) -> RelationshipLoadResult:
    """Idempotently load RelationshipRecords into public.speaker_relationships."""
    if not records:
        return RelationshipLoadResult(
            inserted=0, updated=0, unchanged=0, unmatched_speakers=0
        )

    run_ts = seen_at or datetime.now(timezone.utc)
    all_qids = sorted(
        {rec.subject_qid for rec in records} | {rec.related_qid for rec in records}
    )

    with conn.cursor() as cur:
        speaker_rows = cur.execute(
            """
            SELECT id, wikidata_qid
            FROM public.speakers
            WHERE wikidata_qid = ANY(%s)
            """,
            (all_qids,),
        ).fetchall()
        qid_to_id = {row["wikidata_qid"]: int(row["id"]) for row in speaker_rows}

        resolved_edges: dict[
            tuple[int, int, str], tuple[bool | None, int | None, int | None, str]
        ] = {}
        unmatched_speakers = 0

        for rec in records:
            subj_id = qid_to_id.get(rec.subject_qid)
            rel_id = qid_to_id.get(rec.related_qid)
            if subj_id is None or rel_id is None or subj_id == rel_id:
                unmatched_speakers += 1
                continue

            if rec.relationship_type != "parent_of" and subj_id > rel_id:
                subj_id, rel_id = rel_id, subj_id

            edge_key = (subj_id, rel_id, rec.relationship_type)
            prev = resolved_edges.get(edge_key)
            if prev is None:
                resolved_edges[edge_key] = (
                    rec.is_ended,
                    rec.start_year,
                    rec.end_year,
                    rec.source,
                )
            else:
                m_ended, m_start, m_end = _merge_lifecycle(
                    prev[0],
                    prev[1],
                    prev[2],
                    rec.is_ended,
                    rec.start_year,
                    rec.end_year,
                )
                resolved_edges[edge_key] = (m_ended, m_start, m_end, prev[3])

        if not resolved_edges:
            return RelationshipLoadResult(
                inserted=0,
                updated=0,
                unchanged=0,
                unmatched_speakers=unmatched_speakers,
            )

        subject_ids = sorted({edge[0] for edge in resolved_edges})
        existing_rows = cur.execute(
            """
            SELECT
                id,
                speaker_id,
                related_speaker_id,
                relationship_type,
                is_ended,
                start_year,
                end_year,
                is_verified
            FROM public.speaker_relationships
            WHERE speaker_id = ANY(%s)
            """,
            (subject_ids,),
        ).fetchall()

        existing_by_key = {
            (
                int(row["speaker_id"]),
                int(row["related_speaker_id"]),
                row["relationship_type"],
            ): row
            for row in existing_rows
        }

        inserted = 0
        updated = 0
        unchanged = 0
        unchanged_ids: list[int] = []

        for edge_key, lifecycle in sorted(resolved_edges.items()):
            subj_id, rel_id, rel_type = edge_key
            inc_ended, inc_start, inc_end, source = lifecycle
            existing = existing_by_key.get(edge_key)

            if existing is None:
                cur.execute(
                    """
                    INSERT INTO public.speaker_relationships (
                        speaker_id,
                        related_speaker_id,
                        relationship_type,
                        is_ended,
                        start_year,
                        end_year,
                        source,
                        last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        subj_id,
                        rel_id,
                        rel_type,
                        inc_ended,
                        inc_start,
                        inc_end,
                        source,
                        run_ts,
                    ),
                )
                inserted += 1
                continue

            desired_ended, desired_start, desired_end = _merge_db_lifecycle(
                existing, (inc_ended, inc_start, inc_end)
            )
            if (desired_ended, desired_start, desired_end) != (
                existing["is_ended"],
                existing["start_year"],
                existing["end_year"],
            ):
                cur.execute(
                    """
                    UPDATE public.speaker_relationships
                    SET
                        is_ended = %s,
                        start_year = %s,
                        end_year = %s,
                        last_seen_at = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        desired_ended,
                        desired_start,
                        desired_end,
                        run_ts,
                        existing["id"],
                    ),
                )
                updated += 1
            else:
                unchanged_ids.append(int(existing["id"]))
                unchanged += 1

        if unchanged_ids:
            cur.execute(
                """
                UPDATE public.speaker_relationships
                SET last_seen_at = %s
                WHERE id = ANY(%s)
                  AND (last_seen_at IS NULL OR last_seen_at < %s)
                """,
                (run_ts, unchanged_ids, run_ts),
            )

    return RelationshipLoadResult(
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        unmatched_speakers=unmatched_speakers,
    )
