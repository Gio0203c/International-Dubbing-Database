"""CLI runner for multi-language Wikipedia voice-actor ingestion."""

from __future__ import annotations

import argparse
import logging
from typing import Any

import requests
from dotenv import load_dotenv

from etl.db import transaction
from etl.run_wikidata_sync import (
    DEFAULT_MIN_VOLUME_RATIO,
    CircuitBreakerError,
    check_circuit_breaker,
    create_etl_run,
    finish_etl_run,
    get_previous_successful_count,
)
from etl.wikipedia.extract import (
    DEFAULT_VOICE_ACTOR_CATEGORIES,
    fetch_category_titles,
    fetch_pages_batch,
    save_raw_pages,
)
from etl.wikipedia.load import load_wikipedia_speakers
from etl.wikipedia.transform import transform_wikipedia_pages

LOGGER = logging.getLogger(__name__)


def run_wikipedia_sync(
    *,
    wiki_lang: str = "it",
    categories: tuple[str, ...] | None = None,
    limit: int | None = None,
    min_volume_ratio: float = DEFAULT_MIN_VOLUME_RATIO,
    force: bool = False,
) -> dict[str, Any]:
    """Run one full multi-language Wikipedia ETL cycle into staging and public."""
    lang = wiki_lang.strip().lower()
    target_categories = categories or DEFAULT_VOICE_ACTOR_CATEGORIES.get(lang)
    if not target_categories:
        raise ValueError(
            f"No default categories configured for wiki_lang={lang!r}; pass --categories explicitly."
        )

    source = f"wikipedia_{lang}"
    with transaction() as conn:
        run_id = create_etl_run(conn, source)
        previous_count = get_previous_successful_count(conn, source)

    try:
        with requests.Session() as session:
            titles = fetch_category_titles(lang, target_categories, limit=limit, session=session)
            pages = fetch_pages_batch(lang, titles, session=session)
        if not pages:
            raise CircuitBreakerError(f"{source}: 0 pages extracted; refusing to proceed.")

        cb_error = check_circuit_breaker(
            len(pages),
            previous_count,
            min_ratio=min_volume_ratio,
            force=force,
        )
        if cb_error is not None:
            raise CircuitBreakerError(f"{source}: {cb_error}")

        records, flags_by_page_id, metrics = transform_wikipedia_pages(pages)

        with transaction() as conn:
            raw_counts = save_raw_pages(conn, run_id, pages, flags_by_page_id)
            load_counts = load_wikipedia_speakers(conn, records, run_id=run_id)
            rows_inserted = (
                load_counts["speakers_inserted"]
                + load_counts["ext_ids_inserted"]
                + load_counts["provenance_inserted"]
            )
            rows_updated = load_counts["speakers_updated"]
            rows_discarded = (
                metrics["copyviol_blocked"]
                + metrics["non_person_skipped"]
                + load_counts["skipped_unconfirmed"]
            )
            finish_etl_run(
                conn,
                run_id,
                status="succeeded",
                rows_extracted=len(pages),
                rows_inserted=rows_inserted,
                rows_updated=rows_updated,
                rows_discarded=rows_discarded,
            )

        LOGGER.info(
            "%s (run_id=%s): extracted=%s staging=%s load=%s metrics=%s",
            source,
            run_id,
            len(pages),
            raw_counts,
            load_counts,
            metrics,
        )
        return {
            "source": source,
            "run_id": run_id,
            "rows_extracted": len(pages),
            "rows_inserted": rows_inserted,
            "rows_updated": rows_updated,
            "rows_discarded": rows_discarded,
            "staging": raw_counts,
            "load": load_counts,
            "metrics": metrics,
        }
    except CircuitBreakerError as exc:
        with transaction() as conn:
            finish_etl_run(conn, run_id, status="aborted_circuit_breaker", error_message=str(exc))
        raise
    except Exception as exc:
        with transaction() as conn:
            finish_etl_run(conn, run_id, status="failed", error_message=str(exc))
        raise


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Sync voice actor articles from Wikipedia Action API.")
    parser.add_argument("--lang", default="it", help="Wikipedia language edition code (default: it).")
    parser.add_argument(
        "--categories",
        default=None,
        help="Optional pipe-separated Wikipedia category titles overriding defaults.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Optional cap on titles fetched.")
    parser.add_argument(
        "--min-volume-ratio",
        type=float,
        default=DEFAULT_MIN_VOLUME_RATIO,
        help="Minimum volume ratio relative to previous successful run.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the extraction volume circuit breaker.",
    )
    args = parser.parse_args()

    cat_tuple = (
        tuple(c.strip() for c in args.categories.split("|") if c.strip())
        if args.categories
        else None
    )
    run_wikipedia_sync(
        wiki_lang=args.lang,
        categories=cat_tuple,
        limit=args.limit,
        min_volume_ratio=args.min_volume_ratio,
        force=args.force,
    )


if __name__ == "__main__":
    main()
