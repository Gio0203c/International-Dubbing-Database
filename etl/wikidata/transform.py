"""Defensive transformation from raw WDQS bindings to public.speakers records."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

QID_RE = re.compile(r"^Q[1-9][0-9]*$")
IMDB_NCONST_RE = re.compile(r"^nm[0-9]+$")
TMDB_INT_RE = re.compile(r"^[1-9][0-9]*$")
ISO_LANG_RE = re.compile(r"^[a-z]{2}$")
ISO_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
ISO_YEAR_RE = re.compile(r"^\+?([0-9]{4})(?:-|$)")

MIN_VALID_YEAR = 1850
MAX_VALID_YEAR = 2100
MAX_PG_INT32 = 2_147_483_647

GENDER_QID_MAP: dict[str, str] = {
    "Q6581072": "female",
    "Q1052281": "female",
    "Q6581097": "male",
    "Q2449503": "male",
    "Q48270": "non_binary",
    "Q18116794": "non_binary",
}


@dataclass(frozen=True)
class SpeakerRecord:
    """Normalized speaker row ready for loading into public.speakers."""

    wikidata_qid: str
    display_name: str
    sort_name: str
    legal_name: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    gender: str | None = None
    native_lang: str | None = None
    country_code: str | None = None
    imdb_nconst: str | None = None
    tmdb_person_id: int | None = None
    primary_source: str = "wikidata"


@dataclass(frozen=True)
class TransformResult:
    """Aggregated outcome of transforming a batch of raw WDQS bindings."""

    records: list[SpeakerRecord]
    discarded_count: int
    duplicate_qid_count: int


def is_blank_node(value: str | None) -> bool:
    """Return True for empty values or Wikidata unknown-value blank nodes."""
    if not value:
        return True
    cleaned = value.strip()
    if not cleaned:
        return True
    if cleaned.startswith("_:") or "/.well-known/genid/" in cleaned:
        return True
    return False


def _binding_value(binding: dict[str, Any], key: str) -> str | None:
    cell = binding.get(key)
    if not isinstance(cell, dict):
        return None
    if cell.get("type") == "bnode":
        return None
    raw = cell.get("value")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def split_multi(raw: str | None) -> list[str]:
    """Split a pipe-delimited GROUP_CONCAT value into sorted, clean items."""
    if not raw:
        return []
    seen: set[str] = set()
    for part in raw.split("|"):
        cleaned = " ".join(part.split())
        if not is_blank_node(cleaned):
            seen.add(cleaned)
    return sorted(seen)


def clean_text(raw: str | None) -> str | None:
    """Normalize whitespace and turn blank values or blank nodes into None."""
    if is_blank_node(raw):
        return None
    assert raw is not None
    normalized = " ".join(raw.split())
    return normalized or None


def parse_qid(raw_uri: str | None) -> str | None:
    """Extract and validate a Wikidata QID (e.g. Q3870135) from a URI or token."""
    cleaned = clean_text(raw_uri)
    if not cleaned:
        return None
    candidate = cleaned.rsplit("/", 1)[-1]
    return candidate if QID_RE.match(candidate) else None


def parse_year(raw_dates: str | None) -> int | None:
    """Extract a deterministic year in [1850, 2100] from raw ISO date strings."""
    years: list[int] = []
    for item in split_multi(raw_dates):
        if item.startswith("-"):
            continue
        match = ISO_YEAR_RE.match(item)
        if not match:
            continue
        year = int(match.group(1))
        if MIN_VALID_YEAR <= year <= MAX_VALID_YEAR:
            years.append(year)
    return min(years) if years else None


def parse_gender(raw_uris: str | None) -> str | None:
    """Map Wikidata gender QIDs to the controlled public.speakers vocabulary."""
    qids = sorted(
        qid for item in split_multi(raw_uris) if (qid := parse_qid(item)) is not None
    )
    if not qids:
        return None
    for qid in qids:
        if qid in GENDER_QID_MAP:
            return GENDER_QID_MAP[qid]
    return "other"


def parse_native_lang(raw_codes: str | None) -> str | None:
    """Validate ISO 639-1 two-letter language codes and pick deterministically."""
    valid = sorted(
        code
        for item in split_multi(raw_codes)
        if ISO_LANG_RE.match(code := item.lower())
    )
    return valid[0] if valid else None


def parse_country_code(raw_codes: str | None) -> str | None:
    """Validate ISO 3166-1 alpha-2 country codes and pick deterministically."""
    valid = sorted(
        code
        for item in split_multi(raw_codes)
        if ISO_COUNTRY_RE.match(code := item.upper())
    )
    return valid[0] if valid else None


def parse_imdb_nconst(raw_ids: str | None) -> str | None:
    """Validate IMDb person IDs (^nm[0-9]+$) and pick the lowest numeric ID."""
    valid = [item for item in split_multi(raw_ids) if IMDB_NCONST_RE.match(item)]
    if not valid:
        return None
    return min(valid, key=lambda nconst: (int(nconst[2:]), nconst))


def parse_tmdb_person_id(raw_ids: str | None) -> int | None:
    """Validate positive 32-bit TMDB person IDs and pick the lowest ID."""
    valid: list[int] = []
    for item in split_multi(raw_ids):
        if not TMDB_INT_RE.match(item):
            continue
        parsed = int(item)
        if 0 < parsed <= MAX_PG_INT32:
            valid.append(parsed)
    return min(valid) if valid else None


def build_sort_name(display_name: str, raw_family_names: str | None) -> str:
    """Build a MusicBrainz-style sort_name ('Surname, Given') when safe.

    Falls back to display_name when P734 family names are absent or do not
    match the suffix of display_name (for example mononyms or stage names).
    """
    candidates = sorted(
        split_multi(raw_family_names),
        key=lambda name: (-len(name), name),
    )
    lower_display = display_name.casefold()
    for family_name in candidates:
        suffix = f" {family_name}".casefold()
        if lower_display.endswith(suffix):
            prefix = display_name[: -len(family_name)].strip()
            surname = display_name[-len(family_name) :].strip()
            if prefix and surname:
                return f"{surname}, {prefix}"
    return display_name


def parse_legal_name(raw_legal_names: str | None, display_name: str) -> str | None:
    """Return a clean legal name only when it differs from display_name."""
    candidates = split_multi(raw_legal_names)
    for candidate in candidates:
        if candidate.casefold() != display_name.casefold():
            return candidate
    return None


def transform_binding(binding: dict[str, Any]) -> SpeakerRecord | None:
    """Transform one raw WDQS JSON binding into a validated SpeakerRecord.

    Returns None when required identity fields (wikidata_qid or display_name)
    are missing or invalid.
    """
    wikidata_qid = parse_qid(_binding_value(binding, "person"))
    display_name = clean_text(_binding_value(binding, "displayName"))
    if not wikidata_qid or not display_name:
        logger.debug(
            "Discarding binding with invalid identity: qid=%r display_name=%r",
            wikidata_qid,
            display_name,
        )
        return None

    birth_year = parse_year(_binding_value(binding, "birthDates"))
    death_year = parse_year(_binding_value(binding, "deathDates"))
    if birth_year is not None and death_year is not None and death_year < birth_year:
        logger.warning(
            "Clearing contradictory lifespan for %s: birth_year=%d death_year=%d",
            wikidata_qid,
            birth_year,
            death_year,
        )
        birth_year = None
        death_year = None

    return SpeakerRecord(
        wikidata_qid=wikidata_qid,
        display_name=display_name,
        sort_name=build_sort_name(display_name, _binding_value(binding, "familyNames")),
        legal_name=parse_legal_name(
            _binding_value(binding, "legalNames"), display_name
        ),
        birth_year=birth_year,
        death_year=death_year,
        gender=parse_gender(_binding_value(binding, "genderUris")),
        native_lang=parse_native_lang(_binding_value(binding, "nativeLangCodes")),
        country_code=parse_country_code(_binding_value(binding, "countryCodes")),
        imdb_nconst=parse_imdb_nconst(_binding_value(binding, "imdbIds")),
        tmdb_person_id=parse_tmdb_person_id(_binding_value(binding, "tmdbIds")),
    )


def _completeness_score(record: SpeakerRecord) -> int:
    optional_fields = (
        record.legal_name,
        record.birth_year,
        record.death_year,
        record.gender,
        record.native_lang,
        record.country_code,
        record.imdb_nconst,
        record.tmdb_person_id,
    )
    return sum(field is not None for field in optional_fields)


def transform_bindings(bindings: Iterable[dict[str, Any]]) -> TransformResult:
    """Transform and deduplicate a sequence of raw WDQS bindings by QID."""
    by_qid: dict[str, SpeakerRecord] = {}
    discarded_count = 0
    duplicate_qid_count = 0

    for binding in bindings:
        record = transform_binding(binding)
        if record is None:
            discarded_count += 1
            continue

        existing = by_qid.get(record.wikidata_qid)
        if existing is None:
            by_qid[record.wikidata_qid] = record
        else:
            duplicate_qid_count += 1
            if _completeness_score(record) > _completeness_score(existing):
                by_qid[record.wikidata_qid] = record

    ordered = [by_qid[qid] for qid in sorted(by_qid)]
    return TransformResult(
        records=ordered,
        discarded_count=discarded_count,
        duplicate_qid_count=duplicate_qid_count,
    )
