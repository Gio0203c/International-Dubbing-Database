"""Transform and idempotent PostgreSQL load logic for Wikidata dubbing entities."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import psycopg

from etl.wikidata.transform import (
    ISO_LANG_RE,
    MAX_PG_INT32,
    clean_text,
    is_blank_node,
    parse_country_code,
    parse_gender,
    parse_native_lang,
    parse_qid,
    parse_year,
    split_multi,
)

LOGGER = logging.getLogger(__name__)

IMDB_TCONST_RE = re.compile(r"^tt[0-9]+$")
HTTP_URL_RE = re.compile(r"^https?://\S+$")

# Non-audiovisual work types excluded when no audiovisual P31 or media external ID exists.
PRINT_ONLY_P31_QIDS = frozenset(
    {
        "Q8274",      # manga
        "Q21198342",  # manga series
        "Q747463",    # light novel
        "Q8261",      # novel
        "Q571",       # book
        "Q7725634",   # literary work
        "Q1004",      # comics
        "Q1760610",   # comic book
        "Q2831984",   # comic book series
        "Q14406742",  # comic series
        "Q47461344",  # written work
    }
)

ANIMATED_P31_QIDS = frozenset(
    {
        "Q11425",     # animation
        "Q1107",      # anime
        "Q202866",    # animated film
        "Q20650540",  # anime film
        "Q581714",    # animated television series
        "Q63952888",  # anime television series
        "Q17517379",  # animated short film
        "Q220898",    # original video animation
        "Q1047299",   # original net animation
    }
)

AUDIOVISUAL_P31_QIDS = ANIMATED_P31_QIDS | frozenset(
    {
        "Q7889",      # video game
        "Q106833",    # audiobook
        "Q1259759",   # miniseries
        "Q24862",     # short film
        "Q93204",     # documentary film
        "Q1361758",   # documentary television series
        "Q526877",    # web series
        "Q11424",     # film
        "Q24869",     # feature film
        "Q506240",    # television film
        "Q5398426",   # television series
        "Q15416",     # television program
    }
)


@dataclass(frozen=True)
class StudioRecord:
    """Normalized Wikidata dubbing studio record with alias and lineage QIDs."""

    wikidata_qid: str
    name: str
    status: str
    country_code: str | None
    city: str | None
    founded_year: int | None
    closed_year: int | None
    website_url: str | None
    alt_labels: tuple[str, ...]
    replaced_by_qids: tuple[str, ...]
    followed_by_qids: tuple[str, ...]
    parent_org_qids: tuple[str, ...]
    replaces_qids: tuple[str, ...] = ()
    follows_qids: tuple[str, ...] = ()
    child_org_qids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaSourceRecord:
    """Normalized Wikidata media work mapped to public.media_sources."""

    wikidata_qid: str
    original_title: str
    italian_title: str | None
    media_type: str
    is_animated: bool | None
    release_year: int | None
    end_year: int | None
    original_lang: str | None
    country_code: str | None
    imdb_tconst: str | None
    tmdb_media_type: str | None
    tmdb_id: int | None
    anilist_media_id: int | None


@dataclass(frozen=True)
class CharacterRecord:
    """Normalized Wikidata character mapped to public.characters."""

    wikidata_qid: str
    name: str
    work_qids: tuple[str, ...]
    gender: str | None
    anilist_character_id: int | None


@dataclass(frozen=True)
class VoiceCreditRecord:
    """Normalized P725 voice credit mapped to dubbing_editions + dubbing_credits."""

    work_qid: str
    speaker_qid: str
    character_qid: str | None
    lang_code: str
    credited_as: str | None


def _binding_value(binding: dict[str, Any], key: str) -> str | None:
    cell = binding.get(key)
    if not isinstance(cell, dict):
        return None
    raw = cell.get("value")
    return raw if isinstance(raw, str) else None


def _clamp_year(raw_dates: str | None) -> int | None:
    year = parse_year(raw_dates)
    if year is None or not (1880 <= year <= 2100):
        return None
    return year


def _first_clean(*values: str | None) -> str | None:
    for raw in values:
        if raw is None or is_blank_node(raw):
            continue
        cleaned = clean_text(raw)
        if cleaned is not None:
            return cleaned
    return None


def _parse_positive_int(raw: str | None) -> int | None:
    values: list[int] = []
    for token in split_multi(raw):
        cleaned = clean_text(token)
        if cleaned is None:
            continue
        try:
            value = int(cleaned)
        except ValueError:
            continue
        if 1 <= value <= MAX_PG_INT32:
            values.append(value)
    return min(values) if values else None


def _parse_qids(raw: str | None) -> tuple[str, ...]:
    seen: set[str] = set()
    for token in split_multi(raw):
        qid = parse_qid(token)
        if qid is not None:
            seen.add(qid)
    return tuple(sorted(seen, key=lambda q: int(q[1:])))


def _parse_imdb_tconst(raw: str | None) -> str | None:
    values: list[str] = []
    for token in split_multi(raw):
        cleaned = clean_text(token)
        if cleaned is not None and IMDB_TCONST_RE.fullmatch(cleaned):
            values.append(cleaned)
    return min(values, key=lambda t: (int(t[2:]), t)) if values else None


def _parse_website_url(raw: str | None) -> str | None:
    cleaned = clean_text(raw)
    if cleaned is not None and HTTP_URL_RE.fullmatch(cleaned):
        return cleaned
    return None


def classify_media_type(
    p31_qids: tuple[str, ...],
    *,
    has_tmdb_movie: bool = False,
    has_tmdb_tv: bool = False,
    has_imdb: bool = False,
    has_anilist: bool = False,
) -> tuple[str, bool | None] | None:
    """Classify a Wikidata work into (media_type, is_animated), or None to skip."""
    p31_set = set(p31_qids)
    is_print = bool(p31_set & PRINT_ONLY_P31_QIDS)
    has_audiovisual = bool(p31_set & AUDIOVISUAL_P31_QIDS)
    if is_print and not has_audiovisual and not (has_tmdb_movie or has_tmdb_tv or has_imdb or has_anilist):
        return None

    animated_hint = True if (p31_set & ANIMATED_P31_QIDS) else None

    if p31_set & {"Q220898", "Q1047299"}:
        return ("ova", True)
    if "Q7889" in p31_set:
        return ("video_game", None)
    if "Q106833" in p31_set:
        return ("audiobook", None)
    if p31_set & {"Q202866", "Q20650540"}:
        return ("movie", True)
    if p31_set & {"Q63952888", "Q581714"}:
        return ("tv_series", True)
    if "Q1259759" in p31_set:
        return ("miniseries", animated_hint)
    if "Q17517379" in p31_set:
        return ("short", True)
    if "Q24862" in p31_set:
        return ("short", animated_hint)
    if p31_set & {"Q93204", "Q1361758"}:
        return ("documentary", animated_hint)
    if "Q526877" in p31_set:
        return ("web_series", animated_hint)
    if p31_set & {"Q11424", "Q24869", "Q506240"}:
        return ("movie", animated_hint)
    if p31_set & {"Q5398426", "Q15416", "Q1107"}:
        return ("tv_series", True if (animated_hint or has_anilist) else None)
    if "Q11425" in p31_set:
        return ("other", True)
    if has_tmdb_tv or has_anilist:
        return ("tv_series", True if has_anilist else None)
    if has_tmdb_movie:
        return ("movie", None)
    return ("other", animated_hint)


def transform_studio_bindings(bindings: list[dict[str, Any]]) -> tuple[list[StudioRecord], int]:
    """Transform WDQS studio bindings into deduplicated StudioRecord objects."""
    by_qid: dict[str, StudioRecord] = {}
    discarded = 0

    for row in bindings:
        qid = parse_qid(_binding_value(row, "studio"))
        name = _first_clean(
            _binding_value(row, "nameIt"),
            _binding_value(row, "nameEn"),
            _binding_value(row, "nameAny"),
            _binding_value(row, "name"),
        )
        if qid is None or name is None:
            discarded += 1
            continue

        founded_year = _clamp_year(_binding_value(row, "inceptionDates"))
        closed_year = _clamp_year(_binding_value(row, "dissolvedDates"))
        if founded_year is not None and closed_year is not None and closed_year < founded_year:
            closed_year = None

        replaced_by = _parse_qids(_binding_value(row, "replacedByQids"))
        followed_by = _parse_qids(_binding_value(row, "followedByQids"))
        parent_orgs = _parse_qids(_binding_value(row, "parentOrgQids"))
        replaces_qids = _parse_qids(_binding_value(row, "replacesQids"))
        follows_qids = _parse_qids(_binding_value(row, "followsQids"))
        child_org_qids = _parse_qids(_binding_value(row, "childOrgQids"))

        if replaced_by:
            status = "merged"
        elif closed_year is not None:
            status = "closed"
        else:
            status = "active"

        if status not in ("closed", "merged"):
            closed_year = None

        city = _first_clean(
            _binding_value(row, "cityIt"),
            _binding_value(row, "cityEn"),
            _binding_value(row, "city"),
        )

        alt_seen: set[str] = {name.lower()}
        alt_list: list[str] = []
        for token in split_multi(_binding_value(row, "altLabels")):
            if is_blank_node(token):
                continue
            cleaned_alt = clean_text(token)
            if cleaned_alt is None:
                continue
            key = cleaned_alt.lower()
            if key not in alt_seen:
                alt_seen.add(key)
                alt_list.append(cleaned_alt)

        by_qid[qid] = StudioRecord(
            wikidata_qid=qid,
            name=name,
            status=status,
            country_code=parse_country_code(_binding_value(row, "countryCodes")),
            city=city,
            founded_year=founded_year,
            closed_year=closed_year,
            website_url=_parse_website_url(_binding_value(row, "websiteUrl")),
            alt_labels=tuple(alt_list),
            replaced_by_qids=replaced_by,
            followed_by_qids=followed_by,
            parent_org_qids=parent_orgs,
            replaces_qids=replaces_qids,
            follows_qids=follows_qids,
            child_org_qids=child_org_qids,
        )

    return [by_qid[qid] for qid in sorted(by_qid)], discarded


def transform_work_bindings(bindings: list[dict[str, Any]]) -> tuple[list[MediaSourceRecord], int]:
    """Transform WDQS media work bindings, skipping blank names and print-only entries."""
    by_qid: dict[str, MediaSourceRecord] = {}
    discarded = 0

    for row in bindings:
        qid = parse_qid(_binding_value(row, "work"))
        italian_title = _first_clean(_binding_value(row, "italianTitle"))
        original_title = _first_clean(
            _binding_value(row, "titleProp"),
            _binding_value(row, "labelEn"),
            _binding_value(row, "italianTitle"),
            _binding_value(row, "originalTitle"),
        )
        if qid is None or original_title is None:
            discarded += 1
            continue

        p31_qids = _parse_qids(_binding_value(row, "p31Qids"))
        imdb_tconst = _parse_imdb_tconst(_binding_value(row, "imdbIds"))
        tmdb_movie_id = _parse_positive_int(_binding_value(row, "tmdbMovieIds"))
        tmdb_tv_id = _parse_positive_int(_binding_value(row, "tmdbTvIds"))
        anilist_media_id = _parse_positive_int(_binding_value(row, "anilistIds"))

        classified = classify_media_type(
            p31_qids,
            has_tmdb_movie=tmdb_movie_id is not None,
            has_tmdb_tv=tmdb_tv_id is not None,
            has_imdb=imdb_tconst is not None,
            has_anilist=anilist_media_id is not None,
        )
        if classified is None:
            discarded += 1
            continue
        media_type, is_animated = classified

        tmdb_media_type: str | None = None
        tmdb_id: int | None = None
        if media_type in ("tv_series", "miniseries", "ova", "web_series") and tmdb_tv_id is not None:
            tmdb_media_type, tmdb_id = "tv", tmdb_tv_id
        elif tmdb_movie_id is not None:
            tmdb_media_type, tmdb_id = "movie", tmdb_movie_id
        elif tmdb_tv_id is not None:
            tmdb_media_type, tmdb_id = "tv", tmdb_tv_id

        release_year = _clamp_year(_binding_value(row, "releaseDates"))
        end_year = _clamp_year(_binding_value(row, "endDates"))
        if release_year is not None and end_year is not None and end_year < release_year:
            end_year = None

        by_qid[qid] = MediaSourceRecord(
            wikidata_qid=qid,
            original_title=original_title,
            italian_title=italian_title,
            media_type=media_type,
            is_animated=is_animated,
            release_year=release_year,
            end_year=end_year,
            original_lang=parse_native_lang(_binding_value(row, "originalLangs")),
            country_code=parse_country_code(_binding_value(row, "countryCodes")),
            imdb_tconst=imdb_tconst,
            tmdb_media_type=tmdb_media_type,
            tmdb_id=tmdb_id,
            anilist_media_id=anilist_media_id,
        )

    return [by_qid[qid] for qid in sorted(by_qid)], discarded


def transform_character_bindings(bindings: list[dict[str, Any]]) -> tuple[list[CharacterRecord], int]:
    """Transform WDQS character bindings into deduplicated CharacterRecord objects."""
    by_qid: dict[str, CharacterRecord] = {}
    discarded = 0

    for row in bindings:
        qid = parse_qid(_binding_value(row, "character"))
        if qid is None:
            discarded += 1
            continue
        p31_qids = _parse_qids(_binding_value(row, "p31Qids"))
        if set(p31_qids) & AUDIOVISUAL_P31_QIDS:
            discarded += 1
            continue
        name = _first_clean(
            _binding_value(row, "nameIt"),
            _binding_value(row, "nameEn"),
            _binding_value(row, "name"),
        )
        if name is None:
            discarded += 1
            continue

        parsed_gender = parse_gender(_binding_value(row, "genderQids"))
        gender = None if parsed_gender == "unknown" else parsed_gender

        by_qid[qid] = CharacterRecord(
            wikidata_qid=qid,
            name=name,
            work_qids=_parse_qids(_binding_value(row, "workQids")),
            gender=gender,
            anilist_character_id=_parse_positive_int(_binding_value(row, "anilistCharIds")),
        )

    return [by_qid[qid] for qid in sorted(by_qid)], discarded


def _pick_credit_lang_code(raw_langs: str | None) -> str:
    candidates: list[str] = []
    for token in split_multi(raw_langs):
        cleaned = clean_text(token)
        if cleaned is not None and ISO_LANG_RE.fullmatch(cleaned.lower()):
            candidates.append(cleaned.lower())
    if "it" in candidates:
        return "it"
    return min(candidates) if candidates else "it"


def transform_credit_bindings(bindings: list[dict[str, Any]]) -> tuple[list[VoiceCreditRecord], int]:
    """Transform WDQS P725 bindings into deduplicated VoiceCreditRecord objects."""
    by_key: dict[tuple[str, str, str | None, str], VoiceCreditRecord] = {}
    discarded = 0

    for row in bindings:
        work_qid = parse_qid(_binding_value(row, "work"))
        speaker_qid = parse_qid(_binding_value(row, "speaker"))
        if work_qid is None or speaker_qid is None or work_qid == speaker_qid:
            discarded += 1
            continue

        character_qid = parse_qid(_binding_value(row, "character"))
        lang_code = _pick_credit_lang_code(_binding_value(row, "langCodes"))
        raw_credited = _binding_value(row, "creditedAs")
        credited_as = None if is_blank_node(raw_credited) else clean_text(raw_credited)

        key = (work_qid, speaker_qid, character_qid, lang_code)
        existing = by_key.get(key)
        if existing is None or (existing.credited_as is None and credited_as is not None):
            by_key[key] = VoiceCreditRecord(
                work_qid=work_qid,
                speaker_qid=speaker_qid,
                character_qid=character_qid,
                lang_code=lang_code,
                credited_as=credited_as,
            )

    return [by_key[k] for k in sorted(by_key, key=lambda item: (item[0], item[1], item[2] or "", item[3]))], discarded


def load_studios(conn: psycopg.Connection[dict[str, Any]], records: list[StudioRecord]) -> dict[str, int]:
    """Idempotently upsert studios, studio_aliases, and studio_relationships."""
    inserted = 0
    updated = 0
    aliases_inserted = 0
    aliases_updated = 0
    rels_inserted = 0
    rels_updated = 0

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, wikidata_qid, name, status, country_code, city,
                   founded_year, closed_year, website_url, primary_source, is_verified
            FROM public.studios
            WHERE wikidata_qid IS NOT NULL
            """
        )
        existing_studios: dict[str, dict[str, Any]] = {
            row["wikidata_qid"]: row for row in cur.fetchall()
        }

        for rec in records:
            current = existing_studios.get(rec.wikidata_qid)
            if current is None:
                cur.execute(
                    """
                    INSERT INTO public.studios (
                        wikidata_qid, name, status, country_code, city,
                        founded_year, closed_year, website_url, primary_source, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'wikidata', NOW())
                    """,
                    (
                        rec.wikidata_qid,
                        rec.name,
                        rec.status,
                        rec.country_code,
                        rec.city,
                        rec.founded_year,
                        rec.closed_year,
                        rec.website_url,
                    ),
                )
                inserted += 1
            elif current["is_verified"]:
                cur.execute(
                    "UPDATE public.studios SET last_seen_at = NOW() WHERE id = %s",
                    (current["id"],),
                )
            else:
                # Preserve existing Wikipedia-backfilled attributes when Wikidata omits them.
                merged_country = (
                    rec.country_code if rec.country_code is not None else current["country_code"]
                )
                merged_city = rec.city if rec.city is not None else current["city"]
                merged_founded = (
                    rec.founded_year if rec.founded_year is not None else current["founded_year"]
                )
                merged_website = (
                    rec.website_url if rec.website_url is not None else current["website_url"]
                )
                merged_closed = (
                    rec.closed_year if rec.closed_year is not None else current["closed_year"]
                )
                if (
                    merged_founded is not None
                    and merged_closed is not None
                    and merged_closed < merged_founded
                ):
                    merged_closed = None

                if rec.status in ("closed", "merged"):
                    merged_status = rec.status
                elif current["status"] in ("closed", "merged") and merged_closed is not None:
                    merged_status = current["status"]
                else:
                    merged_status = rec.status

                if merged_status not in ("closed", "merged"):
                    merged_closed = None

                changed = (
                    current["name"],
                    current["status"],
                    current["country_code"],
                    current["city"],
                    current["founded_year"],
                    current["closed_year"],
                    current["website_url"],
                    current["primary_source"],
                ) != (
                    rec.name,
                    merged_status,
                    merged_country,
                    merged_city,
                    merged_founded,
                    merged_closed,
                    merged_website,
                    "wikidata",
                )
                if changed:
                    cur.execute(
                        """
                        UPDATE public.studios
                        SET name = %s, status = %s, country_code = %s, city = %s,
                            founded_year = %s, closed_year = %s, website_url = %s,
                            primary_source = 'wikidata', last_seen_at = NOW(), updated_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            rec.name,
                            merged_status,
                            merged_country,
                            merged_city,
                            merged_founded,
                            merged_closed,
                            merged_website,
                            current["id"],
                        ),
                    )
                    updated += 1
                else:
                    cur.execute(
                        "UPDATE public.studios SET last_seen_at = NOW() WHERE id = %s",
                        (current["id"],),
                    )

        cur.execute("SELECT id, wikidata_qid FROM public.studios WHERE wikidata_qid IS NOT NULL")
        studio_id_by_qid: dict[str, int] = {row["wikidata_qid"]: row["id"] for row in cur.fetchall()}

        cur.execute(
            """
            SELECT id, studio_id, alias_name, alias_type, end_year, source, is_verified
            FROM public.studio_aliases
            WHERE start_year IS NULL
            """
        )
        existing_aliases: dict[tuple[int, str], dict[str, Any]] = {
            (row["studio_id"], row["alias_name"].strip().lower()): row for row in cur.fetchall()
        }

        for rec in records:
            studio_id = studio_id_by_qid[rec.wikidata_qid]
            for alias_name in rec.alt_labels:
                key = (studio_id, alias_name.strip().lower())
                current_alias = existing_aliases.get(key)
                if current_alias is None:
                    cur.execute(
                        """
                        INSERT INTO public.studio_aliases (
                            studio_id, alias_name, alias_type, start_year, end_year, source, last_seen_at
                        )
                        VALUES (%s, %s, 'other', NULL, NULL, 'wikidata', NOW())
                        """,
                        (studio_id, alias_name),
                    )
                    aliases_inserted += 1
                    existing_aliases[key] = {
                        "id": -1,
                        "studio_id": studio_id,
                        "alias_name": alias_name,
                        "alias_type": "other",
                        "end_year": None,
                        "source": "wikidata",
                        "is_verified": False,
                    }
                elif current_alias["is_verified"] or current_alias["alias_type"] != "other":
                    cur.execute(
                        "UPDATE public.studio_aliases SET last_seen_at = NOW() WHERE id = %s",
                        (current_alias["id"],),
                    )
                elif (
                    current_alias["alias_name"],
                    current_alias["alias_type"],
                    current_alias["source"],
                ) != (alias_name, "other", "wikidata") and current_alias["source"] == "wikidata":
                    cur.execute(
                        """
                        UPDATE public.studio_aliases
                        SET alias_name = %s, alias_type = 'other', source = 'wikidata',
                            last_seen_at = NOW(), updated_at = NOW()
                        WHERE id = %s
                        """,
                        (alias_name, current_alias["id"]),
                    )
                    aliases_updated += 1
                else:
                    cur.execute(
                        "UPDATE public.studio_aliases SET last_seen_at = NOW() WHERE id = %s",
                        (current_alias["id"],),
                    )

        cur.execute(
            """
            SELECT id, studio_id, related_studio_id, relationship_type, event_year, source, is_verified
            FROM public.studio_relationships
            """
        )
        existing_rels: dict[tuple[int, int, str], dict[str, Any]] = {
            (row["studio_id"], row["related_studio_id"], row["relationship_type"]): row
            for row in cur.fetchall()
        }

        rel_candidates: list[tuple[int, int, str, int | None]] = []
        seen_rel_keys: set[tuple[int, int, str]] = set()
        for rec in records:
            studio_id = studio_id_by_qid[rec.wikidata_qid]
            edge_groups = (
                ("merged_into", rec.replaced_by_qids, rec.closed_year),
                ("succeeded_by", rec.followed_by_qids, rec.closed_year),
                ("subsidiary_of", rec.parent_org_qids, None),
            )
            for rel_type, targets, event_year in edge_groups:
                for target_qid in targets:
                    related_id = studio_id_by_qid.get(target_qid)
                    if related_id is None or related_id == studio_id:
                        continue
                    rel_key = (studio_id, related_id, rel_type)
                    if rel_key in seen_rel_keys:
                        continue
                    seen_rel_keys.add(rel_key)
                    rel_candidates.append((studio_id, related_id, rel_type, event_year))

        for rec in records:
            studio_id = studio_id_by_qid[rec.wikidata_qid]
            inverse_groups = (
                ("merged_into", rec.replaces_qids, rec.founded_year),
                ("succeeded_by", rec.follows_qids, rec.founded_year),
                ("subsidiary_of", rec.child_org_qids, None),
            )
            for rel_type, sources, event_year in inverse_groups:
                for source_qid in sources:
                    source_id = studio_id_by_qid.get(source_qid)
                    if source_id is None or source_id == studio_id:
                        continue
                    rel_key = (source_id, studio_id, rel_type)
                    if rel_key in seen_rel_keys:
                        continue
                    seen_rel_keys.add(rel_key)
                    rel_candidates.append((source_id, studio_id, rel_type, event_year))

        for studio_id, related_id, rel_type, event_year in rel_candidates:
            rel_key = (studio_id, related_id, rel_type)
            current_rel = existing_rels.get(rel_key)
            if current_rel is None:
                cur.execute(
                    """
                    INSERT INTO public.studio_relationships (
                        studio_id, related_studio_id, relationship_type, event_year, source, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, 'wikidata', NOW())
                    """,
                    (studio_id, related_id, rel_type, event_year),
                )
                rels_inserted += 1
            elif current_rel["is_verified"]:
                cur.execute(
                    "UPDATE public.studio_relationships SET last_seen_at = NOW() WHERE id = %s",
                    (current_rel["id"],),
                )
            else:
                merged_event_year = (
                    event_year if event_year is not None else current_rel["event_year"]
                )
                if merged_event_year != current_rel["event_year"] or (
                    current_rel["source"] == "wikidata"
                    and current_rel["event_year"] != merged_event_year
                ):
                    cur.execute(
                        """
                        UPDATE public.studio_relationships
                        SET event_year = %s, source = 'wikidata', last_seen_at = NOW(), updated_at = NOW()
                        WHERE id = %s
                        """,
                        (merged_event_year, current_rel["id"]),
                    )
                    rels_updated += 1
                else:
                    cur.execute(
                        "UPDATE public.studio_relationships SET last_seen_at = NOW() WHERE id = %s",
                        (current_rel["id"],),
                    )

    return {
        "inserted": inserted,
        "updated": updated,
        "aliases_inserted": aliases_inserted,
        "aliases_updated": aliases_updated,
        "relationships_inserted": rels_inserted,
        "relationships_updated": rels_updated,
    }


def load_media_sources(
    conn: psycopg.Connection[dict[str, Any]],
    records: list[MediaSourceRecord],
) -> dict[str, int]:
    """Idempotently upsert media_sources while protecting external ID uniqueness across QIDs."""
    inserted = 0
    updated = 0
    ext_conflicts = 0

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, wikidata_qid, original_title, media_type, is_animated,
                   release_year, end_year, original_lang, country_code,
                   imdb_tconst, tmdb_media_type, tmdb_id, anilist_media_id,
                   primary_source, is_verified
            FROM public.media_sources
            WHERE wikidata_qid IS NOT NULL
               OR imdb_tconst IS NOT NULL
               OR tmdb_id IS NOT NULL
               OR anilist_media_id IS NOT NULL
            """
        )
        existing_by_qid: dict[str, dict[str, Any]] = {}
        imdb_owner: dict[str, str] = {}
        tmdb_owner: dict[tuple[str, int], str] = {}
        anilist_owner: dict[int, str] = {}

        for row in cur.fetchall():
            qid = row["wikidata_qid"]
            if qid is not None:
                existing_by_qid[qid] = row
            owner = qid or f"row:{row['id']}"
            if row["imdb_tconst"] is not None:
                imdb_owner[row["imdb_tconst"]] = owner
            if row["tmdb_media_type"] is not None and row["tmdb_id"] is not None:
                tmdb_owner[(row["tmdb_media_type"], row["tmdb_id"])] = owner
            if row["anilist_media_id"] is not None:
                anilist_owner[row["anilist_media_id"]] = owner

        for rec in records:
            imdb_tconst = rec.imdb_tconst
            if imdb_tconst is not None:
                owner = imdb_owner.get(imdb_tconst)
                if owner is not None and owner != rec.wikidata_qid:
                    LOGGER.warning("Skipping conflicting imdb_tconst=%s on %s (owned by %s)", imdb_tconst, rec.wikidata_qid, owner)
                    imdb_tconst = None
                    ext_conflicts += 1

            tmdb_media_type, tmdb_id = rec.tmdb_media_type, rec.tmdb_id
            if tmdb_media_type is not None and tmdb_id is not None:
                owner = tmdb_owner.get((tmdb_media_type, tmdb_id))
                if owner is not None and owner != rec.wikidata_qid:
                    LOGGER.warning("Skipping conflicting tmdb=(%s,%s) on %s (owned by %s)", tmdb_media_type, tmdb_id, rec.wikidata_qid, owner)
                    tmdb_media_type, tmdb_id = None, None
                    ext_conflicts += 1

            anilist_id = rec.anilist_media_id
            if anilist_id is not None:
                owner = anilist_owner.get(anilist_id)
                if owner is not None and owner != rec.wikidata_qid:
                    LOGGER.warning("Skipping conflicting anilist_media_id=%s on %s (owned by %s)", anilist_id, rec.wikidata_qid, owner)
                    anilist_id = None
                    ext_conflicts += 1

            current = existing_by_qid.get(rec.wikidata_qid)
            if current is None:
                cur.execute(
                    """
                    INSERT INTO public.media_sources (
                        wikidata_qid, original_title, media_type, is_animated,
                        release_year, end_year, original_lang, country_code,
                        imdb_tconst, tmdb_media_type, tmdb_id, anilist_media_id,
                        primary_source, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'wikidata', NOW())
                    """,
                    (
                        rec.wikidata_qid,
                        rec.original_title,
                        rec.media_type,
                        rec.is_animated,
                        rec.release_year,
                        rec.end_year,
                        rec.original_lang,
                        rec.country_code,
                        imdb_tconst,
                        tmdb_media_type,
                        tmdb_id,
                        anilist_id,
                    ),
                )
                inserted += 1
            elif current["is_verified"]:
                cur.execute(
                    "UPDATE public.media_sources SET last_seen_at = NOW() WHERE id = %s",
                    (current["id"],),
                )
                # Preserve existing verified external IDs if any changed externally
                imdb_tconst = current["imdb_tconst"]
                tmdb_media_type, tmdb_id = current["tmdb_media_type"], current["tmdb_id"]
                anilist_id = current["anilist_media_id"]
            else:
                changed = (
                    current["original_title"],
                    current["media_type"],
                    current["is_animated"],
                    current["release_year"],
                    current["end_year"],
                    current["original_lang"],
                    current["country_code"],
                    current["imdb_tconst"],
                    current["tmdb_media_type"],
                    current["tmdb_id"],
                    current["anilist_media_id"],
                    current["primary_source"],
                ) != (
                    rec.original_title,
                    rec.media_type,
                    rec.is_animated,
                    rec.release_year,
                    rec.end_year,
                    rec.original_lang,
                    rec.country_code,
                    imdb_tconst,
                    tmdb_media_type,
                    tmdb_id,
                    anilist_id,
                    "wikidata",
                )
                if changed:
                    cur.execute(
                        """
                        UPDATE public.media_sources
                        SET original_title = %s, media_type = %s, is_animated = %s,
                            release_year = %s, end_year = %s, original_lang = %s,
                            country_code = %s, imdb_tconst = %s, tmdb_media_type = %s,
                            tmdb_id = %s, anilist_media_id = %s, primary_source = 'wikidata',
                            last_seen_at = NOW(), updated_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            rec.original_title,
                            rec.media_type,
                            rec.is_animated,
                            rec.release_year,
                            rec.end_year,
                            rec.original_lang,
                            rec.country_code,
                            imdb_tconst,
                            tmdb_media_type,
                            tmdb_id,
                            anilist_id,
                            current["id"],
                        ),
                    )
                    updated += 1
                else:
                    cur.execute(
                        "UPDATE public.media_sources SET last_seen_at = NOW() WHERE id = %s",
                        (current["id"],),
                    )

            # Refresh claim tracking so same-batch updates stay collision-free.
            if current is not None and current["imdb_tconst"] and imdb_tconst != current["imdb_tconst"]:
                imdb_owner.pop(current["imdb_tconst"], None)
            if imdb_tconst is not None:
                imdb_owner[imdb_tconst] = rec.wikidata_qid

            if current is not None and current["tmdb_id"] and (tmdb_media_type, tmdb_id) != (current["tmdb_media_type"], current["tmdb_id"]):
                tmdb_owner.pop((current["tmdb_media_type"], current["tmdb_id"]), None)
            if tmdb_media_type is not None and tmdb_id is not None:
                tmdb_owner[(tmdb_media_type, tmdb_id)] = rec.wikidata_qid

            if current is not None and current["anilist_media_id"] and anilist_id != current["anilist_media_id"]:
                anilist_owner.pop(current["anilist_media_id"], None)
            if anilist_id is not None:
                anilist_owner[anilist_id] = rec.wikidata_qid

    return {"inserted": inserted, "updated": updated, "ext_conflicts": ext_conflicts}


def load_characters(
    conn: psycopg.Connection[dict[str, Any]],
    records: list[CharacterRecord],
) -> dict[str, int]:
    """Idempotently upsert characters and link media_source_id when loaded."""
    inserted = 0
    updated = 0
    ext_conflicts = 0

    with conn.cursor() as cur:
        cur.execute("SELECT id, wikidata_qid FROM public.media_sources WHERE wikidata_qid IS NOT NULL")
        media_id_by_qid: dict[str, int] = {row["wikidata_qid"]: row["id"] for row in cur.fetchall()}

        cur.execute(
            """
            SELECT id, wikidata_qid, name, media_source_id, gender,
                   anilist_character_id, primary_source, is_verified
            FROM public.characters
            WHERE wikidata_qid IS NOT NULL OR anilist_character_id IS NOT NULL
            """
        )
        existing_by_qid: dict[str, dict[str, Any]] = {}
        anilist_owner: dict[int, str] = {}
        for row in cur.fetchall():
            qid = row["wikidata_qid"]
            if qid is not None:
                existing_by_qid[qid] = row
            if row["anilist_character_id"] is not None:
                anilist_owner[row["anilist_character_id"]] = qid or f"row:{row['id']}"

        for rec in records:
            media_source_id = next(
                (media_id_by_qid[wq] for wq in rec.work_qids if wq in media_id_by_qid),
                None,
            )

            anilist_char_id = rec.anilist_character_id
            if anilist_char_id is not None:
                owner = anilist_owner.get(anilist_char_id)
                if owner is not None and owner != rec.wikidata_qid:
                    LOGGER.warning(
                        "Skipping conflicting anilist_character_id=%s on %s (owned by %s)",
                        anilist_char_id,
                        rec.wikidata_qid,
                        owner,
                    )
                    anilist_char_id = None
                    ext_conflicts += 1

            current = existing_by_qid.get(rec.wikidata_qid)
            if current is None:
                cur.execute(
                    """
                    INSERT INTO public.characters (
                        wikidata_qid, name, media_source_id, gender,
                        anilist_character_id, primary_source, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 'wikidata', NOW())
                    """,
                    (
                        rec.wikidata_qid,
                        rec.name,
                        media_source_id,
                        rec.gender,
                        anilist_char_id,
                    ),
                )
                inserted += 1
            elif current["is_verified"]:
                cur.execute(
                    "UPDATE public.characters SET last_seen_at = NOW() WHERE id = %s",
                    (current["id"],),
                )
                anilist_char_id = current["anilist_character_id"]
            else:
                changed = (
                    current["name"],
                    current["media_source_id"],
                    current["gender"],
                    current["anilist_character_id"],
                    current["primary_source"],
                ) != (
                    rec.name,
                    media_source_id,
                    rec.gender,
                    anilist_char_id,
                    "wikidata",
                )
                if changed:
                    cur.execute(
                        """
                        UPDATE public.characters
                        SET name = %s, media_source_id = %s, gender = %s,
                            anilist_character_id = %s, primary_source = 'wikidata',
                            last_seen_at = NOW(), updated_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            rec.name,
                            media_source_id,
                            rec.gender,
                            anilist_char_id,
                            current["id"],
                        ),
                    )
                    updated += 1
                else:
                    cur.execute(
                        "UPDATE public.characters SET last_seen_at = NOW() WHERE id = %s",
                        (current["id"],),
                    )

            if current is not None and current["anilist_character_id"] and anilist_char_id != current["anilist_character_id"]:
                anilist_owner.pop(current["anilist_character_id"], None)
            if anilist_char_id is not None:
                anilist_owner[anilist_char_id] = rec.wikidata_qid

    return {"inserted": inserted, "updated": updated, "ext_conflicts": ext_conflicts}


