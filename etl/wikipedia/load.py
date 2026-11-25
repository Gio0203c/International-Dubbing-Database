"""Conservative, idempotent PostgreSQL loader for Wikipedia speaker records."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import unquote

import psycopg

from etl.wikipedia.transform import WikipediaSpeakerRecord

LOGGER = logging.getLogger(__name__)


def _canonical_wiki_external_id(page_url: str, page_title: str) -> str:
    raw = page_url.split("/wiki/", 1)[1] if "/wiki/" in page_url else page_title
    cleaned = unquote(raw).strip().replace(" ", "_")
    return cleaned or page_title.strip().replace(" ", "_")


def _normalize_wiki_key(value: str) -> str:
    raw = value.split("/wiki/", 1)[1] if "/wiki/" in value else value
    return unquote(raw).strip().replace(" ", "_").lower()


def load_wikipedia_speakers(
    conn: psycopg.Connection[dict[str, Any]],
    records: list[WikipediaSpeakerRecord],
    *,
    run_id: int | None = None,
) -> dict[str, int]:
    """Upsert Wikipedia speakers safely and record CC BY-SA 4.0 provenance."""
    speakers_inserted = 0
    speakers_updated = 0
    new_with_qid = 0
    new_without_qid = 0
    ext_ids_inserted = 0
    provenance_inserted = 0
    skipped_unconfirmed = 0

    if not records:
        return {
            "speakers_inserted": 0,
            "speakers_updated": 0,
            "new_with_qid": 0,
            "new_without_qid": 0,
            "ext_ids_inserted": 0,
            "provenance_inserted": 0,
            "skipped_unconfirmed": 0,
        }

    wiki_langs = sorted({rec.wiki_lang for rec in records})
    providers = [f"{lang}wiki" for lang in wiki_langs]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, display_name, sort_name, birth_year, death_year,
                   gender, country_code, wikidata_qid, primary_source, is_verified
            FROM public.speakers
            """
        )
        speakers_by_id: dict[int, dict[str, Any]] = {}
        speakers_by_qid: dict[str, dict[str, Any]] = {}
        for row in cur.fetchall():
            speakers_by_id[row["id"]] = row
            if row["wikidata_qid"] is not None:
                speakers_by_qid[row["wikidata_qid"]] = row

        cur.execute(
            """
            SELECT speaker_id, provider, external_id
            FROM public.speaker_external_ids
            WHERE provider = ANY(%s)
            """,
            (providers,),
        )
        ext_owner: dict[tuple[str, str], int] = {}
        for row in cur.fetchall():
            ext_owner[(row["provider"], _normalize_wiki_key(row["external_id"]))] = row["speaker_id"]

        for rec in records:
            provider = f"{rec.wiki_lang}wiki"
            source_type = "itwiki" if rec.wiki_lang == "it" else "wikipedia"
            canonical_ext_id = _canonical_wiki_external_id(rec.page_url, rec.page_title)
            norm_key = _normalize_wiki_key(canonical_ext_id)

            existing = (
                speakers_by_qid.get(rec.wikidata_qid)
                if rec.wikidata_qid is not None
                else None
            )
            if existing is None:
                matched_id = ext_owner.get((provider, norm_key))
                if matched_id is not None:
                    existing = speakers_by_id.get(matched_id)

            if existing is not None:
                speaker_id = existing["id"]
                if not existing["is_verified"] and rec.allow_bio_backfill:
                    new_birth = existing["birth_year"]
                    if (
                        new_birth is None
                        and rec.birth_year is not None
                        and (existing["death_year"] is None or rec.birth_year <= existing["death_year"])
                    ):
                        new_birth = rec.birth_year

                    new_death = existing["death_year"]
                    if (
                        new_death is None
                        and rec.death_year is not None
                        and (new_birth is None or rec.death_year >= new_birth)
                    ):
                        new_death = rec.death_year

                    new_gender = existing["gender"] if existing["gender"] is not None else rec.gender
                    new_country = (
                        existing["country_code"]
                        if existing["country_code"] is not None
                        else rec.country_code
                    )
                    new_sort_name = existing["sort_name"]
                    if (
                        existing["sort_name"] == existing["display_name"]
                        and rec.sort_name != rec.display_name
                    ):
                        new_sort_name = rec.sort_name

                    changed = (
                        existing["birth_year"],
                        existing["death_year"],
                        existing["gender"],
                        existing["country_code"],
                        existing["sort_name"],
                    ) != (
                        new_birth,
                        new_death,
                        new_gender,
                        new_country,
                        new_sort_name,
                    )
                    if changed:
                        cur.execute(
                            """
                            UPDATE public.speakers
                            SET birth_year = %s,
                                death_year = %s,
                                gender = %s,
                                country_code = %s,
                                sort_name = %s,
                                last_seen_at = NOW(),
                                updated_at = NOW()
                            WHERE id = %s
                            """,
                            (
                                new_birth,
                                new_death,
                                new_gender,
                                new_country,
                                new_sort_name,
                                speaker_id,
                            ),
                        )
                        existing["birth_year"] = new_birth
                        existing["death_year"] = new_death
                        existing["gender"] = new_gender
                        existing["country_code"] = new_country
                        existing["sort_name"] = new_sort_name
                        speakers_updated += 1
            else:
                if not rec.is_person_confirmed:
                    skipped_unconfirmed += 1
                    continue

                birth_year = rec.birth_year if rec.allow_bio_backfill else None
                death_year = rec.death_year if rec.allow_bio_backfill else None
                gender = rec.gender if rec.allow_bio_backfill else None
                country_code = rec.country_code if rec.allow_bio_backfill else None
                sort_name = rec.sort_name if rec.allow_bio_backfill else rec.display_name

                cur.execute(
                    """
                    INSERT INTO public.speakers (
                        display_name, sort_name, birth_year, death_year,
                        gender, country_code, wikidata_qid, primary_source, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    RETURNING id
                    """,
                    (
                        rec.display_name,
                        sort_name,
                        birth_year,
                        death_year,
                        gender,
                        country_code,
                        rec.wikidata_qid,
                        source_type,
                    ),
                )
                speaker_id = int(cur.fetchone()["id"])
                speakers_inserted += 1
                if rec.wikidata_qid is not None:
                    new_with_qid += 1
                else:
                    new_without_qid += 1

                row_snapshot = {
                    "id": speaker_id,
                    "display_name": rec.display_name,
                    "sort_name": sort_name,
                    "birth_year": birth_year,
                    "death_year": death_year,
                    "gender": gender,
                    "country_code": country_code,
                    "wikidata_qid": rec.wikidata_qid,
                    "primary_source": source_type,
                    "is_verified": False,
                }
                speakers_by_id[speaker_id] = row_snapshot
                if rec.wikidata_qid is not None:
                    speakers_by_qid[rec.wikidata_qid] = row_snapshot

            owner_id = ext_owner.get((provider, norm_key))
            if owner_id is not None and owner_id != speaker_id:
                LOGGER.warning(
                    "Skipping conflicting %s sitelink %s for speaker_id=%s (owned by %s)",
                    provider,
                    canonical_ext_id,
                    speaker_id,
                    owner_id,
                )
            elif owner_id is None:
                cur.execute(
                    """
                    INSERT INTO public.speaker_external_ids (
                        speaker_id, provider, external_id, source, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, NOW())
                    """,
                    (speaker_id, provider, canonical_ext_id, source_type),
                )
                ext_owner[(provider, norm_key)] = speaker_id
                ext_ids_inserted += 1

            revision_url = f"https://{rec.wiki_lang}.wikipedia.org/w/index.php?oldid={rec.revision_id}"
            attribution_text = (
                f'Contributors to Wikimedia projects ({rec.wiki_lang}.wikipedia.org, '
                f'"{rec.page_title}", rev {rec.revision_id})'
            )
            cur.execute(
                """
                INSERT INTO public.provenance_records (
                    entity_table, entity_id, field_name, source_type, source_lang,
                    source_license, source_url, source_revision_id, attribution_text, etl_run_id
                )
                VALUES (
                    'speakers', %s, NULL, %s, %s,
                    'CC-BY-SA-4.0', %s, %s, %s, %s
                )
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    speaker_id,
                    source_type,
                    rec.wiki_lang,
                    revision_url,
                    str(rec.revision_id),
                    attribution_text,
                    run_id,
                ),
            )
            if cur.fetchone() is not None:
                provenance_inserted += 1

    return {
        "speakers_inserted": speakers_inserted,
        "speakers_updated": speakers_updated,
        "new_with_qid": new_with_qid,
        "new_without_qid": new_without_qid,
        "ext_ids_inserted": ext_ids_inserted,
        "provenance_inserted": provenance_inserted,
        "skipped_unconfirmed": skipped_unconfirmed,
    }
