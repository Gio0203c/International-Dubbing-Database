"""CLI runner for syncing Wikidata studios, works, characters, and voice credits."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

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
from etl.wikidata.dubbing import (
    load_characters,
    load_dubbing_credits,
    load_media_sources,
    load_studios,
    transform_character_bindings,
    transform_credit_bindings,
    transform_studio_bindings,
    transform_work_bindings,
)
from etl.wikidata.extract import fetch_sparql_bindings, load_query

LOGGER = logging.getLogger(__name__)
QUERIES_DIR = Path(__file__).resolve().parent / "wikidata" / "queries"

STAGE_ORDER = ("studios", "works", "characters", "credits")
STAGE_CONFIG = {
    "studios": ("wikidata_studios", QUERIES_DIR / "dubbing_studios.sparql"),
    "works": ("wikidata_works", QUERIES_DIR / "italian_dubbing_works.sparql"),
    "characters": ("wikidata_characters", QUERIES_DIR / "italian_dubbing_characters.sparql"),
    "credits": ("wikidata_credits", QUERIES_DIR / "italian_dubbing_credits.sparql"),
}


def _run_stage(
    stage: str,
    *,
    min_volume_ratio: float,
    force: bool,
    italian_titles_by_qid: dict[str, str],
) -> dict[str, Any]:
    source, query_path = STAGE_CONFIG[stage]

    with transaction() as conn:
        run_id = create_etl_run(conn, source)
        previous_count = get_previous_successful_count(conn, source)

    try:
        query_text = load_query(query_path)
        bindings = fetch_sparql_bindings(query_text)

        if stage == "studios":
            records, discarded = transform_studio_bindings(bindings)
        elif stage == "works":
            records, discarded = transform_work_bindings(bindings)
            for rec in records:
                if rec.italian_title:
                    italian_titles_by_qid[rec.wikidata_qid] = rec.italian_title
        elif stage == "characters":
            records, discarded = transform_character_bindings(bindings)
        else:
            records, discarded = transform_credit_bindings(bindings)

        if len(records) == 0:
            raise CircuitBreakerError(f"{source}: 0 valid records extracted; refusing to proceed.")
        cb_error = check_circuit_breaker(
            len(bindings),
            previous_count,
            min_ratio=min_volume_ratio,
            force=force,
        )
        if cb_error is not None:
            raise CircuitBreakerError(f"{source}: {cb_error}")

        with transaction() as conn:
            if stage == "studios":
                counts = load_studios(conn, records)
                rows_inserted = (
                    counts["inserted"]
                    + counts["aliases_inserted"]
                    + counts["relationships_inserted"]
                )
                rows_updated = (
                    counts["updated"]
                    + counts["aliases_updated"]
                    + counts["relationships_updated"]
                )
                total_discarded = discarded
            elif stage == "works":
                counts = load_media_sources(conn, records)
                rows_inserted = counts["inserted"]
                rows_updated = counts["updated"]
                total_discarded = discarded
            elif stage == "characters":
                counts = load_characters(conn, records)
                rows_inserted = counts["inserted"]
                rows_updated = counts["updated"]
                total_discarded = discarded
            else:
                counts = load_dubbing_credits(
                    conn,
                    records,
                    italian_titles_by_qid=italian_titles_by_qid,
                )
                rows_inserted = counts["editions_inserted"] + counts["credits_inserted"]
                rows_updated = counts["editions_updated"] + counts["credits_updated"]
                total_discarded = discarded + counts["unresolved"]

            finish_etl_run(
                conn,
                run_id,
                status="succeeded",
                rows_extracted=len(bindings),
                rows_inserted=rows_inserted,
                rows_updated=rows_updated,
                rows_discarded=total_discarded,
            )

        LOGGER.info(
            "%s (run_id=%s): extracted=%s inserted=%s updated=%s discarded=%s details=%s",
            source,
            run_id,
            len(bindings),
            rows_inserted,
            rows_updated,
            total_discarded,
            counts,
        )
        return {
            "source": source,
            "run_id": run_id,
            "rows_extracted": len(bindings),
            "rows_inserted": rows_inserted,
            "rows_updated": rows_updated,
            "rows_discarded": total_discarded,
            "details": counts,
        }
    except Exception as exc:
        with transaction() as conn:
            finish_etl_run(conn, run_id, status="failed", error_message=str(exc))
        raise


def run_dubbing_sync(
    *,
    only: str = "all",
    min_volume_ratio: float = DEFAULT_MIN_VOLUME_RATIO,
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    """Run selected dubbing ETL stages against Wikidata and PostgreSQL."""
    stages = STAGE_ORDER if only == "all" else (only,)
    italian_titles_by_qid: dict[str, str] = {}
    summary: dict[str, dict[str, Any]] = {}

    for stage in stages:
        summary[stage] = _run_stage(
            stage,
            min_volume_ratio=min_volume_ratio,
            force=force,
            italian_titles_by_qid=italian_titles_by_qid,
        )
    return summary


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Sync Wikidata dubbing studios, works, characters, and credits.")
    parser.add_argument(
        "--only",
        choices=("all", *STAGE_ORDER),
        default="all",
        help="Run a single dubbing ETL stage or all stages in dependency order.",
    )
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
    run_dubbing_sync(only=args.only, min_volume_ratio=args.min_volume_ratio, force=args.force)


if __name__ == "__main__":
    main()
