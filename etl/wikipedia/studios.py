"""Multi-language Wikipedia + Wikidata studio enrichment, alias extraction, and PostgreSQL load."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
import requests

from etl.wikidata.dubbing import HTTP_URL_RE
from etl.wikidata.transform import (
    clean_text,
    parse_country_code,
    parse_qid,
    parse_year,
)
from etl.wikipedia.extract import (
    RawWikipediaPage,
    _mediawiki_get,
    fetch_category_titles,
    get_wikipedia_user_agent,
)
from etl.wikipedia.transform import (
    DISAMBIG_TEMPLATE_RE,
    DISAMBIG_TITLE_PREFIXES,
    detect_compliance_flags,
    has_blocking_copyviol,
    has_copyviol_or_unsourced,
)

LOGGER = logging.getLogger(__name__)

WIKIDATA_API_ENDPOINT = "https://www.wikidata.org/w/api.php"
STUDIO_TYPE_QIDS = frozenset({"Q1298801"})
DUBBING_INDUSTRY_QIDS = frozenset({"Q187657"})
DISAMBIG_QID = "Q4167410"
HUMAN_QID = "Q5"

DEFAULT_STUDIO_CATEGORIES: dict[str, tuple[str, ...]] = {
    "it": (
        "Categoria:Studi di doppiaggio italiani",
        "Categoria:Studi di doppiaggio",
    ),
    "en": (
        "Category:Italian dubbing studios",
        "Category:Dubbing studios",
    ),
}

IT_INFOBOX_SEARCH_QUERY = (
    r"insource:/\{\{\s*Azienda/i insource:/(settore|prodotti)\s*=[^\n]*doppiaggio/i"
)

COUNTRY_NAME_TO_ISO: dict[str, str] = {
    "ITA": "IT",
    "ITALY": "IT",
    "ITALIA": "IT",
    "FRA": "FR",
    "FRANCE": "FR",
    "FRANCIA": "FR",
    "USA": "US",
    "U.S.": "US",
    "U.S.A.": "US",
    "UNITED STATES": "US",
    "STATI UNITI": "US",
    "STATI UNITI D'AMERICA": "US",
    "GBR": "GB",
    "UK": "GB",
    "U.K.": "GB",
    "UNITED KINGDOM": "GB",
    "REGNO UNITO": "GB",
    "JPN": "JP",
    "JAPAN": "JP",
    "GIAPPONE": "JP",
    "DEU": "DE",
    "GERMANY": "DE",
    "GERMANIA": "DE",
    "ESP": "ES",
    "SPAIN": "ES",
    "SPAGNA": "ES",
    "CAN": "CA",
    "CANADA": "CA",
    "BEL": "BE",
    "BELGIUM": "BE",
    "BELGIO": "BE",
    "CHE": "CH",
    "SWITZERLAND": "CH",
    "SVIZZERA": "CH",
    "DNK": "DK",
    "DENMARK": "DK",
    "DANIMARCA": "DK",
    "SWE": "SE",
    "SWEDEN": "SE",
    "SVEZIA": "SE",
    "POL": "PL",
    "POLAND": "PL",
    "POLONIA": "PL",
    "BRA": "BR",
    "BRAZIL": "BR",
    "BRASILE": "BR",
    "MEX": "MX",
    "MEXICO": "MX",
    "MESSICO": "MX",
    "ARG": "AR",
    "ARGENTINA": "AR",
    "IND": "IN",
    "INDIA": "IN",
    "FIN": "FI",
    "FINLAND": "FI",
    "FINLANDIA": "FI",
    "ISR": "IL",
    "ISRAEL": "IL",
    "ISRAELE": "IL",
    "LBN": "LB",
    "LEBANON": "LB",
    "LIBANO": "LB",
    "SYR": "SY",
    "SYRIA": "SY",
    "SIRIA": "SY",
    "NOR": "NO",
    "NORWAY": "NO",
    "NORVEGIA": "NO",
    "NLD": "NL",
    "NETHERLANDS": "NL",
    "PAESI BASSI": "NL",
    "AUS": "AU",
    "AUSTRALIA": "AU",
    "KOR": "KR",
    "SOUTH KOREA": "KR",
    "COREA DEL SUD": "KR",
}

YEAR_RE = re.compile(r"\b(18[89][0-9]|19[0-9]{2}|20[0-9]{2}|2100)\b")
PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]+\)\s*$")
REF_TAG_RE = re.compile(r"<ref\b[^>]*>.*?</ref>|<ref\b[^>]*/>", re.IGNORECASE | re.S)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
WIKILINK_TARGET_RE = re.compile(r"\[\[([^|\]]+)(?:\|([^\]]+))?\]\]")
URL_TEMPLATE_RE = re.compile(r"\{\{\s*(?:URL|Official\s+website)\s*\|\s*(?:1\s*=\s*)?([^|}\s]+)", re.IGNORECASE)
HTTP_EXTRACT_RE = re.compile(r"(https?://[^\s\]\|}<>\"]+)")
BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)

EN_UNSOURCED_RE = re.compile(
    r"\{\{\s*(Unreliable\s+sources|More\s+citations\s+needed|BLP\s+sources|Primary\s+sources|Self-published)\b",
    re.IGNORECASE,
)
IT_COMPANY_LEAD_RE = re.compile(
    r"\b(?:societ[àa]|studio|cooperativa|azienda)\b.{0,50}\bdoppiaggio\b",
    re.IGNORECASE | re.S,
)
LEAD_ABBREV_RE = re.compile(
    r"(?:abbreviat[ao]\s+(?:in|come)|nota\s+anche\s+come|sigla)\s+'''([^']{2,20})'''",
    re.IGNORECASE,
)
FORMER_NAME_CUE_RE = re.compile(
    r"(?:attuale\s+incarnazione|precedentemente|cambiato(?:\s+il\s+proprio)?\s+nome|ridenominat[ao]|gi[àa]\s+nota|formerly|originally|previously)",
    re.IGNORECASE,
)
ABBREVIATION_TOKEN_RE = re.compile(r"^(?:[A-Z0-9]{2,6}|(?:[A-Z]\.){2,6})$")
ACQUIRED_CUE_RE = re.compile(r"\b(?:acquisit[ao]|assorbit[ao]|acquired|bought)\b", re.IGNORECASE)
MERGED_CUE_RE = re.compile(r"\b(?:incorporat[ao]|fus[ao]|sciss[ao]|merged)\b", re.IGNORECASE)
DISALLOWED_LINK_TARGET_RE = re.compile(
    r"^(?:Categoria|File|Immagine|Image|Template|Wikipedia|Aiuto|Portale):",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RawWikipediaStudioPage:
    """Wikipedia studio article paired with back-redirects and discovery origin."""

    page: RawWikipediaPage
    redirect_titles: tuple[str, ...]
    from_category: bool


@dataclass(frozen=True)
class WikipediaStudioAlias:
    """Normalized alias entry ready for public.studio_aliases."""

    alias_name: str
    alias_type: str
    source: str


@dataclass(frozen=True)
class WikipediaStudioRelationship:
    """Normalized corporate lineage link ready for public.studio_relationships."""

    direction: str
    relationship_type: str
    target_qid: str | None
    target_name: str | None
    event_year: int | None
    source: str


@dataclass(frozen=True)
class WikipediaStudioRecord:
    """Normalized dubbing studio record combining Wikidata CC0 and Wikipedia CC-BY-SA-4.0."""

    wiki_lang: str
    page_id: int
    revision_id: int
    page_title: str
    page_url: str
    wikidata_qid: str | None
    name: str
    status: str
    country_code: str | None
    city: str | None
    founded_year: int | None
    closed_year: int | None
    website_url: str | None
    used_wikidata_claims: bool
    allow_wikitext_backfill: bool
    missing_wikidata_studio_type: bool
    aliases: tuple[WikipediaStudioAlias, ...]
    relationships: tuple[WikipediaStudioRelationship, ...]
    compliance_flags: tuple[str, ...]


def discover_studio_titles(
    wiki_lang: str,
    categories: tuple[str, ...] | None = None,
    *,
    include_infobox_search: bool = True,
    limit: int | None = None,
    user_agent: str | None = None,
    session: requests.Session | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[list[str], set[str]]:
    """Discover studio article titles from Wikipedia categories and targeted Italian infobox search."""
    lang = wiki_lang.strip().lower()
    target_categories = categories or DEFAULT_STUDIO_CATEGORIES.get(lang)
    if not target_categories:
        raise ValueError(
            f"No default studio categories configured for wiki_lang={lang!r}; pass --categories explicitly."
        )

    cat_titles = set(
        fetch_category_titles(
            lang,
            target_categories,
            limit=limit,
            user_agent=user_agent,
            session=session,
            sleep_fn=sleep_fn,
        )
    )
    all_titles = set(cat_titles)

    if include_infobox_search and lang == "it" and (limit is None or len(all_titles) < limit):
        endpoint = f"https://{lang}.wikipedia.org/w/api.php"
        payload = _mediawiki_get(
            endpoint,
            {
                "action": "query",
                "list": "search",
                "srsearch": IT_INFOBOX_SEARCH_QUERY,
                "srnamespace": "0",
                "srlimit": "50",
            },
            user_agent=user_agent or get_wikipedia_user_agent(),
            session=session,
            sleep_fn=sleep_fn,
        )
        for hit in payload.get("query", {}).get("search", []):
            title = hit.get("title")
            if isinstance(title, str) and title.strip():
                all_titles.add(title.strip())

    ordered = sorted(all_titles)
    if limit is not None:
        ordered = ordered[:limit]
    return ordered, cat_titles


def fetch_studio_pages_batch(
    wiki_lang: str,
    titles: list[str],
    category_titles: set[str],
    *,
    batch_size: int = 50,
    user_agent: str | None = None,
    session: requests.Session | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[RawWikipediaStudioPage]:
    """Fetch studio wikitext, QIDs, and back-redirects in batches via MediaWiki Action API."""
    endpoint = f"https://{wiki_lang}.wikipedia.org/w/api.php"
    ua = user_agent or get_wikipedia_user_agent()
    pages_by_id: dict[int, RawWikipediaPage] = {}
    redirects_by_id: dict[int, set[str]] = {}
    redirect_origin_by_target: dict[str, set[str]] = {}

    for idx in range(0, len(titles), batch_size):
        chunk = titles[idx : idx + batch_size]
        if not chunk:
            continue
        rdcontinue: str | None = None
        while True:
            params: dict[str, Any] = {
                "action": "query",
                "prop": "revisions|pageprops|info|redirects",
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
                "ppprop": "wikibase_item",
                "inprop": "url",
                "redirects": "1",
                "rdnamespace": "0",
                "rdlimit": "500",
                "titles": "|".join(chunk),
            }
            if rdcontinue:
                params["rdcontinue"] = rdcontinue

            payload = _mediawiki_get(
                endpoint,
                params,
                user_agent=ua,
                session=session,
                sleep_fn=sleep_fn,
            )
            query_block = payload.get("query", {})
            for redir in query_block.get("redirects", []):
                src = redir.get("from")
                dst = redir.get("to")
                if isinstance(src, str) and isinstance(dst, str):
                    redirect_origin_by_target.setdefault(dst.strip(), set()).add(src.strip())

            for page in query_block.get("pages", []):
                if page.get("missing"):
                    continue
                page_id = page.get("pageid")
                page_title = page.get("title")
                revisions = page.get("revisions") or []
                if not isinstance(page_id, int) or page_id <= 0 or not page_title or not revisions:
                    continue

                if page_id not in pages_by_id:
                    rev = revisions[0]
                    rev_id = rev.get("revid")
                    wikitext = (
                        rev.get("slots", {}).get("main", {}).get("content")
                        if isinstance(rev.get("slots"), dict)
                        else rev.get("content")
                    )
                    if not isinstance(rev_id, int) or rev_id <= 0 or not isinstance(wikitext, str):
                        continue

                    title_str = str(page_title).strip()
                    page_url = page.get("fullurl") or (
                        f"https://{wiki_lang}.wikipedia.org/wiki/{title_str.replace(' ', '_')}"
                    )
                    raw_qid = (
                        page.get("pageprops", {}).get("wikibase_item")
                        if isinstance(page.get("pageprops"), dict)
                        else None
                    )
                    wikidata_qid = str(raw_qid).strip() if raw_qid else None
                    pages_by_id[page_id] = RawWikipediaPage(
                        wiki_lang=wiki_lang,
                        page_id=page_id,
                        revision_id=rev_id,
                        page_title=title_str,
                        page_url=str(page_url).strip(),
                        wikidata_qid=wikidata_qid,
                        wikitext=wikitext,
                        content_hash=hashlib.sha256(wikitext.encode("utf-8")).hexdigest(),
                    )
                    redirects_by_id.setdefault(page_id, set())

                for backlink in page.get("redirects", []):
                    btitle = backlink.get("title")
                    if isinstance(btitle, str) and btitle.strip():
                        redirects_by_id[page_id].add(btitle.strip())

            cont = payload.get("continue", {}).get("rdcontinue")
            if not cont:
                break
            rdcontinue = str(cont)

    results: list[RawWikipediaStudioPage] = []
    for pid in sorted(pages_by_id):
        raw_page = pages_by_id[pid]
        origins = redirect_origin_by_target.get(raw_page.page_title, set())
        all_redirects = sorted(redirects_by_id.get(pid, set()) | origins)
        from_cat = (
            raw_page.page_title in category_titles
            or any(title in category_titles for title in all_redirects)
        )
        results.append(
            RawWikipediaStudioPage(
                page=raw_page,
                redirect_titles=tuple(all_redirects),
                from_category=from_cat,
            )
        )
    return results


def _wbgetentities_chunked(
    qids: list[str],
    *,
    props: str,
    languages: str = "it|en",
    user_agent: str | None = None,
    session: requests.Session | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, dict[str, Any]]:
    if not qids:
        return {}
    ua = user_agent or get_wikipedia_user_agent()
    client = session or requests.Session()
    owns_session = session is None
    entities: dict[str, dict[str, Any]] = {}
    unique_qids = sorted(set(qids), key=lambda q: int(q[1:]))

    try:
        for idx in range(0, len(unique_qids), 50):
            chunk = unique_qids[idx : idx + 50]
            payload = _mediawiki_get(
                WIKIDATA_API_ENDPOINT,
                {
                    "action": "wbgetentities",
                    "ids": "|".join(chunk),
                    "props": props,
                    "languages": languages,
                },
                user_agent=ua,
                session=client,
                sleep_fn=sleep_fn,
            )
            entities.update(payload.get("entities", {}))
    finally:
        if owns_session:
            client.close()
    return entities


def _claim_entity_qids(entity: dict[str, Any], prop: str) -> tuple[str, ...]:
    qids: set[str] = set()
    for stmt in entity.get("claims", {}).get(prop, []):
        if stmt.get("rank") == "deprecated":
            continue
        dv = stmt.get("mainsnak", {}).get("datavalue", {})
        if dv.get("type") == "wikibase-entityid":
            target = dv.get("value", {}).get("id")
            if isinstance(target, str) and parse_qid(target) is not None:
                qids.add(target)
    return tuple(sorted(qids, key=lambda q: int(q[1:])))


def _claim_year(entity: dict[str, Any], prop: str) -> int | None:
    years: list[int] = []
    for stmt in entity.get("claims", {}).get(prop, []):
        if stmt.get("rank") == "deprecated":
            continue
        dv = stmt.get("mainsnak", {}).get("datavalue", {})
        if dv.get("type") == "time":
            raw_time = dv.get("value", {}).get("time")
            year = parse_year(raw_time)
            if year is not None and 1880 <= year <= 2100:
                years.append(year)
    return min(years) if years else None


def _claim_strings(entity: dict[str, Any], prop: str) -> tuple[str, ...]:
    values: list[str] = []
    for stmt in entity.get("claims", {}).get(prop, []):
        if stmt.get("rank") == "deprecated":
            continue
        dv = stmt.get("mainsnak", {}).get("datavalue", {})
        if dv.get("type") == "string":
            raw = clean_text(str(dv.get("value", "")))
            if raw is not None:
                values.append(raw)
    return tuple(sorted(set(values)))


def fetch_wikidata_studio_entities(
    qids: list[str],
    *,
    user_agent: str | None = None,
    session: requests.Session | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Fetch studio Wikidata items and resolve referenced country and city entities."""
    valid_qids = [q for q in qids if parse_qid(q) is not None]
    studio_entities = _wbgetentities_chunked(
        valid_qids,
        props="labels|aliases|claims",
        user_agent=user_agent,
        session=session,
        sleep_fn=sleep_fn,
    )
    aux_qids: set[str] = set()
    for ent in studio_entities.values():
        for prop in ("P17", "P159", "P276"):
            aux_qids.update(_claim_entity_qids(ent, prop))

    aux_entities = _wbgetentities_chunked(
        list(aux_qids),
        props="labels|claims",
        user_agent=user_agent,
        session=session,
        sleep_fn=sleep_fn,
    )
    return studio_entities, aux_entities


