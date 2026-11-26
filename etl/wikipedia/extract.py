"""Polite MediaWiki Action API extractor into staging.wikipedia_raw."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
import requests
from dotenv import load_dotenv

from etl.wikidata.extract import get_user_agent

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_VOICE_ACTOR_CATEGORIES: dict[str, tuple[str, ...]] = {
    "it": (
        "Categoria:Doppiatori italiani del XX secolo",
        "Categoria:Doppiatori italiani del XXI secolo",
        "Categoria:Direttori del doppiaggio italiani",
        "Categoria:Dialoghisti italiani",
    ),
    "en": (
        "Category:English voice actors",
        "Category:American voice actors",
    ),
    "ja": ("Category:日本の声優",),
}

RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
DEFAULT_TIMEOUT = (10.0, 60.0)


@dataclass(frozen=True)
class RawWikipediaPage:
    """Single Wikipedia article revision retrieved from the MediaWiki Action API."""

    wiki_lang: str
    page_id: int
    revision_id: int
    page_title: str
    page_url: str
    wikidata_qid: str | None
    wikitext: str
    content_hash: str


def get_wikipedia_user_agent() -> str:
    """Return the configured Wikimedia User-Agent header."""
    ua = os.getenv("WIKIMEDIA_USER_AGENT", "").strip()
    return ua if ua else get_user_agent()


def _parse_retry_after(header_value: str | None) -> float | None:
    if not header_value:
        return None
    try:
        return max(0.0, float(header_value.strip()))
    except ValueError:
        return None


def _mediawiki_get(
    endpoint: str,
    params: dict[str, Any],
    *,
    user_agent: str | None = None,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    max_retries: int = 4,
    base_delay: float = 2.0,
    session: requests.Session | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    ua = user_agent or get_wikipedia_user_agent()
    headers = {
        "User-Agent": ua,
        "Accept": "application/json",
    }
    query_params = {
        **params,
        "format": "json",
        "formatversion": "2",
        "maxlag": "5",
    }
    client = session or requests.Session()
    owns_session = session is None

    try:
        for attempt in range(max_retries + 1):
            try:
                response = client.get(
                    endpoint,
                    params=query_params,
                    headers=headers,
                    timeout=timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= max_retries:
                    raise RuntimeError(
                        f"MediaWiki request failed after {max_retries + 1} attempts: {exc}"
                    ) from exc
                delay = base_delay * (2**attempt)
                logger.warning("MediaWiki network error (%s). Retrying in %.1fs.", exc, delay)
                sleep_fn(delay)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES:
                if attempt >= max_retries:
                    response.raise_for_status()
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else base_delay * (2**attempt)
                logger.warning(
                    "MediaWiki HTTP %s. Retrying in %.1fs.",
                    response.status_code,
                    delay,
                )
                sleep_fn(delay)
                continue

            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("error", {}).get("code") == "maxlag":
                if attempt >= max_retries:
                    raise RuntimeError(f"MediaWiki maxlag exceeded after retries: {payload['error']}")
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else base_delay * (2**attempt)
                logger.warning("MediaWiki maxlag triggered. Retrying in %.1fs.", delay)
                sleep_fn(delay)
                continue

            return payload
    finally:
        if owns_session:
            client.close()

    raise RuntimeError("Unreachable MediaWiki retry exit.")


def fetch_category_titles(
    wiki_lang: str,
    categories: tuple[str, ...],
    *,
    limit: int | None = None,
    user_agent: str | None = None,
    session: requests.Session | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Fetch deduplicated sorted article titles from the given Wikipedia categories."""
    endpoint = f"https://{wiki_lang}.wikipedia.org/w/api.php"
    titles: set[str] = set()

    for category in categories:
        cmcontinue: str | None = None
        while True:
            params: dict[str, Any] = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": category,
                "cmnamespace": "0",
                "cmtype": "page",
                "cmlimit": "500",
            }
            if cmcontinue:
                params["cmcontinue"] = cmcontinue

            payload = _mediawiki_get(
                endpoint,
                params,
                user_agent=user_agent,
                session=session,
                sleep_fn=sleep_fn,
            )
            members = payload.get("query", {}).get("categorymembers", [])
            for item in members:
                title = item.get("title")
                if isinstance(title, str) and title.strip():
                    titles.add(title.strip())
                    if limit is not None and len(titles) >= limit:
                        return sorted(titles)[:limit]

            cont = payload.get("continue", {}).get("cmcontinue")
            if not cont:
                break
            cmcontinue = str(cont)

    ordered = sorted(titles)
    return ordered[:limit] if limit is not None else ordered


