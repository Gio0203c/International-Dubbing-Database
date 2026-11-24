"""End-to-end Wikidata sync runner for speaker satellite tables."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Iterable, Sequence
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
from etl.wikidata.satellites import (
    DEFAULT_ALIASES_QUERY_PATH,
    DEFAULT_EXTERNAL_IDS_QUERY_PATH,
    DEFAULT_LANGUAGES_QUERY_PATH,
    SatelliteLoadResult,
    load_speaker_aliases,
    load_speaker_external_ids,
    load_speaker_languages,
    transform_alias_bindings,
    transform_external_id_bindings,
    transform_language_bindings,
)

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SatelliteTargetSummary:
    """Outcome summary for one satellite table sync execution."""

    target: str
    run_id: int
    source: str
    status: str
    rows_extracted: int
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    rows_discarded: int
    unmatched_speakers: int
    conflicts_skipped: int


@dataclass(frozen=True)
class SatellitesSyncSummary:
    """Combined outcome summary across requested speaker satellite targets."""

    results: dict[str, SatelliteTargetSummary]


def _run_single_target(
    *,
    target: str,
    source_name: str,
    query_path: Path,
    transform_fn: Callable[[Iterable[dict[str, Any]]], Any],
    load_fn: Callable[..., SatelliteLoadResult],
    min_volume_ratio: float,
    force: bool,
    bindings_override: list[dict[str, Any]] | None,
) -> SatelliteTargetSummary:
    with transaction() as conn:
        run_id = create_etl_run(conn, source_name)

    rows_extracted = 0
    rows_discarded = 0

    try:
        if bindings_override is None:
            query = load_query(query_path)
            logger.info(
                "Starting Wikidata %s sync run_id=%d using %s",
                target,
                run_id,
                query_path,
            )
            bindings = fetch_sparql_bindings(query)
        else:
            bindings = list(bindings_override)

        rows_extracted = len(bindings)
        transform_res = transform_fn(bindings)
        rows_discarded = int(transform_res.discarded_count)
        valid_records = transform_res.records
        logger.info(
            "Transformed %d unique %s records from %d raw bindings (%d discarded, %d duplicates).",
            len(valid_records),
            target,
            rows_extracted,
            rows_discarded,
            transform_res.duplicate_count,
        )

        with transaction() as conn:
            previous_count = get_previous_successful_count(conn, source_name)

        abort_reason = (
            f"Extracted zero valid {target} records from Wikidata."
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
                "Circuit breaker aborted %s run_id=%d: %s",
                target,
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
            load_res = load_fn(conn, valid_records)
            finish_etl_run(
                conn,
                run_id,
                status="succeeded",
                rows_extracted=rows_extracted,
                rows_inserted=load_res.inserted,
                rows_updated=load_res.updated,
                rows_discarded=rows_discarded,
            )

        summary = SatelliteTargetSummary(
            target=target,
            run_id=run_id,
            source=source_name,
            status="succeeded",
            rows_extracted=rows_extracted,
            rows_inserted=load_res.inserted,
            rows_updated=load_res.updated,
            rows_unchanged=load_res.unchanged,
            rows_discarded=rows_discarded,
            unmatched_speakers=load_res.unmatched_speakers,
            conflicts_skipped=load_res.conflicts_skipped,
        )
        logger.info(
            "Satellite sync %s run_id=%d succeeded: extracted=%d inserted=%d updated=%d unchanged=%d discarded=%d unmatched=%d conflicts=%d",
            summary.target,
            summary.run_id,
            summary.rows_extracted,
            summary.rows_inserted,
            summary.rows_updated,
            summary.rows_unchanged,
            summary.rows_discarded,
            summary.unmatched_speakers,
            summary.conflicts_skipped,
        )
        return summary

    except CircuitBreakerError:
        raise
    except Exception as exc:
        logger.exception("Satellite sync %s run_id=%d failed: %s", target, run_id, exc)
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


def run_satellites_sync(
    *,
    targets: Sequence[str] = ("aliases", "languages", "external_ids"),
    min_volume_ratio: float = DEFAULT_MIN_VOLUME_RATIO,
    force: bool = False,
    aliases_bindings_override: list[dict[str, Any]] | None = None,
    languages_bindings_override: list[dict[str, Any]] | None = None,
    external_ids_bindings_override: list[dict[str, Any]] | None = None,
) -> SatellitesSyncSummary:
    """Run Extract -> Transform -> Circuit Breaker -> Load for requested satellite tables."""
    target_specs: dict[str, tuple[str, Path, Callable[..., Any], Callable[..., SatelliteLoadResult], list[dict[str, Any]] | None]] = {
        "aliases": (
            "wikidata_aliases",
            DEFAULT_ALIASES_QUERY_PATH,
            transform_alias_bindings,
            load_speaker_aliases,
            aliases_bindings_override,
        ),
        "languages": (
            "wikidata_languages",
            DEFAULT_LANGUAGES_QUERY_PATH,
            transform_language_bindings,
            load_speaker_languages,
            languages_bindings_override,
        ),
        "external_ids": (
            "wikidata_external_ids",
            DEFAULT_EXTERNAL_IDS_QUERY_PATH,
            transform_external_id_bindings,
            load_speaker_external_ids,
            external_ids_bindings_override,
        ),
    }

    results: dict[str, SatelliteTargetSummary] = {}
    for target in targets:
        if target not in target_specs:
            raise ValueError(f"Unsupported satellite sync target: {target!r}")
        source_name, query_path, transform_fn, load_fn, override = target_specs[target]
        results[target] = _run_single_target(
            target=target,
            source_name=source_name,
            query_path=query_path,
            transform_fn=transform_fn,
            load_fn=load_fn,
            min_volume_ratio=min_volume_ratio,
            force=force,
            bindings_override=override,
        )

    return SatellitesSyncSummary(results=results)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync speaker aliases, languages, and external IDs from Wikidata."
    )
    parser.add_argument(
        "--only",
        choices=("all", "aliases", "languages", "external_ids"),
        default="all",
        help="Run all satellite targets or a single table sync.",
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
    targets = (
        ("aliases", "languages", "external_ids")
        if args.only == "all"
        else (args.only,)
    )
    try:
        run_satellites_sync(
            targets=targets,
            min_volume_ratio=args.min_volume_ratio,
            force=args.force,
        )
    except CircuitBreakerError:
        sys.exit(2)


if __name__ == "__main__":
    main()
