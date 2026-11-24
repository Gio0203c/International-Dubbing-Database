"""Idempotent transactional loader for public.speakers with bridge-ID resolution."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg

from etl.wikidata.transform import (
    PartialDate,
    SpeakerRecord,
    is_lifespan_order_valid,
    is_valid_partial_date,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadResult:
    """Row-level counts returned after loading a batch into public.speakers."""

    inserted: int
    updated: int
    unchanged: int
    bridge_conflicts_skipped: int


def _fetch_existing_candidates(
    conn: psycopg.Connection[dict[str, Any]],
    records: Sequence[SpeakerRecord],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Load existing speakers that match any incoming bridge identifier."""
    qids = [r.wikidata_qid for r in records if r.wikidata_qid]
    imdbs = [r.imdb_nconst for r in records if r.imdb_nconst]
    tmdbs = [r.tmdb_person_id for r in records if r.tmdb_person_id is not None]

    if not qids and not imdbs and not tmdbs:
        return {}, {}, {}

    sql = """
        SELECT
            id,
            display_name,
            sort_name,
            legal_name,
            birth_year,
            birth_month,
            birth_day,
            death_year,
            death_month,
            death_day,
            gender,
            native_lang,
            country_code,
            wikidata_qid,
            imdb_nconst,
            tmdb_person_id,
            is_verified
        FROM public.speakers
        WHERE wikidata_qid = ANY(%s)
           OR imdb_nconst = ANY(%s)
           OR tmdb_person_id = ANY(%s)
    """
    with conn.cursor() as cur:
        rows = cur.execute(sql, (qids, imdbs, tmdbs)).fetchall()

    by_qid: dict[str, dict[str, Any]] = {}
    by_imdb: dict[str, dict[str, Any]] = {}
    by_tmdb: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row["wikidata_qid"] is not None:
            by_qid[row["wikidata_qid"]] = row
        if row["imdb_nconst"] is not None:
            by_imdb[row["imdb_nconst"]] = row
        if row["tmdb_person_id"] is not None:
            by_tmdb[row["tmdb_person_id"]] = row
    return by_qid, by_imdb, by_tmdb


def _resolve_target_row(
    record: SpeakerRecord,
    by_qid: dict[str, dict[str, Any]],
    by_imdb: dict[str, dict[str, Any]],
    by_tmdb: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None, int | None, int]:
    """Resolve an incoming record across bridge IDs in cascading order.

    Order: wikidata_qid -> imdb_nconst -> tmdb_person_id.
    Only merges on imdb_nconst or tmdb_person_id when the matched row has no
    conflicting wikidata_qid. When two different QIDs claim the same external
    ID, drops the conflicting external ID on the incoming record to preserve
    UNIQUE constraints without aborting the batch.
    """
    conflicts = 0
    safe_imdb = record.imdb_nconst
    safe_tmdb = record.tmdb_person_id

    target = by_qid.get(record.wikidata_qid)

    if target is None and safe_imdb is not None:
        imdb_row = by_imdb.get(safe_imdb)
        if imdb_row is not None:
            if imdb_row["wikidata_qid"] is None:
                target = imdb_row
            else:
                logger.warning(
                    "Bridge conflict on imdb_nconst=%s between incoming %s and existing %s; dropping IMDb ID on incoming row.",
                    safe_imdb,
                    record.wikidata_qid,
                    imdb_row["wikidata_qid"],
                )
                safe_imdb = None
                conflicts += 1

    if target is None and safe_tmdb is not None:
        tmdb_row = by_tmdb.get(safe_tmdb)
        if tmdb_row is not None:
            if tmdb_row["wikidata_qid"] is None:
                target = tmdb_row
            else:
                logger.warning(
                    "Bridge conflict on tmdb_person_id=%s between incoming %s and existing %s; dropping TMDB ID on incoming row.",
                    safe_tmdb,
                    record.wikidata_qid,
                    tmdb_row["wikidata_qid"],
                )
                safe_tmdb = None
                conflicts += 1

    target_id = target["id"] if target is not None else None

    # Guard against colliding with a different speaker's UNIQUE bridge IDs.
    if safe_imdb is not None:
        imdb_owner = by_imdb.get(safe_imdb)
        if imdb_owner is not None and imdb_owner["id"] != target_id:
            logger.warning(
                "Skipping imdb_nconst=%s for %s because speaker id=%d already owns it.",
                safe_imdb,
                record.wikidata_qid,
                imdb_owner["id"],
            )
            safe_imdb = None
            conflicts += 1

    if safe_tmdb is not None:
        tmdb_owner = by_tmdb.get(safe_tmdb)
        if tmdb_owner is not None and tmdb_owner["id"] != target_id:
            logger.warning(
                "Skipping tmdb_person_id=%s for %s because speaker id=%d already owns it.",
                safe_tmdb,
                record.wikidata_qid,
                tmdb_owner["id"],
            )
            safe_tmdb = None
            conflicts += 1

    return target, safe_imdb, safe_tmdb, conflicts


