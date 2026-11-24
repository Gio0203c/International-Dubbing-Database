"""Polite Wikidata Query Service (WDQS) extractor into staging.wikidata_raw."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from etl.db import transaction

load_dotenv()

logger = logging.getLogger(__name__)

WDQS_ENDPOINT = "https://query.wikidata.org/sparql"
DEFAULT_QUERY_PATH = (
    Path(__file__).resolve().parent / "queries" / "italian_voice_actors.sparql"
)
RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
DEFAULT_TIMEOUT = (10.0, 90.0)


def load_query(query_path: Path = DEFAULT_QUERY_PATH) -> str:
    """Read a standalone .sparql query file from disk."""
    query = query_path.read_text(encoding="utf-8").strip()
    if not query:
        raise ValueError(f"SPARQL query file is empty: {query_path}")
    return query


def get_user_agent() -> str:
    """Return the configured Wikimedia-compliant User-Agent header."""
    user_agent = os.getenv("WIKIDATA_USER_AGENT", "").strip()
    if not user_agent:
        raise ValueError(
            "WIKIDATA_USER_AGENT must be set in the environment or .env file."
        )
    return user_agent


def _parse_retry_after(header_value: str | None) -> float | None:
    if not header_value:
        return None
    try:
        seconds = float(header_value.strip())
    except ValueError:
        return None
    return max(0.0, seconds)


def fetch_sparql_bindings(
    query: str,
    *,
    user_agent: str | None = None,
    endpoint: str = WDQS_ENDPOINT,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    max_retries: int = 4,
    base_delay: float = 2.0,
    session: requests.Session | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Execute a SPARQL query against WDQS with timeouts and exponential backoff."""
    ua = user_agent or get_user_agent()
    headers = {
        "User-Agent": ua,
        "Accept": "application/sparql-results+json",
    }
    client = session or requests.Session()
    owns_session = session is None

    try:
        for attempt in range(max_retries + 1):
            try:
                response = client.post(
                    endpoint,
                    data={"query": query, "format": "json"},
                    headers=headers,
                    timeout=timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= max_retries:
                    raise RuntimeError(
                        f"WDQS request failed after {max_retries + 1} attempts: {exc}"
                    ) from exc
                delay = base_delay * (2**attempt)
                logger.warning(
                    "WDQS network error (%s). Retrying in %.1fs (attempt %d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    max_retries,
                )
                sleep_fn(delay)
                continue

            if response.status_code == 200:
                payload = response.json()
                bindings = payload.get("results", {}).get("bindings")
                if not isinstance(bindings, list):
                    raise ValueError("Unexpected WDQS JSON structure: missing bindings.")
                return bindings

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                delay = (
                    retry_after
                    if retry_after is not None
                    else base_delay * (2**attempt)
                )
                logger.warning(
                    "WDQS returned HTTP %d. Retrying in %.1fs (attempt %d/%d).",
                    response.status_code,
                    delay,
                    attempt + 1,
                    max_retries,
                )
                sleep_fn(delay)
                continue

            response.raise_for_status()

        raise RuntimeError("WDQS extraction exhausted retries unexpectedly.")
    finally:
        if owns_session:
            client.close()


def extract_subject_qid(binding: dict[str, Any]) -> str:
    """Extract the raw QID (or fallback identifier) from a SPARQL binding."""
    raw_person = str(binding.get("person", {}).get("value", "")).strip()
    if not raw_person:
        return "UNKNOWN"
    return raw_person.rsplit("/", 1)[-1] or raw_person


def compute_payload_hash(binding: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 hash of a raw WDQS binding.

    Pipe-delimited GROUP_CONCAT values are sorted in a normalized copy before
    hashing so arbitrary SPARQL aggregation order does not change the hash.
    """
    normalized: dict[str, Any] = {}
    for key in sorted(binding.keys()):
        cell = binding[key]
        if isinstance(cell, dict) and isinstance(cell.get("value"), str):
            value = cell["value"]
            if "|" in value:
                value = "|".join(sorted(value.split("|")))
            normalized[key] = {**cell, "value": value}
        else:
            normalized[key] = cell
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def save_raw_bindings(
    conn: psycopg.Connection[dict[str, Any]],
    bindings: list[dict[str, Any]],
    *,
    run_id: int | None = None,
) -> int:
    """Insert raw WDQS bindings into staging.wikidata_raw."""
    if not bindings:
        return 0

    rows = [
        (
            run_id,
            extract_subject_qid(binding),
            Jsonb(binding),
            compute_payload_hash(binding),
        )
        for binding in bindings
    ]
    sql = """
        INSERT INTO staging.wikidata_raw (run_id, wikidata_qid, payload, payload_hash)
        VALUES (%s, %s, %s, %s)
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def extract_to_staging(
    *,
    query_path: Path = DEFAULT_QUERY_PATH,
    run_id: int | None = None,
    conn: psycopg.Connection[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Run the WDQS query and archive raw JSONB rows in staging.wikidata_raw."""
    query = load_query(query_path)
    logger.info("Fetching Wikidata bindings using %s", query_path)
    bindings = fetch_sparql_bindings(query)
    logger.info("Fetched %d rows from WDQS.", len(bindings))

    if dry_run:
        return bindings

    with transaction(conn) as active_conn:
        inserted = save_raw_bindings(active_conn, bindings, run_id=run_id)
    logger.info("Archived %d raw rows into staging.wikidata_raw.", inserted)
    return bindings


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Wikidata voice actor rows into staging.wikidata_raw."
    )
    parser.add_argument(
        "--query",
        type=Path,
        default=DEFAULT_QUERY_PATH,
        help="Path to the .sparql query file.",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="Optional public.etl_runs.id to associate with staged rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query WDQS without writing to PostgreSQL.",
    )
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_arg_parser().parse_args()
    extract_to_staging(
        query_path=args.query,
        run_id=args.run_id,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