def load_dubbing_credits(
    conn: psycopg.Connection[dict[str, Any]],
    records: list[VoiceCreditRecord],
    *,
    italian_titles_by_qid: dict[str, str] | None = None,
) -> dict[str, int]:
    """Idempotently upsert dubbing_editions and dubbing_credits for resolved works and speakers."""
    editions_inserted = 0
    editions_updated = 0
    credits_inserted = 0
    credits_updated = 0
    unresolved = 0

    titles_map = italian_titles_by_qid or {}

    with conn.cursor() as cur:
        cur.execute("SELECT id, wikidata_qid, original_lang FROM public.media_sources WHERE wikidata_qid IS NOT NULL")
        media_by_qid: dict[str, tuple[int, str | None]] = {
            row["wikidata_qid"]: (row["id"], row["original_lang"]) for row in cur.fetchall()
        }

        cur.execute("SELECT id, wikidata_qid FROM public.speakers WHERE wikidata_qid IS NOT NULL")
        speaker_id_by_qid: dict[str, int] = {row["wikidata_qid"]: row["id"] for row in cur.fetchall()}

        cur.execute("SELECT id, wikidata_qid FROM public.characters WHERE wikidata_qid IS NOT NULL")
        character_id_by_qid: dict[str, int] = {row["wikidata_qid"]: row["id"] for row in cur.fetchall()}

        resolved_rows: list[tuple[int, str, str, str | None, int, int | None, str | None]] = []
        edition_specs: dict[tuple[int, str, str], str | None] = {}

        for rec in records:
            media_info = media_by_qid.get(rec.work_qid)
            speaker_id = speaker_id_by_qid.get(rec.speaker_qid)
            if media_info is None or speaker_id is None:
                unresolved += 1
                continue
            media_id, original_lang = media_info
            character_id = character_id_by_qid.get(rec.character_qid) if rec.character_qid else None
            edition_type = "original" if original_lang == rec.lang_code else "dub"
            localized_title = titles_map.get(rec.work_qid) if rec.lang_code == "it" else None

            ed_key = (media_id, rec.lang_code, edition_type)
            if ed_key not in edition_specs or (edition_specs[ed_key] is None and localized_title is not None):
                edition_specs[ed_key] = localized_title

            resolved_rows.append(
                (
                    media_id,
                    rec.lang_code,
                    edition_type,
                    localized_title,
                    speaker_id,
                    character_id,
                    rec.credited_as,
                )
            )

        cur.execute(
            """
            SELECT id, media_source_id, lang_code, edition_type,
                   localized_title, primary_source, is_verified
            FROM public.dubbing_editions
            WHERE COALESCE(region_code, '') = ''
              AND COALESCE(release_year, 0) = 0
              AND COALESCE(lower(btrim(edition_label)), '') = ''
            """
        )
        existing_editions: dict[tuple[int, str, str], dict[str, Any]] = {
            (row["media_source_id"], row["lang_code"], row["edition_type"]): row
            for row in cur.fetchall()
        }

        edition_id_by_key: dict[tuple[int, str, str], int] = {
            key: row["id"] for key, row in existing_editions.items()
        }

        for ed_key, localized_title in edition_specs.items():
            media_id, lang_code, edition_type = ed_key
            current_ed = existing_editions.get(ed_key)
            if current_ed is None:
                cur.execute(
                    """
                    INSERT INTO public.dubbing_editions (
                        media_source_id, lang_code, edition_type,
                        localized_title, primary_source, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, 'wikidata', NOW())
                    RETURNING id
                    """,
                    (media_id, lang_code, edition_type, localized_title),
                )
                new_id = cur.fetchone()["id"]
                edition_id_by_key[ed_key] = new_id
                editions_inserted += 1
            elif current_ed["is_verified"]:
                cur.execute(
                    "UPDATE public.dubbing_editions SET last_seen_at = NOW() WHERE id = %s",
                    (current_ed["id"],),
                )
            else:
                target_title = localized_title if localized_title is not None else current_ed["localized_title"]
                if (current_ed["localized_title"], current_ed["primary_source"]) != (target_title, "wikidata"):
                    cur.execute(
                        """
                        UPDATE public.dubbing_editions
                        SET localized_title = %s, primary_source = 'wikidata',
                            last_seen_at = NOW(), updated_at = NOW()
                        WHERE id = %s
                        """,
                        (target_title, current_ed["id"]),
                    )
                    editions_updated += 1
                else:
                    cur.execute(
                        "UPDATE public.dubbing_editions SET last_seen_at = NOW() WHERE id = %s",
                        (current_ed["id"],),
                    )

        cur.execute(
            """
            SELECT id, dubbing_edition_id, speaker_id, role_type,
                   COALESCE(character_id, -1) AS char_key,
                   credited_as, source, is_verified
            FROM public.dubbing_credits
            WHERE role_type = 'voice'
              AND original_actor_qid IS NULL
              AND COALESCE(lower(btrim(original_actor_name)), '') = ''
              AND COALESCE(lower(btrim(episode_range)), '') = ''
            """
        )
        existing_credits: dict[tuple[int, int, int], dict[str, Any]] = {
            (row["dubbing_edition_id"], row["speaker_id"], row["char_key"]): row
            for row in cur.fetchall()
        }

        deduped_credits: dict[tuple[int, int, int], tuple[int, int, int | None, str | None]] = {}
        for media_id, lang_code, edition_type, _title, speaker_id, character_id, credited_as in resolved_rows:
            edition_id = edition_id_by_key[(media_id, lang_code, edition_type)]
            char_key = -1 if character_id is None else character_id
            cr_key = (edition_id, speaker_id, char_key)
            existing_batch = deduped_credits.get(cr_key)
            if existing_batch is None or (existing_batch[3] is None and credited_as is not None):
                deduped_credits[cr_key] = (edition_id, speaker_id, character_id, credited_as)

        for cr_key, (edition_id, speaker_id, character_id, credited_as) in deduped_credits.items():
            current_cr = existing_credits.get(cr_key)
            if current_cr is None:
                cur.execute(
                    """
                    INSERT INTO public.dubbing_credits (
                        dubbing_edition_id, speaker_id, character_id,
                        role_type, credited_as, source, last_seen_at
                    )
                    VALUES (%s, %s, %s, 'voice', %s, 'wikidata', NOW())
                    """,
                    (edition_id, speaker_id, character_id, credited_as),
                )
                credits_inserted += 1
            elif current_cr["is_verified"]:
                cur.execute(
                    "UPDATE public.dubbing_credits SET last_seen_at = NOW() WHERE id = %s",
                    (current_cr["id"],),
                )
            else:
                target_credited = credited_as if credited_as is not None else current_cr["credited_as"]
                if (current_cr["credited_as"], current_cr["source"]) != (target_credited, "wikidata"):
                    cur.execute(
                        """
                        UPDATE public.dubbing_credits
                        SET credited_as = %s, source = 'wikidata',
                            last_seen_at = NOW(), updated_at = NOW()
                        WHERE id = %s
                        """,
                        (target_credited, current_cr["id"]),
                    )
                    credits_updated += 1
                else:
                    cur.execute(
                        "UPDATE public.dubbing_credits SET last_seen_at = NOW() WHERE id = %s",
                        (current_cr["id"],),
                    )

    return {
        "editions_inserted": editions_inserted,
        "editions_updated": editions_updated,
        "credits_inserted": credits_inserted,
        "credits_updated": credits_updated,
        "unresolved": unresolved,
    }