def _register_in_memory(
    row: dict[str, Any],
    by_qid: dict[str, dict[str, Any]],
    by_imdb: dict[str, dict[str, Any]],
    by_tmdb: dict[int, dict[str, Any]],
) -> None:
    if row["wikidata_qid"] is not None:
        by_qid[row["wikidata_qid"]] = row
    if row["imdb_nconst"] is not None:
        by_imdb[row["imdb_nconst"]] = row
    if row["tmdb_person_id"] is not None:
        by_tmdb[row["tmdb_person_id"]] = row


def _insert_speaker(
    cur: psycopg.Cursor[dict[str, Any]],
    record: SpeakerRecord,
    safe_imdb: str | None,
    safe_tmdb: int | None,
    seen_at: datetime,
) -> dict[str, Any]:
    sql = """
        INSERT INTO public.speakers (
            display_name,
            sort_name,
            legal_name,
            birth_year,
            birth_month,
            birth_day,
            death_year,
            death_month,
            death_day,
            gender,
            native_lang,
            country_code,
            wikidata_qid,
            imdb_nconst,
            tmdb_person_id,
            primary_source,
            last_seen_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (wikidata_qid) DO UPDATE SET
            display_name = CASE
                WHEN public.speakers.is_verified THEN public.speakers.display_name
                ELSE EXCLUDED.display_name
            END,
            sort_name = CASE
                WHEN public.speakers.is_verified THEN public.speakers.sort_name
                ELSE EXCLUDED.sort_name
            END,
            legal_name = CASE
                WHEN public.speakers.is_verified THEN COALESCE(public.speakers.legal_name, EXCLUDED.legal_name)
                ELSE COALESCE(EXCLUDED.legal_name, public.speakers.legal_name)
            END,
            birth_year = CASE
                WHEN public.speakers.is_verified THEN COALESCE(public.speakers.birth_year, EXCLUDED.birth_year)
                ELSE COALESCE(EXCLUDED.birth_year, public.speakers.birth_year)
            END,
            birth_month = CASE
                WHEN public.speakers.is_verified THEN COALESCE(public.speakers.birth_month, EXCLUDED.birth_month)
                ELSE COALESCE(EXCLUDED.birth_month, public.speakers.birth_month)
            END,
            birth_day = CASE
                WHEN public.speakers.is_verified THEN COALESCE(public.speakers.birth_day, EXCLUDED.birth_day)
                ELSE COALESCE(EXCLUDED.birth_day, public.speakers.birth_day)
            END,
            death_year = CASE
                WHEN public.speakers.is_verified THEN COALESCE(public.speakers.death_year, EXCLUDED.death_year)
                ELSE COALESCE(EXCLUDED.death_year, public.speakers.death_year)
            END,
            death_month = CASE
                WHEN public.speakers.is_verified THEN COALESCE(public.speakers.death_month, EXCLUDED.death_month)
                ELSE COALESCE(EXCLUDED.death_month, public.speakers.death_month)
            END,
            death_day = CASE
                WHEN public.speakers.is_verified THEN COALESCE(public.speakers.death_day, EXCLUDED.death_day)
                ELSE COALESCE(EXCLUDED.death_day, public.speakers.death_day)
            END,
            gender = CASE
                WHEN public.speakers.is_verified THEN COALESCE(public.speakers.gender, EXCLUDED.gender)
                ELSE COALESCE(EXCLUDED.gender, public.speakers.gender)
            END,
            native_lang = CASE
                WHEN public.speakers.is_verified THEN COALESCE(public.speakers.native_lang, EXCLUDED.native_lang)
                ELSE COALESCE(EXCLUDED.native_lang, public.speakers.native_lang)
            END,
            country_code = CASE
                WHEN public.speakers.is_verified THEN COALESCE(public.speakers.country_code, EXCLUDED.country_code)
                ELSE COALESCE(EXCLUDED.country_code, public.speakers.country_code)
            END,
            imdb_nconst = CASE
                WHEN public.speakers.is_verified THEN COALESCE(public.speakers.imdb_nconst, EXCLUDED.imdb_nconst)
                ELSE COALESCE(EXCLUDED.imdb_nconst, public.speakers.imdb_nconst)
            END,
            tmdb_person_id = CASE
                WHEN public.speakers.is_verified THEN COALESCE(public.speakers.tmdb_person_id, EXCLUDED.tmdb_person_id)
                ELSE COALESCE(EXCLUDED.tmdb_person_id, public.speakers.tmdb_person_id)
            END,
            last_seen_at = EXCLUDED.last_seen_at,
            updated_at = NOW()
        RETURNING
            id,
            display_name,
            sort_name,
            legal_name,
            birth_year,
            birth_month,
            birth_day,
            death_year,
            death_month,
            death_day,
            gender,
            native_lang,
            country_code,
            wikidata_qid,
            imdb_nconst,
            tmdb_person_id,
            is_verified
    """
    row = cur.execute(
        sql,
        (
            record.display_name,
            record.sort_name,
            record.legal_name,
            record.birth_year,
            record.birth_month,
            record.birth_day,
            record.death_year,
            record.death_month,
            record.death_day,
            record.gender,
            record.native_lang,
            record.country_code,
            record.wikidata_qid,
            safe_imdb,
            safe_tmdb,
            record.primary_source,
            seen_at,
        ),
    ).fetchone()
    assert row is not None
    return row


