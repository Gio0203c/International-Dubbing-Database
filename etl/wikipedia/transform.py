"""Conservative wikitext parser and anti-data-laundering filter for Wikipedia pages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from etl.wikidata.transform import clean_text, parse_qid
from etl.wikipedia.extract import RawWikipediaPage

DISAMBIG_TITLE_PREFIXES = ("Lista di ", "Elenco di ", "List of ", "Categoria:", "Template:")
PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]+\)\s*$")
YEAR_RE = re.compile(r"\b(1[89][0-9]{2}|20[0-9]{2}|2100)\b")

COPYVIOL_RE = re.compile(
    r"\{\{\s*(Controlcopy|ViolazioneCopyright|Sospetta\s+violazione|Copyvio|Cancella\s+subito)\b",
    re.IGNORECASE,
)
UNSOURCED_RE = re.compile(
    r"\{\{\s*(F|NN|Senza\s+fonti|Unreferenced|BLP\s+unsourced)\s*(\||\}\})",
    re.IGNORECASE,
)
CLEANUP_RE = re.compile(r"\{\{\s*(C|P)\s*(\||\}\})", re.IGNORECASE)
DISAMBIG_TEMPLATE_RE = re.compile(
    r"\{\{\s*(Disambigua|Disambiguation|Dab)\s*(\||\}\})",
    re.IGNORECASE,
)
DUBBING_HEADING_RE = re.compile(
    r"^==+\s*(Doppiaggio|Voice\s+acting|Dubbing|Filmography|Ruoli\s+doppiati)\s*==+",
    re.IGNORECASE | re.MULTILINE,
)
BIO_PARAM_RE = re.compile(r"\|\s*([A-Za-zÀ-ÿ0-9_ ]+?)\s*=\s*([^|\n}]*)")


@dataclass(frozen=True)
class WikipediaSpeakerRecord:
    """Normalized voice-actor record ready for conservative loading into public.speakers."""

    wiki_lang: str
    page_id: int
    revision_id: int
    page_title: str
    page_url: str
    wikidata_qid: str | None
    display_name: str
    sort_name: str
    birth_year: int | None
    death_year: int | None
    gender: str | None
    country_code: str | None
    allow_bio_backfill: bool
    is_person_confirmed: bool
    has_dubbing_section: bool
    compliance_flags: tuple[str, ...]


def detect_compliance_flags(wikitext: str, wiki_lang: str = "it") -> tuple[str, ...]:
    """Detect copyviol, unsourced, cleanup, and third-party catalog warnings in wikitext."""
    flags: set[str] = set()
    if COPYVIOL_RE.search(wikitext):
        flags.add("copyviol_template")
    if UNSOURCED_RE.search(wikitext):
        flags.add("unsourced_template")
    if CLEANUP_RE.search(wikitext):
        flags.add("cleanup_template")
    if "antoniogenna.net" in wikitext.lower():
        flags.add("third_party_catalog_ref")
    return tuple(sorted(flags))


def has_blocking_copyviol(flags: tuple[str, ...]) -> bool:
    """Return True if a page carries an active copyright-violation warning template."""
    return "copyviol_template" in flags


def has_copyviol_or_unsourced(flags: tuple[str, ...]) -> bool:
    """Return True if biographical backfills must be suppressed for this page."""
    return "copyviol_template" in flags or "unsourced_template" in flags


def has_dubbing_section(wikitext: str) -> bool:
    """Return True if wikitext contains a dubbing or voice-acting cast heading."""
    return bool(DUBBING_HEADING_RE.search(wikitext))


def _extract_bio_block(wikitext: str) -> str | None:
    match = re.search(r"\{\{\s*Bio\b", wikitext, flags=re.IGNORECASE)
    if not match:
        return None
    start = match.start()
    depth = 0
    i = start
    length = len(wikitext)
    while i < length - 1:
        two = wikitext[i : i + 2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return wikitext[start:i]
            continue
        i += 1
    return None


def _clean_bio_value(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    cleaned = re.sub(r"\[\[(?:[^|\]]+\|)?([^\]]+)\]\]", r"\1", cleaned)
    return clean_text(cleaned)


def _extract_valid_year(raw: str | None) -> int | None:
    cleaned = _clean_bio_value(raw)
    if not cleaned:
        return None
    match = YEAR_RE.search(cleaned)
    if not match:
        return None
    year = int(match.group(1))
    return year if 1850 <= year <= 2100 else None


def parse_itwiki_bio_template(wikitext: str) -> dict[str, Any]:
    """Extract structured biographical attributes from an Italian Wikipedia {{Bio}} block."""
    block = _extract_bio_block(wikitext)
    if block is None:
        return {}

    raw_params: dict[str, str] = {}
    for match in BIO_PARAM_RE.finditer(block):
        key = clean_text(match.group(1))
        if key:
            raw_params[key.lower()] = match.group(2)

    given_name = _clean_bio_value(raw_params.get("nome"))
    surname = _clean_bio_value(raw_params.get("cognome"))
    sesso = (_clean_bio_value(raw_params.get("sesso")) or "").upper()
    gender = "male" if sesso == "M" else ("female" if sesso == "F" else None)

    birth_year = _extract_valid_year(raw_params.get("annonascita"))
    death_year = _extract_valid_year(raw_params.get("annomorte"))
    if birth_year is not None and death_year is not None and death_year < birth_year:
        death_year = None

    nationality = (_clean_bio_value(raw_params.get("nazionalità")) or "").lower()
    country_code = "IT" if nationality.startswith("italian") else None

    return {
        "has_bio": True,
        "given_name": given_name,
        "surname": surname,
        "gender": gender,
        "birth_year": birth_year,
        "death_year": death_year,
        "country_code": country_code,
    }


def _strip_disambiguator(title: str) -> str:
    stripped = PAREN_SUFFIX_RE.sub("", title).strip()
    return stripped if stripped else title.strip()


def transform_wikipedia_pages(
    pages: list[RawWikipediaPage],
) -> tuple[list[WikipediaSpeakerRecord], dict[int, tuple[str, ...]], dict[str, int]]:
    """Screen raw Wikipedia pages and emit deduplicated conservative speaker records."""
    flags_by_page_id: dict[int, tuple[str, ...]] = {}
    by_qid: dict[str, WikipediaSpeakerRecord] = {}
    no_qid_by_page: dict[tuple[str, int], WikipediaSpeakerRecord] = {}
    metrics = {
        "copyviol_blocked": 0,
        "unsourced_flagged": 0,
        "third_party_ref_flagged": 0,
        "dubbing_sections_detected": 0,
        "non_person_skipped": 0,
    }

    for page in sorted(pages, key=lambda item: (item.wiki_lang, item.page_id)):
        flags = detect_compliance_flags(page.wikitext, page.wiki_lang)
        flags_by_page_id[page.page_id] = flags

        if "unsourced_template" in flags:
            metrics["unsourced_flagged"] += 1
        if "third_party_ref_flagged" in flags or "third_party_catalog_ref" in flags:
            metrics["third_party_ref_flagged"] += 1
        has_dub = has_dubbing_section(page.wikitext)
        if has_dub:
            metrics["dubbing_sections_detected"] += 1

        if has_blocking_copyviol(flags):
            metrics["copyviol_blocked"] += 1
            continue

        if any(page.page_title.startswith(prefix) for prefix in DISAMBIG_TITLE_PREFIXES) or DISAMBIG_TEMPLATE_RE.search(page.wikitext):
            metrics["non_person_skipped"] += 1
            continue

        bio = parse_itwiki_bio_template(page.wikitext) if page.wiki_lang == "it" else {}
        is_person_confirmed = bool(bio.get("has_bio")) if page.wiki_lang == "it" else True
        qid = parse_qid(page.wikidata_qid)

        if not is_person_confirmed and qid is None:
            metrics["non_person_skipped"] += 1
            continue

        display_name = _strip_disambiguator(page.page_title)
        given_name = bio.get("given_name")
        surname = bio.get("surname")
        if given_name and surname:
            sort_name = f"{surname}, {given_name}"
        else:
            sort_name = display_name

        record = WikipediaSpeakerRecord(
            wiki_lang=page.wiki_lang,
            page_id=page.page_id,
            revision_id=page.revision_id,
            page_title=page.page_title,
            page_url=page.page_url,
            wikidata_qid=qid,
            display_name=display_name,
            sort_name=sort_name,
            birth_year=bio.get("birth_year"),
            death_year=bio.get("death_year"),
            gender=bio.get("gender"),
            country_code=bio.get("country_code"),
            allow_bio_backfill=not has_copyviol_or_unsourced(flags),
            is_person_confirmed=is_person_confirmed,
            has_dubbing_section=has_dub,
            compliance_flags=flags,
        )

        if qid is not None:
            if qid not in by_qid:
                by_qid[qid] = record
        else:
            no_qid_by_page[(page.wiki_lang, page.page_id)] = record

    ordered = [by_qid[q] for q in sorted(by_qid)] + [
        no_qid_by_page[k] for k in sorted(no_qid_by_page)
    ]
    return ordered, flags_by_page_id, metrics
