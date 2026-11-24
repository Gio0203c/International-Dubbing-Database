"""End-to-end Wikidata sync runner for public.speaker_relationships."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
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
from etl.wikidata.extract import fetch_sparql_bindings, load_query
from etl.wikidata.relationships import (
    DEFAULT_RELATIONSHIPS_QUERY_PATH,
    RelationshipLoadResult,
    load_relationships,
    transform_relationship_bindings,
)

load_dotenv()

logger = logging.getLogger(__name__)

SOURCE_NAME = "wikidata_relationships"


@dataclass(frozen=True)
class RelationshipsSyncSummary:
    """Outcome summary of a single speaker relationships sync execution."""

    run_id: int
    status: str
    rows_extracted: int
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    rows_discarded: int
    unmatched_speakers: int


def run_relationships_sync(
    *,
    query_path: Path = DEFAULT_RELATIONSHIPS_QUERY_PATH,
    min_volume_ratio: float = DEFAULT_MIN_VOLUME_RATIO,
    force: bool = False,
    bindings_override: list[dict[str, Any]] | None = None,
) -> RelationshipsSyncSummary:
    """Run Extract -> Transform -> Circuit Breaker -> Load -> Audit for relationships."""
    with transaction() as conn:
        run_id = create_etl_run(conn, SOURCE_NAME)

    rows_extracted = 0
    rows_discarded = 0

    try:
        if bindings_override is None:
            query = load_query(query_path)
            logger.info(
                "Starting Wikidata relationships sync run_id=%d using %s",
                run_id,
                query_path,
            )
            bindings = fetch_sparql_bindings(query)
        else:
            bindings = list(bindings_override)

        rows_extracted = len(bindings)
        transform_res = transform_relationship_bindings(bindings)
        rows_discarded = transform_res.discarded_count
        valid_records = transform_res.records
        logger.info(
            "Transformed %d unique kinship edges from %d raw statements (%d discarded, %d duplicates merged).",
            len(valid_records),
            rows_extracted,
            rows_discarded,
            transform_res.duplicate_count,
        )

        with transaction() as conn:
            previous_count = get_previous_successful_count(conn, SOURCE_NAME)

        abort_reason = (
            "Extracted zero valid relationship records from Wikidata."
            if not valid_records
            else check_circuit_breaker(
                rows_extracted,
                previous_count,
                min_ratio=min_volume_ratio,
                force=force,
            )
        )
        if abort_reason is not None and not force:
            logger.error(
                "Circuit breaker aborted relationships run_id=%d: %s",
                run_id,
                abort_reason,
            )
            with transaction() as conn:
                finish_etl_run(
                    conn,
                    run_id,
                    status="aborted_circuit_breaker",
                    rows_extracted=rows_extracted,
                    rows_discarded=rows_discarded,
                    error_message=abort_reason,
                )
            raise CircuitBreakerError(abort_reason)

        with transaction() as conn:
            load_res: RelationshipLoadResult = load_relationships(conn, valid_records)
            finish_etl_run(
                conn,
                run_id,
                status="succeeded",
                rows_extracted=rows_extracted,
                rows_inserted=load_res.inserted,
                rows_updated=load_res.updated,
                rows_discarded=rows_discarded,
            )

        summary = RelationshipsSyncSummary(
            run_id=run_id,
            status="succeeded",
            rows_extracted=rows_extracted,
            rows_inserted=load_res.inserted,
            rows_updated=load_res.updated,
            rows_unchanged=load_res.unchanged,
            rows_discarded=rows_discarded,
            unmatched_speakers=load_res.unmatched_speakers,
        )
        logger.info(
            "Relationships sync run_id=%d succeeded: extracted=%d inserted=%d updated=%d unchanged=%d discarded=%d unmatched=%d",
            summary.run_id,
            summary.rows_extracted,
            summary.rows_inserted,
            summary.rows_updated,
            summary.rows_unchanged,
            summary.rows_discarded,
            summary.unmatched_speakers,
        )
        return summary

    except CircuitBreakerError:
        raise
    except Exception as exc:
        logger.exception("Relationships sync run_id=%d failed: %s", run_id, exc)
        with transaction() as conn:
            finish_etl_run(
                conn,
                run_id,
                status="failed",
                rows_extracted=rows_extracted,
                rows_discarded=rows_discarded,
                error_message=str(exc),
            )
        raise


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync speaker kinship links from Wikidata into public.speaker_relationships."
    )
    parser.add_argument(
        "--query",
        type=Path,
        default=DEFAULT_RELATIONSHIPS_QUERY_PATH,
        help="Path to the .sparql relationships query file.",
    )
    parser.add_argument(
        "--min-volume-ratio",
        type=float,
        default=DEFAULT_MIN_VOLUME_RATIO,
        help="Minimum allowed ratio vs previous successful run (default: 0.70).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the volume circuit breaker for deliberate manual runs.",
    )
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_arg_parser().parse_args()
    try:
        run_relationships_sync(
            query_path=args.query,
            min_volume_ratio=args.min_volume_ratio,
            force=args.force,
        )
    except CircuitBreakerError:
        sys.exit(2)


if __name__ == "__main__":
    main()