def _merge_partial_date(
    existing_date: PartialDate,
    incoming_date: PartialDate,
    *,
    is_verified: bool,
) -> PartialDate:
    """Merge partial date parts safely without violating calendar constraints."""
    primary, secondary = (
        (existing_date, incoming_date)
        if is_verified
        else (incoming_date, existing_date)
    )

    year = primary.year if primary.year is not None else secondary.year
    years_compatible = (
        primary.year is None
        or secondary.year is None
        or primary.year == secondary.year
    )

    if primary.month is not None:
        month = primary.month
    elif years_compatible:
        month = secondary.month
    else:
        month = None

    months_compatible = (
        primary.month is None
        or secondary.month is None
        or primary.month == secondary.month
    )

    if primary.day is not None:
        day = primary.day
    elif years_compatible and months_compatible and month is not None:
        day = secondary.day
    else:
        day = None

    candidate = PartialDate(year=year, month=month, day=day)
    if is_valid_partial_date(candidate.year, candidate.month, candidate.day):
        return candidate
    return existing_date


def _merge_fields(
    existing: dict[str, Any],
    record: SpeakerRecord,
    safe_imdb: str | None,
    safe_tmdb: int | None,
) -> dict[str, Any]:
    """Compute desired column values respecting is_verified precedence."""
    existing_birth = PartialDate(
        existing["birth_year"], existing["birth_month"], existing["birth_day"]
    )
    incoming_birth = PartialDate(
        record.birth_year, record.birth_month, record.birth_day
    )
    existing_death = PartialDate(
        existing["death_year"], existing["death_month"], existing["death_day"]
    )
    incoming_death = PartialDate(
        record.death_year, record.death_month, record.death_day
    )

    is_verified = bool(existing["is_verified"])
    merged_birth = _merge_partial_date(
        existing_birth, incoming_birth, is_verified=is_verified
    )
    merged_death = _merge_partial_date(
        existing_death, incoming_death, is_verified=is_verified
    )
    if not is_lifespan_order_valid(merged_birth, merged_death):
        merged_birth = existing_birth
        merged_death = existing_death

    if is_verified:
        return {
            "display_name": existing["display_name"],
            "sort_name": existing["sort_name"],
            "legal_name": existing["legal_name"] or record.legal_name,
            "birth_year": merged_birth.year,
            "birth_month": merged_birth.month,
            "birth_day": merged_birth.day,
            "death_year": merged_death.year,
            "death_month": merged_death.month,
            "death_day": merged_death.day,
            "gender": existing["gender"] or record.gender,
            "native_lang": existing["native_lang"] or record.native_lang,
            "country_code": existing["country_code"] or record.country_code,
            "wikidata_qid": existing["wikidata_qid"] or record.wikidata_qid,
            "imdb_nconst": existing["imdb_nconst"] or safe_imdb,
            "tmdb_person_id": (
                existing["tmdb_person_id"]
                if existing["tmdb_person_id"] is not None
                else safe_tmdb
            ),
        }

    return {
        "display_name": record.display_name,
        "sort_name": record.sort_name,
        "legal_name": record.legal_name or existing["legal_name"],
        "birth_year": merged_birth.year,
        "birth_month": merged_birth.month,
        "birth_day": merged_birth.day,
        "death_year": merged_death.year,
        "death_month": merged_death.month,
        "death_day": merged_death.day,
        "gender": record.gender or existing["gender"],
        "native_lang": record.native_lang or existing["native_lang"],
        "country_code": record.country_code or existing["country_code"],
        "wikidata_qid": existing["wikidata_qid"] or record.wikidata_qid,
        "imdb_nconst": safe_imdb or existing["imdb_nconst"],
        "tmdb_person_id": (
            safe_tmdb
            if safe_tmdb is not None
            else existing["tmdb_person_id"]
        ),
    }


