"""Transforms and idempotent loaders for speaker satellite tables."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import psycopg

from etl.wikidata.transform import (
    ISO_LANG_RE,
    clean_text,
    is_blank_node,
    parse_qid,
)

logger = logging.getLogger(__name__)

QUERIES_DIR = Path(__file__).resolve().parent / "queries"
DEFAULT_ALIASES_QUERY_PATH = QUERIES_DIR / "italian_speaker_aliases.sparql"
DEFAULT_LANGUAGES_QUERY_PATH = QUERIES_DIR / "italian_speaker_languages.sparql"
DEFAULT_EXTERNAL_IDS_QUERY_PATH = QUERIES_DIR / "italian_speaker_external_ids.sparql"

ALLOWED_ALIAS_TYPES = (
    "stage_name",
    "legal_name",
    "maiden_name",
    "credit_variant",
    "transliteration",
    "other",
)
ALIAS_TYPE_PRECEDENCE = {name: idx for idx, name in enumerate(ALLOWED_ALIAS_TYPES)}
LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[A-Z][a-z]{3})?(?:-[A-Z]{2})?$")

ALLOWED_LANGUAGE_ROLES = frozenset({"native", "working", "spoken"})

PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
CORE_BRIDGE_PROVIDERS = frozenset({"wikidata", "imdb", "tmdb"})
WIKI_PROVIDERS = frozenset({"itwiki", "enwiki"})


@dataclass(frozen=True)
class AliasRecord:
    """Normalized speaker alias record ready for public.speaker_aliases."""

    wikidata_qid: str
    alias_name: str
    alias_type: str = "other"
    locale: str | None = None
    source: str = "wikidata"


@dataclass(frozen=True)
class LanguageRecord:
    """Normalized speaker language record ready for public.speaker_languages."""

    wikidata_qid: str
    lang_code: str
    role: str = "spoken"
    source: str = "wikidata"


@dataclass(frozen=True)
class ExternalIdRecord:
    """Normalized speaker external catalog ID ready for public.speaker_external_ids."""

    wikidata_qid: str
    provider: str
    external_id: str
    source: str = "wikidata"


@dataclass(frozen=True)
class AliasesTransformResult:
    """Aggregated outcome of transforming raw alias bindings."""

    records: list[AliasRecord]
    discarded_count: int
    duplicate_count: int


@dataclass(frozen=True)
class LanguagesTransformResult:
    """Aggregated outcome of transforming raw language bindings."""

    records: list[LanguageRecord]
    discarded_count: int
    duplicate_count: int


@dataclass(frozen=True)
class ExternalIdsTransformResult:
    """Aggregated outcome of transforming raw external ID bindings."""

    records: list[ExternalIdRecord]
    discarded_count: int
    duplicate_count: int


@dataclass(frozen=True)
class SatelliteLoadResult:
    """Row-level counts returned after loading one speaker satellite table."""

    inserted: int
    updated: int
    unchanged: int
    unmatched_speakers: int
    conflicts_skipped: int = 0


def _cell_text(binding: dict[str, Any], key: str) -> str | None:
    cell = binding.get(key)
    if not isinstance(cell, dict):
        return None
    if cell.get("type") == "bnode":
        return None
    raw = cell.get("value")
    if raw is None:
        return None
    return clean_text(str(raw))


def _normalize_locale(raw_lang: Any) -> str | None:
    if raw_lang is None:
        return None
    cleaned = str(raw_lang).strip()
    if not cleaned or cleaned == "mul":
        return None
    return cleaned if LOCALE_RE.match(cleaned) else None


def _normalize_external_id(provider: str, raw_value: str) -> str | None:
    value = clean_text(raw_value)
    if not value or is_blank_node(value):
        return None
    if provider in WIKI_PROVIDERS and "/wiki/" in value:
        title = value.split("/wiki/", 1)[1]
        value = clean_text(unquote(title))
        if not value:
            return None
    return value


def transform_alias_bindings(
    bindings: Iterable[dict[str, Any]],
) -> AliasesTransformResult:
    """Transform and deduplicate raw WDQS alias bindings."""
    by_key: dict[tuple[str, str, str], AliasRecord] = {}
    discarded_count = 0
    duplicate_count = 0

    for binding in bindings:
        qid = parse_qid(_cell_text(binding, "person"))
        alias_name = _cell_text(binding, "aliasName")
        alias_type = (_cell_text(binding, "aliasType") or "other").lower()
        if not qid or not alias_name or alias_type not in ALIAS_TYPE_PRECEDENCE:
            discarded_count += 1
            continue

        cell = binding.get("aliasName")
        raw_locale = cell.get("xml:lang") if isinstance(cell, dict) else None
        locale = _normalize_locale(raw_locale)

        key = (qid, alias_name.casefold(), locale or "")
        existing = by_key.get(key)
        candidate = AliasRecord(
            wikidata_qid=qid,
            alias_name=alias_name,
            alias_type=alias_type,
            locale=locale,
        )
        if existing is None:
            by_key[key] = candidate
        else:
            duplicate_count += 1
            if (
                ALIAS_TYPE_PRECEDENCE[candidate.alias_type]
                < ALIAS_TYPE_PRECEDENCE[existing.alias_type]
            ):
                by_key[key] = candidate

    ordered = [by_key[k] for k in sorted(by_key)]
    return AliasesTransformResult(
        records=ordered,
        discarded_count=discarded_count,
        duplicate_count=duplicate_count,
    )


def transform_language_bindings(
    bindings: Iterable[dict[str, Any]],
) -> LanguagesTransformResult:
    """Transform and deduplicate raw WDQS language bindings."""
    by_key: dict[tuple[str, str, str], LanguageRecord] = {}
    discarded_count = 0
    duplicate_count = 0

    for binding in bindings:
        qid = parse_qid(_cell_text(binding, "person"))
        raw_code = _cell_text(binding, "langCode")
        role = (_cell_text(binding, "role") or "spoken").lower()
        lang_code = raw_code.lower() if raw_code else ""
        if (
            not qid
            or not ISO_LANG_RE.match(lang_code)
            or role not in ALLOWED_LANGUAGE_ROLES
        ):
            discarded_count += 1
            continue

        key = (qid, lang_code, role)
        if key in by_key:
            duplicate_count += 1
            continue
        by_key[key] = LanguageRecord(wikidata_qid=qid, lang_code=lang_code, role=role)

    ordered = [by_key[k] for k in sorted(by_key)]
    return LanguagesTransformResult(
        records=ordered,
        discarded_count=discarded_count,
        duplicate_count=duplicate_count,
    )


def transform_external_id_bindings(
    bindings: Iterable[dict[str, Any]],
) -> ExternalIdsTransformResult:
    """Transform and deduplicate raw WDQS external identifier bindings."""
    by_key: dict[tuple[str, str, str], ExternalIdRecord] = {}
    discarded_count = 0
    duplicate_count = 0

    for binding in bindings:
        qid = parse_qid(_cell_text(binding, "person"))
        raw_provider = _cell_text(binding, "provider")
        raw_ext = _cell_text(binding, "externalId")
        if not qid or not raw_provider or not raw_ext:
            discarded_count += 1
            continue

        provider = raw_provider.lower()
        if (
            not PROVIDER_RE.match(provider)
            or provider in CORE_BRIDGE_PROVIDERS
        ):
            discarded_count += 1
            continue

        external_id = _normalize_external_id(provider, raw_ext)
        if not external_id:
            discarded_count += 1
            continue

        key = (qid, provider, external_id)
        if key in by_key:
            duplicate_count += 1
            continue
        by_key[key] = ExternalIdRecord(
            wikidata_qid=qid,
            provider=provider,
            external_id=external_id,
        )

    ordered = [by_key[k] for k in sorted(by_key)]
    return ExternalIdsTransformResult(
        records=ordered,
        discarded_count=discarded_count,
        duplicate_count=duplicate_count,
    )


def _resolve_speakers_by_qid(
    cur: psycopg.Cursor[dict[str, Any]],
    qids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    if not qids:
        return {}
    rows = cur.execute(
        """
        SELECT id, wikidata_qid, display_name
        FROM public.speakers
        WHERE wikidata_qid = ANY(%s)
        """,
        (list(qids),),
    ).fetchall()
    return {row["wikidata_qid"]: row for row in rows}


def _touch_last_seen_at(
    cur: psycopg.Cursor[dict[str, Any]],
    table_name: str,
    unchanged_ids: Sequence[int],
    run_ts: datetime,
) -> None:
    if not unchanged_ids:
        return
    cur.execute(
        f"""
        UPDATE public.{table_name}
        SET last_seen_at = %s
        WHERE id = ANY(%s)
          AND (last_seen_at IS NULL OR last_seen_at < %s)
        """,
        (run_ts, list(unchanged_ids), run_ts),
    )


def load_speaker_aliases(
    conn: psycopg.Connection[dict[str, Any]],
    records: Sequence[AliasRecord],
    *,
    seen_at: datetime | None = None,
) -> SatelliteLoadResult:
    """Idempotently load AliasRecords into public.speaker_aliases."""
    if not records:
        return SatelliteLoadResult(inserted=0, updated=0, unchanged=0, unmatched_speakers=0)

    run_ts = seen_at or datetime.now(timezone.utc)
    all_qids = sorted({rec.wikidata_qid for rec in records})

    with conn.cursor() as cur:
        speakers_by_qid = _resolve_speakers_by_qid(cur, all_qids)
        resolved: dict[tuple[int, str, str], tuple[str, str, str | None, str]] = {}
        unmatched_speakers = 0

        for rec in records:
            speaker = speakers_by_qid.get(rec.wikidata_qid)
            if speaker is None:
                unmatched_speakers += 1
                continue
            if rec.alias_name.casefold() == str(speaker["display_name"]).casefold():
                continue

            speaker_id = int(speaker["id"])
            key = (speaker_id, rec.alias_name.strip().lower(), rec.locale or "")
            existing_inc = resolved.get(key)
            if existing_inc is None:
                resolved[key] = (rec.alias_name, rec.alias_type, rec.locale, rec.source)
            elif (
                ALIAS_TYPE_PRECEDENCE[rec.alias_type]
                < ALIAS_TYPE_PRECEDENCE[existing_inc[1]]
            ):
                resolved[key] = (
                    existing_inc[0],
                    rec.alias_type,
                    existing_inc[2],
                    existing_inc[3],
                )

        if not resolved:
            return SatelliteLoadResult(
                inserted=0,
                updated=0,
                unchanged=0,
                unmatched_speakers=unmatched_speakers,
            )

        speaker_ids = sorted({key[0] for key in resolved})
        db_rows = cur.execute(
            """
            SELECT id, speaker_id, alias_name, alias_type, locale, is_verified
            FROM public.speaker_aliases
            WHERE speaker_id = ANY(%s)
            """,
            (speaker_ids,),
        ).fetchall()
        existing_by_key = {
            (
                int(row["speaker_id"]),
                str(row["alias_name"]).strip().lower(),
                row["locale"] or "",
            ): row
            for row in db_rows
        }

        inserted = 0
        updated = 0
        unchanged = 0
        unchanged_ids: list[int] = []

        for key, (alias_name, alias_type, locale, source) in sorted(resolved.items()):
            speaker_id = key[0]
            existing = existing_by_key.get(key)
            if existing is None:
                cur.execute(
                    """
                    INSERT INTO public.speaker_aliases (
                        speaker_id, alias_name, alias_type, locale, source, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (speaker_id, alias_name, alias_type, locale, source, run_ts),
                )
                inserted += 1
                continue

            should_update_type = (
                not existing["is_verified"] and existing["alias_type"] != alias_type
            )
            if should_update_type:
                cur.execute(
                    """
                    UPDATE public.speaker_aliases
                    SET alias_type = %s,
                        last_seen_at = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (alias_type, run_ts, existing["id"]),
                )
                updated += 1
            else:
                unchanged_ids.append(int(existing["id"]))
                unchanged += 1

        _touch_last_seen_at(cur, "speaker_aliases", unchanged_ids, run_ts)

    return SatelliteLoadResult(
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        unmatched_speakers=unmatched_speakers,
    )


def load_speaker_languages(
    conn: psycopg.Connection[dict[str, Any]],
    records: Sequence[LanguageRecord],
    *,
    seen_at: datetime | None = None,
) -> SatelliteLoadResult:
    """Idempotently load LanguageRecords into public.speaker_languages."""
    if not records:
        return SatelliteLoadResult(inserted=0, updated=0, unchanged=0, unmatched_speakers=0)

    run_ts = seen_at or datetime.now(timezone.utc)
    all_qids = sorted({rec.wikidata_qid for rec in records})

    with conn.cursor() as cur:
        speakers_by_qid = _resolve_speakers_by_qid(cur, all_qids)
        resolved: dict[tuple[int, str, str], str] = {}
        unmatched_speakers = 0

        for rec in records:
            speaker = speakers_by_qid.get(rec.wikidata_qid)
            if speaker is None:
                unmatched_speakers += 1
                continue
            key = (int(speaker["id"]), rec.lang_code, rec.role)
            resolved.setdefault(key, rec.source)

        if not resolved:
            return SatelliteLoadResult(
                inserted=0,
                updated=0,
                unchanged=0,
                unmatched_speakers=unmatched_speakers,
            )

        speaker_ids = sorted({key[0] for key in resolved})
        db_rows = cur.execute(
            """
            SELECT id, speaker_id, lang_code, role
            FROM public.speaker_languages
            WHERE speaker_id = ANY(%s)
            """,
            (speaker_ids,),
        ).fetchall()
        existing_by_key = {
            (int(row["speaker_id"]), row["lang_code"], row["role"]): int(row["id"])
            for row in db_rows
        }

        inserted = 0
        unchanged = 0
        unchanged_ids: list[int] = []

        for key, source in sorted(resolved.items()):
            existing_id = existing_by_key.get(key)
            if existing_id is None:
                speaker_id, lang_code, role = key
                cur.execute(
                    """
                    INSERT INTO public.speaker_languages (
                        speaker_id, lang_code, role, source, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (speaker_id, lang_code, role, source, run_ts),
                )
                inserted += 1
            else:
                unchanged_ids.append(existing_id)
                unchanged += 1

        _touch_last_seen_at(cur, "speaker_languages", unchanged_ids, run_ts)

    return SatelliteLoadResult(
        inserted=inserted,
        updated=0,
        unchanged=unchanged,
        unmatched_speakers=unmatched_speakers,
    )


def load_speaker_external_ids(
    conn: psycopg.Connection[dict[str, Any]],
    records: Sequence[ExternalIdRecord],
    *,
    seen_at: datetime | None = None,
) -> SatelliteLoadResult:
    """Idempotently load ExternalIdRecords into public.speaker_external_ids."""
    if not records:
        return SatelliteLoadResult(inserted=0, updated=0, unchanged=0, unmatched_speakers=0)

    run_ts = seen_at or datetime.now(timezone.utc)
    all_qids = sorted({rec.wikidata_qid for rec in records})

    with conn.cursor() as cur:
        speakers_by_qid = _resolve_speakers_by_qid(cur, all_qids)
        resolved: dict[tuple[int, str, str], str] = {}
        unmatched_speakers = 0

        for rec in records:
            speaker = speakers_by_qid.get(rec.wikidata_qid)
            if speaker is None:
                unmatched_speakers += 1
                continue
            key = (int(speaker["id"]), rec.provider, rec.external_id)
            resolved.setdefault(key, rec.source)

        if not resolved:
            return SatelliteLoadResult(
                inserted=0,
                updated=0,
                unchanged=0,
                unmatched_speakers=unmatched_speakers,
            )

        unique_pairs = sorted({(key[1], key[2]) for key in resolved})
        pair_providers = [p for p, _ in unique_pairs]
        pair_values = [v for _, v in unique_pairs]
        db_rows = cur.execute(
            """
            SELECT e.id, e.speaker_id, e.provider, e.external_id
            FROM public.speaker_external_ids e
            JOIN unnest(%s::text[], %s::text[]) AS t(provider, external_id)
              ON e.provider = t.provider
             AND e.external_id = t.external_id
            """,
            (pair_providers, pair_values),
        ).fetchall()
        owner_by_pair: dict[tuple[str, str], tuple[int, int]] = {
            (row["provider"], row["external_id"]): (
                int(row["speaker_id"]),
                int(row["id"]),
            )
            for row in db_rows
        }

        inserted = 0
        unchanged = 0
        conflicts_skipped = 0
        unchanged_ids: list[int] = []

        for key, source in sorted(resolved.items()):
            speaker_id, provider, external_id = key
            pair = (provider, external_id)
            owner = owner_by_pair.get(pair)
            if owner is not None:
                owner_speaker_id, owner_row_id = owner
                if owner_speaker_id == speaker_id:
                    if owner_row_id >= 0:
                        unchanged_ids.append(owner_row_id)
                    unchanged += 1
                else:
                    logger.warning(
                        "Skipping external_id (%s=%s) for speaker_id=%d because speaker_id=%d already owns it.",
                        provider,
                        external_id,
                        speaker_id,
                        owner_speaker_id,
                    )
                    conflicts_skipped += 1
                continue

            cur.execute(
                """
                INSERT INTO public.speaker_external_ids (
                    speaker_id, provider, external_id, source, last_seen_at
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (speaker_id, provider, external_id, source, run_ts),
            )
            owner_by_pair[pair] = (speaker_id, -1)
            inserted += 1

        _touch_last_seen_at(cur, "speaker_external_ids", unchanged_ids, run_ts)

    return SatelliteLoadResult(
        inserted=inserted,
        updated=0,
        unchanged=unchanged,
        unmatched_speakers=unmatched_speakers,
        conflicts_skipped=conflicts_skipped,
    )