def fetch_pages_batch(
    wiki_lang: str,
    titles: list[str],
    *,
    batch_size: int = 50,
    user_agent: str | None = None,
    session: requests.Session | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[RawWikipediaPage]:
    """Fetch revision wikitext, page URLs, and Wikidata QIDs in batches of up to 50 titles."""
    endpoint = f"https://{wiki_lang}.wikipedia.org/w/api.php"
    by_page_id: dict[int, RawWikipediaPage] = {}

    for idx in range(0, len(titles), batch_size):
        chunk = titles[idx : idx + batch_size]
        if not chunk:
            continue
        payload = _mediawiki_get(
            endpoint,
            {
                "action": "query",
                "prop": "revisions|pageprops|info",
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
                "ppprop": "wikibase_item",
                "inprop": "url",
                "redirects": "1",
                "titles": "|".join(chunk),
            },
            user_agent=user_agent,
            session=session,
            sleep_fn=sleep_fn,
        )
        pages = payload.get("query", {}).get("pages", [])
        for page in pages:
            if page.get("missing"):
                continue
            page_id = page.get("pageid")
            page_title = page.get("title")
            revisions = page.get("revisions") or []
            if not isinstance(page_id, int) or page_id <= 0 or not page_title or not revisions:
                continue

            rev = revisions[0]
            rev_id = rev.get("revid")
            wikitext = (
                rev.get("slots", {}).get("main", {}).get("content")
                if isinstance(rev.get("slots"), dict)
                else rev.get("content")
            )
            if not isinstance(rev_id, int) or rev_id <= 0 or not isinstance(wikitext, str):
                continue

            page_url = page.get("fullurl") or (
                f"https://{wiki_lang}.wikipedia.org/wiki/{page_title.replace(' ', '_')}"
            )
            raw_qid = (
                page.get("pageprops", {}).get("wikibase_item")
                if isinstance(page.get("pageprops"), dict)
                else None
            )
            wikidata_qid = str(raw_qid).strip() if raw_qid else None
            content_hash = hashlib.sha256(wikitext.encode("utf-8")).hexdigest()

            by_page_id[page_id] = RawWikipediaPage(
                wiki_lang=wiki_lang,
                page_id=page_id,
                revision_id=rev_id,
                page_title=str(page_title).strip(),
                page_url=str(page_url).strip(),
                wikidata_qid=wikidata_qid,
                wikitext=wikitext,
                content_hash=content_hash,
            )

    return [by_page_id[pid] for pid in sorted(by_page_id)]


def save_raw_pages(
    conn: psycopg.Connection[dict[str, Any]],
    run_id: int | None,
    pages: list[RawWikipediaPage],
    flags_by_page_id: dict[int, tuple[str, ...]],
) -> dict[str, int]:
    """Idempotently archive Wikipedia revisions in staging.wikipedia_raw."""
    inserted = 0
    unchanged = 0

    with conn.cursor() as cur:
        for page in pages:
            flags = list(flags_by_page_id.get(page.page_id, ()))
            flagged = any(
                flag in {"copyviol_template", "unsourced_template"}
                for flag in flags
            )
            cur.execute(
                """
                INSERT INTO staging.wikipedia_raw (
                    run_id, wiki_lang, page_id, revision_id,
                    page_title, page_url, wikidata_qid, wikitext,
                    content_hash, has_copyviol_or_unsourced_flag, compliance_flags
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (wiki_lang, page_id, revision_id) DO NOTHING
                RETURNING id
                """,
                (
                    run_id,
                    page.wiki_lang,
                    page.page_id,
                    page.revision_id,
                    page.page_title,
                    page.page_url,
                    page.wikidata_qid,
                    page.wikitext,
                    page.content_hash,
                    flagged,
                    flags,
                ),
            )
            if cur.fetchone() is not None:
                inserted += 1
            else:
                unchanged += 1

    return {"inserted": inserted, "unchanged": unchanged}
