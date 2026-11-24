"""End-to-end Wikidata sync runner with audit logging and circuit breaker."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv

from etl.db import transaction
from etl.wikidata.extract import (
    DEFAULT_QUERY_PATH,
    fetch_sparql_bindings,
    load_query,
    save_raw_bindings,
)
from etl.wikidata.load import LoadResult, load_speakers
from etl.wikidata.transform import transform_bindings

load_dotenv()

logger = logging.getLogger(__name__)

SOURCE_NAME = "wikidata"
DEFAULT_MIN_VOLUME_RATIO = 0.70


class CircuitBreakerError(RuntimeError):
    """Raised when extracted volume drops below the configured safety threshold."""


@dataclass(frozen=True)
class SyncSummary:
    """Outcome summary of a single Wikidata sync execution."""

    run_id: int
    status: str
    rows_extracted: int
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    rows_discarded: int
    bridge_conflicts_skipped: int


def create_etl_run(conn: psycopg.Connection[dict[str, Any]], source: str) -> int:
    """Insert a new 'running' audit row into public.etl_runs and return its id."""
    row = conn.execute(
        """
        INSERT INTO public.etl_runs (source, status)
        VALUES (%s, 'running')
        RETURNING id
        """,
        (source,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def finish_etl_run(
    conn: psycopg.Connection[dict[str, Any]],
    run_id: int,
    *,
    status: str,
    rows_extracted: int = 0,
    rows_inserted: int = 0,
    rows_updated: int = 0,
    rows_discarded: int = 0,
    error_message: str | None = None,
) -> None:
    """Update an existing public.etl_runs row with final counts and status."""
    conn.execute(
        """
        UPDATE public.etl_runs
        SET
            status = %s,
            finished_at = NOW(),
            rows_extracted = %s,
            rows_inserted = %s,
            rows_updated = %s,
            rows_discarded = %s,
            error_message = %s
        WHERE id = %s
        """,
        (
            status,
            rows_extracted,
            rows_inserted,
            rows_updated,
            rows_discarded,
            error_message,
            run_id,
        ),
    )


def get_previous_successful_count(
    conn: psycopg.Connection[dict[str, Any]],
    source: str,
) -> int | None:
    """Return rows_extracted from the latest succeeded run for source, if any."""
    row = conn.execute(
        """
        SELECT rows_extracted
        FROM public.etl_runs
        WHERE source = %s
          AND status = 'succeeded'
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (source,),
    ).fetchone()
    return int(row["rows_extracted"]) if row is not None else None


def check_circuit_breaker(
    current_valid_count: int,
    previous_count: int | None,
    *,
    min_ratio: float = DEFAULT_MIN_VOLUME_RATIO,
    force: bool = False,
) -> str | None:
    """Return an abort reason when extracted volume drops abnormally, else None."""
    if force:
        return None
    if current_valid_count <= 0:
        return "Extracted zero valid speaker records from Wikidata."
    if previous_count is not None and previous_count > 0:
        min_allowed = int(previous_count * min_ratio)
        if current_valid_count < min_allowed:
            return (
                f"Volume drop detected: got {current_valid_count} valid records, "
                f"below {min_ratio:.0%} threshold ({min_allowed}) of previous "
                f"successful run ({previous_count})."
            )
    return None


def run_sync(
    *,
    query_path: Path = DEFAULT_QUERY_PATH,
    min_volume_ratio: float = DEFAULT_MIN_VOLUME_RATIO,
    force: bool = False,
    bindings_override: list[dict[str, Any]] | None = None,
) -> SyncSummary:
    """Run Extract -> Staging -> Transform -> Circuit Breaker -> Load -> Audit."""
    with transaction() as conn:
        run_id = create_etl_run(conn, SOURCE_NAME)

    rows_extracted = 0
    rows_discarded = 0

    try:
        if bindings_override is None:
            query = load_query(query_path)
            logger.info("Starting Wikidata sync run_id=%d using %s", run_id, query_path)
            bindings = fetch_sparql_bindings(query)
        else:
            bindings = list(bindings_override)

        rows_extracted = len(bindings)
        logger.info("Extracted %d raw rows from Wikidata.", rows_extracted)

        with transaction() as conn:
            save_raw_bindings(conn, bindings, run_id=run_id)
            previous_count = get_previous_successful_count(conn, SOURCE_NAME)

        transform_res = transform_bindings(bindings)
        rows_discarded = transform_res.discarded_count
        valid_records = transform_res.records
        logger.info(
            "Transformed %d valid records (%d discarded, %d duplicate QIDs).",
            len(valid_records),
            rows_discarded,
            transform_res.duplicate_qid_count,
        )

        abort_reason = check_circuit_breaker(
            len(valid_records),
            previous_count,
            min_ratio=min_volume_ratio,
            force=force,
        )
        if abort_reason is not None:
            logger.error("Circuit breaker aborted run_id=%d: %s", run_id, abort_reason)
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
            load_res: LoadResult = load_speakers(conn, valid_records)
            finish_etl_run(
                conn,
                run_id,
                status="succeeded",
                rows_extracted=rows_extracted,
                rows_inserted=load_res.inserted,
                rows_updated=load_res.updated,
                rows_discarded=rows_discarded,
            )

        summary = SyncSummary(
            run_id=run_id,
            status="succeeded",
            rows_extracted=rows_extracted,
            rows_inserted=load_res.inserted,
            rows_updated=load_res.updated,
            rows_unchanged=load_res.unchanged,
            rows_discarded=rows_discarded,
            bridge_conflicts_skipped=load_res.bridge_conflicts_skipped,
        )
        logger.info(
            "Wikidata sync run_id=%d succeeded: extracted=%d inserted=%d updated=%d unchanged=%d discarded=%d conflicts=%d",
            summary.run_id,
            summary.rows_extracted,
            summary.rows_inserted,
            summary.rows_updated,
            summary.rows_unchanged,
            summary.rows_discarded,
            summary.bridge_conflicts_skipped,
        )
        return summary

    except CircuitBreakerError:
        raise
    except Exception as exc:
        logger.exception("Wikidata sync run_id=%d failed: %s", run_id, exc)
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
        description="Run full Wikidata -> staging -> public.speakers sync."
    )
    parser.add_argument(
        "--query",
        type=Path,
        default=DEFAULT_QUERY_PATH,
        help="Path to the .sparql query file.",
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
        run_sync(
            query_path=args.query,
            min_volume_ratio=args.min_volume_ratio,
            force=args.force,
        )
    except CircuitBreakerError:
        sys.exit(2)


if __name__ == "__main__":
    main()