def _extract_balanced_template(wikitext: str, pattern: re.Pattern[str]) -> tuple[str, int, int] | None:
    match = pattern.search(wikitext)
    if not match:
        return None
    start = match.start()
    depth = 0
    idx = start
    length = len(wikitext)
    while idx < length - 1:
        two = wikitext[idx : idx + 2]
        if two == "{{":
            depth += 1
            idx += 2
            continue
        if two == "}}":
            depth -= 1
            idx += 2
            if depth == 0:
                return wikitext[start:idx], start, idx
            continue
        idx += 1
    return None


def _strip_refs_and_comments(raw: str) -> str:
    text = COMMENT_RE.sub("", raw)
    return REF_TAG_RE.sub("", text)


def _strip_wikilinks(raw: str) -> str:
    return WIKILINK_TARGET_RE.sub(lambda m: m.group(2) or m.group(1), raw)


def _clean_infobox_field(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _strip_wikilinks(_strip_refs_and_comments(raw))
    text = re.sub(r"''+", "", text)
    return clean_text(text)


def _extract_year_from_field(raw: str | None) -> int | None:
    cleaned = _clean_infobox_field(raw)
    if not cleaned:
        return None
    match = YEAR_RE.search(cleaned)
    return int(match.group(1)) if match else None


def _parse_country_from_field(raw: str | None) -> str | None:
    cleaned = _clean_infobox_field(raw)
    if not cleaned:
        return None
    upper = cleaned.upper()
    if upper in COUNTRY_NAME_TO_ISO:
        return COUNTRY_NAME_TO_ISO[upper]
    return parse_country_code(upper)


def _extract_city_from_field(raw: str | None) -> str | None:
    if not raw or BR_RE.search(raw):
        return None
    cleaned = _clean_infobox_field(raw)
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = clean_text(cleaned.split(",", 1)[0])
        if not cleaned:
            return None
    if len(cleaned) > 60 or "*" in cleaned:
        return None
    if cleaned.upper() in COUNTRY_NAME_TO_ISO:
        return None
    return cleaned


def _extract_website_from_field(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _strip_refs_and_comments(raw).strip()
    tmpl = URL_TEMPLATE_RE.search(text)
    candidate = tmpl.group(1).strip() if tmpl else None
    if candidate is None:
        http_match = HTTP_EXTRACT_RE.search(text)
        candidate = http_match.group(1).strip() if http_match else None
    if candidate is None:
        return None
    if not candidate.startswith(("http://", "https://")) and "." in candidate:
        candidate = f"https://{candidate}"
    candidate = candidate.rstrip(".,;)")
    return candidate if HTTP_URL_RE.fullmatch(candidate) else None


def _extract_link_targets(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    cleaned = _strip_refs_and_comments(raw)
    targets: list[str] = []
    seen: set[str] = set()
    for match in WIKILINK_TARGET_RE.finditer(cleaned):
        target = clean_text(match.group(1))
        if not target or DISALLOWED_LINK_TARGET_RE.match(target):
            continue
        key = target.lower()
        if key not in seen:
            seen.add(key)
            targets.append(target)
    return tuple(targets)


def _parse_template_params(block: str) -> dict[str, str]:
    """Parse top-level key=value parameters from a balanced template block."""
    parts: list[str] = []
    buf: list[str] = []
    tmpl_depth = 1
    link_depth = 0
    idx = 2 if block.startswith("{{") else 0
    length = len(block)

    while idx < length:
        two = block[idx : idx + 2]
        if two == "{{":
            tmpl_depth += 1
            buf.append(two)
            idx += 2
            continue
        if two == "}}":
            tmpl_depth -= 1
            if tmpl_depth <= 0:
                break
            buf.append(two)
            idx += 2
            continue
        if two == "[[":
            link_depth += 1
            buf.append(two)
            idx += 2
            continue
        if two == "]]":
            link_depth = max(0, link_depth - 1)
            buf.append(two)
            idx += 2
            continue
        ch = block[idx]
        if ch == "|" and tmpl_depth == 1 and link_depth == 0:
            parts.append("".join(buf))
            buf.clear()
        else:
            buf.append(ch)
        idx += 1

    if buf:
        parts.append("".join(buf))

    params: dict[str, str] = {}
    for chunk in parts[1:]:
        if "=" not in chunk:
            continue
        raw_key, raw_val = chunk.split("=", 1)
        key = clean_text(raw_key)
        if key:
            params[key.lower()] = raw_val.strip()
    return params


def parse_studio_infobox(wikitext: str, wiki_lang: str) -> dict[str, Any]:
    """Extract structured company attributes from {{Azienda}} or {{Infobox company}}."""
    lang = wiki_lang.strip().lower()
    if lang == "it":
        extracted = _extract_balanced_template(
            wikitext, re.compile(r"\{\{\s*Azienda\b", re.IGNORECASE)
        )
    else:
        extracted = _extract_balanced_template(
            wikitext, re.compile(r"\{\{\s*Infobox\s+company\b", re.IGNORECASE)
        )

    if extracted is None:
        return {"has_infobox": False, "lead_text": _clean_lead_text(wikitext)}

    block, start, end = extracted
    params = _parse_template_params(block)

    if lang == "it":
        founded_year = _extract_year_from_field(params.get("data fondazione"))
        closed_year = _extract_year_from_field(params.get("data chiusura"))
        country_code = _parse_country_from_field(params.get("nazione"))
        city = _extract_city_from_field(params.get("sede")) or _extract_city_from_field(
            params.get("luogo fondazione")
        )
        website_url = _extract_website_from_field(params.get("sito"))
        closure_raw = params.get("causa chiusura")
        parent_raw = params.get("gruppo")
    else:
        founded_year = _extract_year_from_field(params.get("founded"))
        closed_year = _extract_year_from_field(params.get("defunct"))
        country_code = _parse_country_from_field(
            params.get("hq_location_country") or params.get("location_country")
        )
        city = _extract_city_from_field(
            params.get("hq_location_city")
            or params.get("location_city")
            or params.get("hq_location")
        )
        website_url = _extract_website_from_field(params.get("website") or params.get("homepage"))
        closure_raw = params.get("fate")
        parent_parts = [params.get("parent", ""), params.get("owner", "")]
        parent_raw = " ".join(part for part in parent_parts if part).strip() or None

    if founded_year is not None and closed_year is not None and closed_year < founded_year:
        closed_year = None

    remainder = (wikitext[:start] + wikitext[end:]).strip()
    return {
        "has_infobox": True,
        "founded_year": founded_year,
        "closed_year": closed_year,
        "country_code": country_code,
        "city": city,
        "website_url": website_url,
        "closure_cause_text": _clean_infobox_field(closure_raw),
        "closure_targets": _extract_link_targets(closure_raw),
        "parent_targets": _extract_link_targets(parent_raw),
        "lead_text": _clean_lead_text(remainder),
    }


def _clean_lead_text(wikitext: str) -> str:
    no_templates = re.sub(r"\{\{[^{}]*\}\}", "", _strip_refs_and_comments(wikitext))
    return _strip_wikilinks(no_templates)[:1000]


def _classify_wikipedia_alias(alias_name: str, canonical_name: str, lead_text: str) -> str | None:
    cleaned = clean_text(alias_name)
    if cleaned is None:
        return None
    if cleaned.lower() == canonical_name.lower():
        return None
    if ABBREVIATION_TOKEN_RE.fullmatch(cleaned):
        return "abbreviation"
    pattern = rf"(?<!\w){re.escape(cleaned)}(?!\w)"
    for match in re.finditer(pattern, lead_text, flags=re.IGNORECASE):
        window_start = max(0, match.start() - 120)
        window = lead_text[window_start : match.start()]
        if FORMER_NAME_CUE_RE.search(window):
            return "former_name"
    return "other"


def _extract_wikidata_studio_attrs(
    qid: str | None,
    wikidata_entities: dict[str, dict[str, Any]],
    aux_entities: dict[str, dict[str, Any]],
    preferred_lang: str,
) -> dict[str, Any]:
    if qid is None or qid not in wikidata_entities:
        return {"present": False}
    ent = wikidata_entities[qid]
    p31 = set(_claim_entity_qids(ent, "P31"))
    p452 = set(_claim_entity_qids(ent, "P452"))
    p1056 = set(_claim_entity_qids(ent, "P1056"))
    labels = ent.get("labels", {})
    label_it = clean_text(labels.get("it", {}).get("value"))
    label_en = clean_text(labels.get("en", {}).get("value"))

    country_codes: list[str] = []
    for cq in _claim_entity_qids(ent, "P17"):
        aux = aux_entities.get(cq, {})
        for code in _claim_strings(aux, "P297"):
            parsed = parse_country_code(code)
            if parsed is not None:
                country_codes.append(parsed)
    country_code = min(country_codes) if country_codes else None

    city: str | None = None
    for prop in ("P159", "P276"):
        for loc_q in _claim_entity_qids(ent, prop):
            aux_labels = aux_entities.get(loc_q, {}).get("labels", {})
            loc_it = clean_text(aux_labels.get("it", {}).get("value"))
            loc_en = clean_text(aux_labels.get("en", {}).get("value"))
            chosen = loc_it or loc_en if preferred_lang == "it" else loc_en or loc_it
            if chosen is not None:
                city = chosen
                break
        if city is not None:
            break

    founded_year = _claim_year(ent, "P571")
    closed_year = _claim_year(ent, "P576")
    if founded_year is not None and closed_year is not None and closed_year < founded_year:
        closed_year = None

    website_candidates = [url for url in _claim_strings(ent, "P856") if HTTP_URL_RE.fullmatch(url)]
    website_url = min(website_candidates) if website_candidates else None

    alt_names: list[str] = []
    for lang in ("it", "en"):
        for item in ent.get("aliases", {}).get(lang, []):
            cleaned = clean_text(item.get("value"))
            if cleaned is not None:
                alt_names.append(cleaned)
    for alt_label in (label_it, label_en):
        if alt_label is not None:
            alt_names.append(alt_label)

    replaced_by = _claim_entity_qids(ent, "P1366")
    followed_by = _claim_entity_qids(ent, "P156")
    parent_orgs = _claim_entity_qids(ent, "P749")
    replaces = _claim_entity_qids(ent, "P1365")
    follows = _claim_entity_qids(ent, "P155")
    children = _claim_entity_qids(ent, "P355")

    return {
        "present": True,
        "is_human": HUMAN_QID in p31,
        "is_disambig": DISAMBIG_QID in p31,
        "has_studio_tag": bool((p31 & STUDIO_TYPE_QIDS) or (p452 & DUBBING_INDUSTRY_QIDS) or (p1056 & DUBBING_INDUSTRY_QIDS)),
        "label_it": label_it,
        "label_en": label_en,
        "country_code": country_code,
        "city": city,
        "founded_year": founded_year,
        "closed_year": closed_year,
        "website_url": website_url,
        "alt_names": tuple(alt_names),
        "replaced_by": replaced_by,
        "followed_by": followed_by,
        "parent_orgs": parent_orgs,
        "replaces": replaces,
        "follows": follows,
        "children": children,
    }


def _build_studio_flags(wikitext: str, wiki_lang: str) -> tuple[str, ...]:
    flags = set(detect_compliance_flags(wikitext, wiki_lang))
    if EN_UNSOURCED_RE.search(wikitext):
        flags.add("cleanup_template")
    return tuple(sorted(flags))


def _strip_disambiguator(title: str) -> str:
    stripped = PAREN_SUFFIX_RE.sub("", title).strip()
    return stripped or title.strip()


def transform_wikipedia_studio_pages(
    pages: list[RawWikipediaStudioPage],
    wikidata_entities: dict[str, dict[str, Any]] | None = None,
    aux_entities: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[WikipediaStudioRecord], dict[int, tuple[str, ...]], dict[str, int]]:
    """Screen raw Wikipedia studio pages and emit normalized WikipediaStudioRecord objects."""
    wd_map = wikidata_entities or {}
    aux_map = aux_entities or {}
    flags_by_page_id: dict[int, tuple[str, ...]] = {}
    records: list[WikipediaStudioRecord] = []
    metrics = {
        "copyviol_blocked": 0,
        "unsourced_flagged": 0,
        "non_studio_skipped": 0,
        "missing_wikidata_studio_type": 0,
    }

    for item in sorted(pages, key=lambda obj: (obj.page.wiki_lang, obj.page.page_id)):
        raw = item.page
        flags = _build_studio_flags(raw.wikitext, raw.wiki_lang)
        flags_by_page_id[raw.page_id] = flags

        if "unsourced_template" in flags:
            metrics["unsourced_flagged"] += 1
        if has_blocking_copyviol(flags):
            metrics["copyviol_blocked"] += 1
            continue
        if (
            any(raw.page_title.startswith(prefix) for prefix in DISAMBIG_TITLE_PREFIXES)
            or DISAMBIG_TEMPLATE_RE.search(raw.wikitext)
        ):
            metrics["non_studio_skipped"] += 1
            continue

        qid = parse_qid(raw.wikidata_qid)
        wd_attrs = _extract_wikidata_studio_attrs(qid, wd_map, aux_map, raw.wiki_lang)
        if wd_attrs.get("is_human") or wd_attrs.get("is_disambig"):
            metrics["non_studio_skipped"] += 1
            continue

        infobox = parse_studio_infobox(raw.wikitext, raw.wiki_lang)
        if not item.from_category:
            if not infobox["has_infobox"] or not IT_COMPANY_LEAD_RE.search(infobox["lead_text"]):
                metrics["non_studio_skipped"] += 1
                continue

        if raw.wiki_lang == "it":
            canonical_name = (
                wd_attrs.get("label_it")
                or wd_attrs.get("label_en")
                or _strip_disambiguator(raw.page_title)
            )
        else:
            canonical_name = (
                wd_attrs.get("label_en")
                or wd_attrs.get("label_it")
                or _strip_disambiguator(raw.page_title)
            )

        allow_backfill = not has_copyviol_or_unsourced(flags)
        wiki_source = "itwiki" if raw.wiki_lang == "it" else "wikipedia"

        country_code = wd_attrs.get("country_code")
        city = wd_attrs.get("city")
        founded_year = wd_attrs.get("founded_year")
        closed_year = wd_attrs.get("closed_year")
        website_url = wd_attrs.get("website_url")

        if allow_backfill:
            if country_code is None:
                country_code = infobox.get("country_code")
            if city is None:
                city = infobox.get("city")
            if founded_year is None:
                founded_year = infobox.get("founded_year")
            if closed_year is None:
                closed_year = infobox.get("closed_year")
            if website_url is None:
                website_url = infobox.get("website_url")
            if founded_year is not None and closed_year is not None and closed_year < founded_year:
                closed_year = None

        closure_cause = (infobox.get("closure_cause_text") or "") if allow_backfill else ""
        if wd_attrs.get("replaced_by") or MERGED_CUE_RE.search(closure_cause):
            status = "merged"
        elif closed_year is not None:
            status = "closed"
        else:
            status = "active"
        if status not in ("closed", "merged"):
            closed_year = None

        alias_by_key: dict[str, WikipediaStudioAlias] = {}
        type_priority = {"former_name": 3, "abbreviation": 2, "legal_name": 2, "other": 1}

        def _register_alias(raw_alias: str, raw_type: str | None, source_tag: str) -> None:
            if not raw_type:
                return
            cleaned_alias = clean_text(raw_alias)
            if cleaned_alias is None or cleaned_alias.lower() == canonical_name.lower():
                return
            key = cleaned_alias.lower()
            existing = alias_by_key.get(key)
            if existing is None or type_priority.get(raw_type, 0) > type_priority.get(existing.alias_type, 0):
                alias_by_key[key] = WikipediaStudioAlias(
                    alias_name=cleaned_alias,
                    alias_type=raw_type,
                    source=source_tag,
                )

        if wd_attrs.get("present"):
            for wd_alt in wd_attrs.get("alt_names", ()):
                kind = "abbreviation" if ABBREVIATION_TOKEN_RE.fullmatch(wd_alt) else "other"
                _register_alias(wd_alt, kind, "wikidata")

        if allow_backfill:
            lead_text = infobox.get("lead_text", "")
            stripped_title = _strip_disambiguator(raw.page_title)
            if stripped_title.lower() != canonical_name.lower():
                _register_alias(stripped_title, "other", wiki_source)
            for redirect_title in item.redirect_titles:
                kind = _classify_wikipedia_alias(redirect_title, canonical_name, lead_text)
                _register_alias(redirect_title, kind, wiki_source)
            for match in LEAD_ABBREV_RE.finditer(raw.wikitext[:2500]):
                abbrev = clean_text(match.group(1))
                if abbrev is not None:
                    kind = "abbreviation" if ABBREVIATION_TOKEN_RE.fullmatch(abbrev) else "other"
                    _register_alias(abbrev, kind, wiki_source)

        rels: list[WikipediaStudioRelationship] = []
        seen_rel_tuples: set[tuple[str, str, str | None, str | None]] = set()

        def _add_rel(
            direction: str,
            rel_type: str,
            target_qid: str | None,
            target_name: str | None,
            event_year: int | None,
            source_tag: str,
        ) -> None:
            sig = (direction, rel_type, target_qid, target_name.lower() if target_name else None)
            if sig in seen_rel_tuples:
                return
            seen_rel_tuples.add(sig)
            rels.append(
                WikipediaStudioRelationship(
                    direction=direction,
                    relationship_type=rel_type,
                    target_qid=target_qid,
                    target_name=target_name,
                    event_year=event_year,
                    source=source_tag,
                )
            )

        if wd_attrs.get("present"):
            for target_qid in wd_attrs.get("replaced_by", ()):
                _add_rel("outgoing", "merged_into", target_qid, None, closed_year, "wikidata")
            for target_qid in wd_attrs.get("followed_by", ()):
                _add_rel("outgoing", "succeeded_by", target_qid, None, closed_year, "wikidata")
            for target_qid in wd_attrs.get("parent_orgs", ()):
                _add_rel("outgoing", "subsidiary_of", target_qid, None, None, "wikidata")
            for target_qid in wd_attrs.get("replaces", ()):
                _add_rel("incoming", "merged_into", target_qid, None, founded_year, "wikidata")
            for target_qid in wd_attrs.get("follows", ()):
                _add_rel("incoming", "succeeded_by", target_qid, None, founded_year, "wikidata")
            for target_qid in wd_attrs.get("children", ()):
                _add_rel("incoming", "subsidiary_of", target_qid, None, None, "wikidata")

        if allow_backfill:
            closure_targets = infobox.get("closure_targets", ())
            if closure_targets:
                if ACQUIRED_CUE_RE.search(closure_cause):
                    rel_kind = "acquired_by"
                elif MERGED_CUE_RE.search(closure_cause):
                    rel_kind = "merged_into"
                else:
                    rel_kind = None
                if rel_kind is not None:
                    for target_name in closure_targets:
                        _add_rel("outgoing", rel_kind, None, target_name, closed_year, wiki_source)
            for parent_name in infobox.get("parent_targets", ()):
                _add_rel("outgoing", "subsidiary_of", None, parent_name, None, wiki_source)

        missing_tag = bool(qid and wd_attrs.get("present") and not wd_attrs.get("has_studio_tag"))
        if missing_tag:
            metrics["missing_wikidata_studio_type"] += 1

        records.append(
            WikipediaStudioRecord(
                wiki_lang=raw.wiki_lang,
                page_id=raw.page_id,
                revision_id=raw.revision_id,
                page_title=raw.page_title,
                page_url=raw.page_url,
                wikidata_qid=qid,
                name=canonical_name,
                status=status,
                country_code=country_code,
                city=city,
                founded_year=founded_year,
                closed_year=closed_year,
                website_url=website_url,
                used_wikidata_claims=bool(wd_attrs.get("present")),
                allow_wikitext_backfill=allow_backfill,
                missing_wikidata_studio_type=missing_tag,
                aliases=tuple(alias_by_key[k] for k in sorted(alias_by_key)),
                relationships=tuple(rels),
                compliance_flags=flags,
            )
        )

    return records, flags_by_page_id, metrics


def _record_provenance(
    cur: psycopg.Cursor[dict[str, Any]],
    *,
    entity_table: str,
    entity_id: int,
    source_type: str,
    source_lang: str | None,
    source_license: str,
    source_url: str,
    source_revision_id: str | None,
    attribution_text: str | None,
    run_id: int | None,
) -> int:
    cur.execute(
        """
        INSERT INTO public.provenance_records (
            entity_table, entity_id, field_name, source_type, source_lang,
            source_license, source_url, source_revision_id, attribution_text, etl_run_id
        )
        VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (
            entity_table,
            entity_id,
            source_type,
            source_lang,
            source_license,
            source_url,
            source_revision_id,
            attribution_text,
            run_id,
        ),
    )
    return 1 if cur.fetchone() is not None else 0


def load_wikipedia_studios(
    conn: psycopg.Connection[dict[str, Any]],
    records: list[WikipediaStudioRecord],
    *,
    run_id: int | None = None,
) -> dict[str, int]:
    """Conservatively upsert studios, aliases, relationships, and provenance records."""
    counts = {
        "studios_inserted": 0,
        "studios_updated": 0,
        "aliases_inserted": 0,
        "aliases_updated": 0,
        "relationships_inserted": 0,
        "relationships_updated": 0,
        "provenance_inserted": 0,
    }
    if not records:
        return counts

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, wikidata_qid, name, status, country_code, city,
                   founded_year, closed_year, website_url, primary_source, is_verified
            FROM public.studios
            ORDER BY id
            """
        )
        studios_by_id: dict[int, dict[str, Any]] = {}
        studios_by_qid: dict[str, dict[str, Any]] = {}
        studios_by_name: dict[str, dict[str, Any]] = {}
        studios_by_name_unlinked: dict[str, dict[str, Any]] = {}
        for row in cur.fetchall():
            studios_by_id[row["id"]] = row
            if row["wikidata_qid"] is not None:
                studios_by_qid[row["wikidata_qid"]] = row
            name_key = row["name"].strip().lower()
            studios_by_name.setdefault(name_key, row)
            if row["wikidata_qid"] is None:
                studios_by_name_unlinked.setdefault(name_key, row)

        studio_ids_for_records: list[tuple[WikipediaStudioRecord, int]] = []
        for rec in records:
            wiki_source = "itwiki" if rec.wiki_lang == "it" else "wikipedia"
            name_key = rec.name.strip().lower()
            existing = (
                studios_by_qid.get(rec.wikidata_qid)
                if rec.wikidata_qid is not None
                else None
            )
            if existing is None:
                # Never merge a distinct Wikidata QID into an existing studio with another QID.
                existing = (
                    studios_by_name_unlinked.get(name_key)
                    if rec.wikidata_qid is not None
                    else studios_by_name.get(name_key)
                )

            if existing is not None:
                studio_id = int(existing["id"])
                if existing["is_verified"]:
                    cur.execute(
                        "UPDATE public.studios SET last_seen_at = NOW() WHERE id = %s",
                        (studio_id,),
                    )
                else:
                    new_qid = existing["wikidata_qid"] or rec.wikidata_qid
                    if new_qid is not None and new_qid in studios_by_qid and studios_by_qid[new_qid]["id"] != studio_id:
                        new_qid = existing["wikidata_qid"]
                    new_country = existing["country_code"] if existing["country_code"] is not None else rec.country_code
                    new_city = existing["city"] if existing["city"] is not None else rec.city
                    new_founded = existing["founded_year"] if existing["founded_year"] is not None else rec.founded_year
                    new_closed = existing["closed_year"] if existing["closed_year"] is not None else rec.closed_year
                    if new_founded is not None and new_closed is not None and new_closed < new_founded:
                        new_closed = existing["closed_year"]
                    new_website = existing["website_url"] if existing["website_url"] is not None else rec.website_url
                    new_status = existing["status"]
                    if new_status is None:
                        new_status = rec.status
                    elif new_status == "active" and new_closed is not None and rec.status in ("closed", "merged"):
                        new_status = rec.status
                    if new_status not in ("closed", "merged"):
                        new_closed = None

                    changed = (
                        existing["wikidata_qid"],
                        existing["country_code"],
                        existing["city"],
                        existing["founded_year"],
                        existing["closed_year"],
                        existing["website_url"],
                        existing["status"],
                    ) != (
                        new_qid,
                        new_country,
                        new_city,
                        new_founded,
                        new_closed,
                        new_website,
                        new_status,
                    )
                    if changed:
                        cur.execute(
                            """
                            UPDATE public.studios
                            SET wikidata_qid = %s,
                                country_code = %s,
                                city = %s,
                                founded_year = %s,
                                closed_year = %s,
                                website_url = %s,
                                status = %s,
                                last_seen_at = NOW(),
                                updated_at = NOW()
                            WHERE id = %s
                            """,
                            (
                                new_qid,
                                new_country,
                                new_city,
                                new_founded,
                                new_closed,
                                new_website,
                                new_status,
                                studio_id,
                            ),
                        )
                        existing.update(
                            {
                                "wikidata_qid": new_qid,
                                "country_code": new_country,
                                "city": new_city,
                                "founded_year": new_founded,
                                "closed_year": new_closed,
                                "website_url": new_website,
                                "status": new_status,
                            }
                        )
                        if new_qid is not None:
                            studios_by_qid[new_qid] = existing
                        counts["studios_updated"] += 1
                    else:
                        cur.execute(
                            "UPDATE public.studios SET last_seen_at = NOW() WHERE id = %s",
                            (studio_id,),
                        )
            else:
                cur.execute(
                    """
                    INSERT INTO public.studios (
                        wikidata_qid, name, status, country_code, city,
                        founded_year, closed_year, website_url, primary_source, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    RETURNING id
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
                        wiki_source,
                    ),
                )
                studio_id = int(cur.fetchone()["id"])
                snapshot = {
                    "id": studio_id,
                    "wikidata_qid": rec.wikidata_qid,
                    "name": rec.name,
                    "status": rec.status,
                    "country_code": rec.country_code,
                    "city": rec.city,
                    "founded_year": rec.founded_year,
                    "closed_year": rec.closed_year,
                    "website_url": rec.website_url,
                    "primary_source": wiki_source,
                    "is_verified": False,
                }
                studios_by_id[studio_id] = snapshot
                studios_by_name.setdefault(rec.name.strip().lower(), snapshot)
                if rec.wikidata_qid is not None:
                    studios_by_qid[rec.wikidata_qid] = snapshot
                else:
                    studios_by_name_unlinked.setdefault(rec.name.strip().lower(), snapshot)
                counts["studios_inserted"] += 1

            studio_ids_for_records.append((rec, studio_id))

        # Pass B: aliases
        cur.execute(
            """
            SELECT id, studio_id, alias_name, alias_type, source, is_verified
            FROM public.studio_aliases
            WHERE start_year IS NULL
            """
        )
        existing_aliases: dict[tuple[int, str], dict[str, Any]] = {}
        unique_alias_to_studio: dict[str, int | None] = {}
        for row in cur.fetchall():
            key_lower = row["alias_name"].strip().lower()
            existing_aliases[(row["studio_id"], key_lower)] = row
            if key_lower not in unique_alias_to_studio:
                unique_alias_to_studio[key_lower] = row["studio_id"]
            elif unique_alias_to_studio[key_lower] != row["studio_id"]:
                unique_alias_to_studio[key_lower] = None

        for rec, studio_id in studio_ids_for_records:
            canonical_lower = studios_by_id[studio_id]["name"].strip().lower()
            revision_url = f"https://{rec.wiki_lang}.wikipedia.org/w/index.php?oldid={rec.revision_id}"
            attribution_text = (
                f'Contributors to Wikimedia projects ({rec.wiki_lang}.wikipedia.org, '
                f'"{rec.page_title}", rev {rec.revision_id})'
            )

            for alias in rec.aliases:
                alias_lower = alias.alias_name.strip().lower()
                if alias_lower == canonical_lower:
                    continue
                key = (studio_id, alias_lower)
                current_alias = existing_aliases.get(key)
                alias_id: int

                if current_alias is None:
                    cur.execute(
                        """
                        INSERT INTO public.studio_aliases (
                            studio_id, alias_name, alias_type, start_year, end_year, source, last_seen_at
                        )
                        VALUES (%s, %s, %s, NULL, NULL, %s, NOW())
                        RETURNING id
                        """,
                        (studio_id, alias.alias_name, alias.alias_type, alias.source),
                    )
                    alias_id = int(cur.fetchone()["id"])
                    existing_aliases[key] = {
                        "id": alias_id,
                        "studio_id": studio_id,
                        "alias_name": alias.alias_name,
                        "alias_type": alias.alias_type,
                        "source": alias.source,
                        "is_verified": False,
                    }
                    if alias_lower not in unique_alias_to_studio:
                        unique_alias_to_studio[alias_lower] = studio_id
                    elif unique_alias_to_studio[alias_lower] != studio_id:
                        unique_alias_to_studio[alias_lower] = None
                    counts["aliases_inserted"] += 1
                elif current_alias["is_verified"]:
                    alias_id = int(current_alias["id"])
                    cur.execute(
                        "UPDATE public.studio_aliases SET last_seen_at = NOW() WHERE id = %s",
                        (alias_id,),
                    )
                elif (
                    current_alias["alias_type"] == "other"
                    and alias.alias_type in ("former_name", "abbreviation", "legal_name")
                ):
                    alias_id = int(current_alias["id"])
                    cur.execute(
                        """
                        UPDATE public.studio_aliases
                        SET alias_type = %s, source = %s, last_seen_at = NOW(), updated_at = NOW()
                        WHERE id = %s
                        """,
                        (alias.alias_type, alias.source, alias_id),
                    )
                    current_alias["alias_type"] = alias.alias_type
                    current_alias["source"] = alias.source
                    counts["aliases_updated"] += 1
                else:
                    alias_id = int(current_alias["id"])
                    cur.execute(
                        "UPDATE public.studio_aliases SET last_seen_at = NOW() WHERE id = %s",
                        (alias_id,),
                    )

                if alias.source == "wikidata" and rec.wikidata_qid:
                    counts["provenance_inserted"] += _record_provenance(
                        cur,
                        entity_table="studio_aliases",
                        entity_id=alias_id,
                        source_type="wikidata",
                        source_lang=None,
                        source_license="CC0-1.0",
                        source_url=f"https://www.wikidata.org/wiki/{rec.wikidata_qid}",
                        source_revision_id=None,
                        attribution_text=None,
                        run_id=run_id,
                    )
                else:
                    wiki_source = "itwiki" if rec.wiki_lang == "it" else "wikipedia"
                    counts["provenance_inserted"] += _record_provenance(
                        cur,
                        entity_table="studio_aliases",
                        entity_id=alias_id,
                        source_type=wiki_source,
                        source_lang=rec.wiki_lang,
                        source_license="CC-BY-SA-4.0",
                        source_url=revision_url,
                        source_revision_id=str(rec.revision_id),
                        attribution_text=attribution_text,
                        run_id=run_id,
                    )

        # Pass C: corporate relationships
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

        for rec, studio_id in studio_ids_for_records:
            revision_url = f"https://{rec.wiki_lang}.wikipedia.org/w/index.php?oldid={rec.revision_id}"
            attribution_text = (
                f'Contributors to Wikimedia projects ({rec.wiki_lang}.wikipedia.org, '
                f'"{rec.page_title}", rev {rec.revision_id})'
            )

            for rel in rec.relationships:
                target_id: int | None = None
                if rel.target_qid is not None and rel.target_qid in studios_by_qid:
                    target_id = int(studios_by_qid[rel.target_qid]["id"])
                elif rel.target_name is not None:
                    name_key = rel.target_name.strip().lower()
                    if name_key in studios_by_name:
                        target_id = int(studios_by_name[name_key]["id"])
                    elif unique_alias_to_studio.get(name_key) is not None:
                        target_id = int(unique_alias_to_studio[name_key])  # type: ignore[arg-type]

                if target_id is None or target_id == studio_id:
                    continue

                if rel.direction == "outgoing":
                    src_id, dst_id = studio_id, target_id
                else:
                    src_id, dst_id = target_id, studio_id

                rel_key = (src_id, dst_id, rel.relationship_type)
                current_rel = existing_rels.get(rel_key)
                rel_id: int

                if current_rel is None:
                    cur.execute(
                        """
                        INSERT INTO public.studio_relationships (
                            studio_id, related_studio_id, relationship_type, event_year, source, last_seen_at
                        )
                        VALUES (%s, %s, %s, %s, %s, NOW())
                        RETURNING id
                        """,
                        (src_id, dst_id, rel.relationship_type, rel.event_year, rel.source),
                    )
                    rel_id = int(cur.fetchone()["id"])
                    existing_rels[rel_key] = {
                        "id": rel_id,
                        "studio_id": src_id,
                        "related_studio_id": dst_id,
                        "relationship_type": rel.relationship_type,
                        "event_year": rel.event_year,
                        "source": rel.source,
                        "is_verified": False,
                    }
                    counts["relationships_inserted"] += 1
                elif current_rel["is_verified"]:
                    rel_id = int(current_rel["id"])
                    cur.execute(
                        "UPDATE public.studio_relationships SET last_seen_at = NOW() WHERE id = %s",
                        (rel_id,),
                    )
                elif current_rel["event_year"] is None and rel.event_year is not None:
                    rel_id = int(current_rel["id"])
                    cur.execute(
                        """
                        UPDATE public.studio_relationships
                        SET event_year = %s, last_seen_at = NOW(), updated_at = NOW()
                        WHERE id = %s
                        """,
                        (rel.event_year, rel_id),
                    )
                    current_rel["event_year"] = rel.event_year
                    counts["relationships_updated"] += 1
                else:
                    rel_id = int(current_rel["id"])
                    cur.execute(
                        "UPDATE public.studio_relationships SET last_seen_at = NOW() WHERE id = %s",
                        (rel_id,),
                    )

                if rel.source == "wikidata" and rec.wikidata_qid:
                    counts["provenance_inserted"] += _record_provenance(
                        cur,
                        entity_table="studio_relationships",
                        entity_id=rel_id,
                        source_type="wikidata",
                        source_lang=None,
                        source_license="CC0-1.0",
                        source_url=f"https://www.wikidata.org/wiki/{rec.wikidata_qid}",
                        source_revision_id=None,
                        attribution_text=None,
                        run_id=run_id,
                    )
                else:
                    wiki_source = "itwiki" if rec.wiki_lang == "it" else "wikipedia"
                    counts["provenance_inserted"] += _record_provenance(
                        cur,
                        entity_table="studio_relationships",
                        entity_id=rel_id,
                        source_type=wiki_source,
                        source_lang=rec.wiki_lang,
                        source_license="CC-BY-SA-4.0",
                        source_url=revision_url,
                        source_revision_id=str(rec.revision_id),
                        attribution_text=attribution_text,
                        run_id=run_id,
                    )

        # Pass D: studio entity provenance
        for rec, studio_id in studio_ids_for_records:
            wiki_source = "itwiki" if rec.wiki_lang == "it" else "wikipedia"
            revision_url = f"https://{rec.wiki_lang}.wikipedia.org/w/index.php?oldid={rec.revision_id}"
            attribution_text = (
                f'Contributors to Wikimedia projects ({rec.wiki_lang}.wikipedia.org, '
                f'"{rec.page_title}", rev {rec.revision_id})'
            )
            counts["provenance_inserted"] += _record_provenance(
                cur,
                entity_table="studios",
                entity_id=studio_id,
                source_type=wiki_source,
                source_lang=rec.wiki_lang,
                source_license="CC-BY-SA-4.0",
                source_url=revision_url,
                source_revision_id=str(rec.revision_id),
                attribution_text=attribution_text,
                run_id=run_id,
            )
            if rec.used_wikidata_claims and rec.wikidata_qid:
                counts["provenance_inserted"] += _record_provenance(
                    cur,
                    entity_table="studios",
                    entity_id=studio_id,
                    source_type="wikidata",
                    source_lang=None,
                    source_license="CC0-1.0",
                    source_url=f"https://www.wikidata.org/wiki/{rec.wikidata_qid}",
                    source_revision_id=None,
                    attribution_text=None,
                    run_id=run_id,
                )

    return counts