def _update_speaker_if_changed(
    cur: psycopg.Cursor[dict[str, Any]],
    speaker_id: int,
    desired: dict[str, Any],
    seen_at: datetime,
) -> dict[str, Any] | None:
    """Update a speaker only when at least one column IS DISTINCT FROM current state."""
    sql = """
        UPDATE public.speakers
        SET
            display_name = %(display_name)s,
            sort_name = %(sort_name)s,
            legal_name = %(legal_name)s,
            birth_year = %(birth_year)s,
            birth_month = %(birth_month)s,
            birth_day = %(birth_day)s,
            death_year = %(death_year)s,
            death_month = %(death_month)s,
            death_day = %(death_day)s,
            gender = %(gender)s,
            native_lang = %(native_lang)s,
            country_code = %(country_code)s,
            wikidata_qid = %(wikidata_qid)s,
            imdb_nconst = %(imdb_nconst)s,
            tmdb_person_id = %(tmdb_person_id)s,
            last_seen_at = %(seen_at)s,
            updated_at = NOW()
        WHERE id = %(id)s
          AND (
              display_name IS DISTINCT FROM %(display_name)s
              OR sort_name IS DISTINCT FROM %(sort_name)s
              OR legal_name IS DISTINCT FROM %(legal_name)s
              OR birth_year IS DISTINCT FROM %(birth_year)s
              OR birth_month IS DISTINCT FROM %(birth_month)s
              OR birth_day IS DISTINCT FROM %(birth_day)s
              OR death_year IS DISTINCT FROM %(death_year)s
              OR death_month IS DISTINCT FROM %(death_month)s
              OR death_day IS DISTINCT FROM %(death_day)s
              OR gender IS DISTINCT FROM %(gender)s
              OR native_lang IS DISTINCT FROM %(native_lang)s
              OR country_code IS DISTINCT FROM %(country_code)s
              OR wikidata_qid IS DISTINCT FROM %(wikidata_qid)s
              OR imdb_nconst IS DISTINCT FROM %(imdb_nconst)s
              OR tmdb_person_id IS DISTINCT FROM %(tmdb_person_id)s
          )
        RETURNING
            id,
            display_name,
            sort_name,
            legal_name,
            birth_year,
            birth_month,
            birth_day,
            death_year,
            death_month,
            death_day,
            gender,
            native_lang,
            country_code,
            wikidata_qid,
            imdb_nconst,
            tmdb_person_id,
            is_verified
    """
    params = {**desired, "id": speaker_id, "seen_at": seen_at}
    return cur.execute(sql, params).fetchone()


def load_speakers(
    conn: psycopg.Connection[dict[str, Any]],
    records: Sequence[SpeakerRecord],
    *,
    seen_at: datetime | None = None,
) -> LoadResult:
    """Idempotently load validated SpeakerRecords into public.speakers.

    Must be called inside an open transaction. Never deletes missing rows;
    updates last_seen_at for confirmed records without modifying updated_at
    when no domain attribute changed.
    """
    if not records:
        return LoadResult(
            inserted=0, updated=0, unchanged=0, bridge_conflicts_skipped=0
        )

    run_ts = seen_at or datetime.now(timezone.utc)
    by_qid, by_imdb, by_tmdb = _fetch_existing_candidates(conn, records)

    inserted = 0
    updated = 0
    unchanged = 0
    conflicts_skipped = 0
    unchanged_ids: list[int] = []

    with conn.cursor() as cur:
        for record in records:
            target, safe_imdb, safe_tmdb, conflicts = _resolve_target_row(
                record, by_qid, by_imdb, by_tmdb
            )
            conflicts_skipped += conflicts

            if target is None:
                created = _insert_speaker(cur, record, safe_imdb, safe_tmdb, run_ts)
                _register_in_memory(created, by_qid, by_imdb, by_tmdb)
                inserted += 1
                continue

            desired = _merge_fields(target, record, safe_imdb, safe_tmdb)
            changed_row = _update_speaker_if_changed(
                cur, target["id"], desired, run_ts
            )
            if changed_row is not None:
                _register_in_memory(changed_row, by_qid, by_imdb, by_tmdb)
                updated += 1
            else:
                unchanged_ids.append(target["id"])
                unchanged += 1

        if unchanged_ids:
            cur.execute(
                """
                UPDATE public.speakers
                SET last_seen_at = %s
                WHERE id = ANY(%s)
                  AND (last_seen_at IS NULL OR last_seen_at < %s)
                """,
                (run_ts, unchanged_ids, run_ts),
            )

    return LoadResult(
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        bridge_conflicts_skipped=conflicts_skipped,
    )
