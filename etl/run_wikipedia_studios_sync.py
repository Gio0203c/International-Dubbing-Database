"""CLI runner for multi-language Wikipedia + Wikidata studio enrichment."""

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
from etl.wikipedia.extract import save_raw_pages
from etl.wikipedia.studios import (
    discover_studio_titles,
    fetch_studio_pages_batch,
    fetch_wikidata_studio_entities,
    load_wikipedia_studios,
    transform_wikipedia_studio_pages,
)

LOGGER = logging.getLogger(__name__)


def run_wikipedia_studios_sync(
    *,
    wiki_lang: str = "it",
    categories: tuple[str, ...] | None = None,
    include_infobox_search: bool = True,
    limit: int | None = None,
    min_volume_ratio: float = DEFAULT_MIN_VOLUME_RATIO,
    force: bool = False,
) -> dict[str, Any]:
    """Run one full multi-language Wikipedia studio ETL cycle into staging and public."""
    lang = wiki_lang.strip().lower()
    source = f"wikipedia_studios_{lang}"

    with transaction() as conn:
        run_id = create_etl_run(conn, source)
        previous_count = get_previous_successful_count(conn, source)

    try:
        with requests.Session() as session:
            titles, category_titles = discover_studio_titles(
                lang,
                categories,
                include_infobox_search=include_infobox_search,
                limit=limit,
                session=session,
            )
            studio_pages = fetch_studio_pages_batch(
                lang,
                titles,
                category_titles,
                session=session,
            )
            if not studio_pages:
                raise CircuitBreakerError(f"{source}: 0 studio pages extracted; refusing to proceed.")

            cb_error = check_circuit_breaker(
                len(studio_pages),
                previous_count,
                min_ratio=min_volume_ratio,
                force=force,
            )
            if cb_error is not None:
                raise CircuitBreakerError(f"{source}: {cb_error}")

            qids = [
                item.page.wikidata_qid
                for item in studio_pages
                if item.page.wikidata_qid is not None
            ]
            studio_entities, aux_entities = fetch_wikidata_studio_entities(qids, session=session)

        records, flags_by_page_id, metrics = transform_wikipedia_studio_pages(
            studio_pages,
            studio_entities,
            aux_entities,
        )
        raw_pages = [item.page for item in studio_pages]

        with transaction() as conn:
            raw_counts = save_raw_pages(conn, run_id, raw_pages, flags_by_page_id)
            load_counts = load_wikipedia_studios(conn, records, run_id=run_id)
            rows_inserted = (
                load_counts["studios_inserted"]
                + load_counts["aliases_inserted"]
                + load_counts["relationships_inserted"]
                + load_counts["provenance_inserted"]
            )
            rows_updated = (
                load_counts["studios_updated"]
                + load_counts["aliases_updated"]
                + load_counts["relationships_updated"]
            )
            rows_discarded = metrics["copyviol_blocked"] + metrics["non_studio_skipped"]
            finish_etl_run(
                conn,
                run_id,
                status="succeeded",
                rows_extracted=len(studio_pages),
                rows_inserted=rows_inserted,
                rows_updated=rows_updated,
                rows_discarded=rows_discarded,
            )

        missing_studio_qids = sorted(
            ((rec.wikidata_qid, rec.name) for rec in records if rec.missing_wikidata_studio_type and rec.wikidata_qid),
            key=lambda pair: (pair[1].lower(), pair[0]),
        )
        LOGGER.info(
            "%s (run_id=%s): extracted=%s staging=%s load=%s metrics=%s missing_wd_type=%s",
            source,
            run_id,
            len(studio_pages),
            raw_counts,
            load_counts,
            metrics,
            missing_studio_qids,
        )
        return {
            "source": source,
            "run_id": run_id,
            "rows_extracted": len(studio_pages),
            "rows_inserted": rows_inserted,
            "rows_updated": rows_updated,
            "rows_discarded": rows_discarded,
            "staging": raw_counts,
            "load": load_counts,
            "metrics": metrics,
            "missing_wikidata_studio_type_qids": missing_studio_qids,
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

    parser = argparse.ArgumentParser(description="Sync dubbing studios from Wikipedia and Wikidata.")
    parser.add_argument("--lang", default="it", help="Wikipedia language edition code (default: it).")
    parser.add_argument(
        "--categories",
        default=None,
        help="Optional pipe-separated Wikipedia category titles overriding defaults.",
    )
    parser.add_argument(
        "--no-infobox-search",
        action="store_true",
        help="Disable supplemental Italian {{Azienda}} infobox search.",
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
    run_wikipedia_studios_sync(
        wiki_lang=args.lang,
        categories=cat_tuple,
        include_infobox_search=not args.no_infobox_search,
        limit=args.limit,
        min_volume_ratio=args.min_volume_ratio,
        force=args.force,
    )


if __name__ == "__main__":
    main()
