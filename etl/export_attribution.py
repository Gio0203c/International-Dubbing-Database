"""Compliance-gated ATTRIBUTION.md exporter backed by public.provenance_records."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv

from etl.db import connect

ALLOWED_OPEN_SPDX = (
    "CC0-1.0",
    "PDM-1.0",
    "CC-BY-1.0",
    "CC-BY-2.0",
    "CC-BY-3.0",
    "CC-BY-4.0",
    "CC-BY-SA-1.0",
    "CC-BY-SA-2.0",
    "CC-BY-SA-3.0",
    "CC-BY-SA-4.0",
)


class ComplianceExportError(RuntimeError):
    """Raised when provenance rows violate open-license or attribution requirements."""


def build_attribution_markdown(conn: psycopg.Connection[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Validate provenance compliance and render ATTRIBUTION.md content from IDDb."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM public.provenance_records
            WHERE source_license ~* '(^|-)(NC|ND)(-|$)'
               OR NOT (source_license = ANY(%s))
               OR (
                   source_license NOT IN ('CC0-1.0', 'PDM-1.0')
                   AND (attribution_text IS NULL OR btrim(attribution_text) = '')
               )
               OR (
                   source_type IN ('itwiki', 'wikipedia')
                   AND (
                       source_url IS NULL
                       OR btrim(source_url) = ''
                       OR source_revision_id IS NULL
                       OR btrim(source_revision_id) = ''
                   )
               )
            """,
            (list(ALLOWED_OPEN_SPDX),),
        )
        bad_rows = int(cur.fetchone()["cnt"])
        if bad_rows > 0:
            raise ComplianceExportError(
                f"Found {bad_rows} provenance rows failing license, revision, or attribution compliance."
            )

        cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM public.provenance_records p
            JOIN public.contributors c ON c.id = p.contributor_id
            WHERE c.accepted_cc_by_sa_4_0 IS NOT TRUE
            """
        )
        unaccepted_contributors = int(cur.fetchone()["cnt"])
        if unaccepted_contributors > 0:
            raise ComplianceExportError(
                f"Found {unaccepted_contributors} provenance rows linked to contributors without accepted_cc_by_sa_4_0 = TRUE."
            )

        cur.execute(
            """
            SELECT
                source_type,
                COALESCE(source_lang, '-') AS source_lang,
                source_license,
                COUNT(*) AS record_count,
                MIN(source_url) AS sample_url
            FROM public.provenance_records
            GROUP BY source_type, COALESCE(source_lang, '-'), source_license
            ORDER BY record_count DESC, source_type, source_lang
            """
        )
        source_groups = cur.fetchall()

        cur.execute(
            """
            SELECT DISTINCT ON (source_type, COALESCE(source_url, attribution_text))
                source_type,
                source_lang,
                source_license,
                source_url,
                source_revision_id,
                attribution_text
            FROM public.provenance_records
            WHERE source_license NOT IN ('CC0-1.0', 'PDM-1.0')
            ORDER BY source_type, COALESCE(source_url, attribution_text), id
            """
        )
        full_attributions = cur.fetchall()

        cur.execute(
            """
            SELECT DISTINCT c.public_id, c.display_handle
            FROM public.provenance_records p
            JOIN public.contributors c ON c.id = p.contributor_id
            ORDER BY c.public_id
            """
        )
        contributors = cur.fetchall()

    lines = [
        "# IDDb Attribution & Licensing Notice",
        "",
        "- **Database & Dataset License:** [CC BY-SA 4.0 (Attribution-ShareAlike 4.0 International)](https://creativecommons.org/licenses/by-sa/4.0/)",
        "- **Repository Engine & Code License:** Apache License, Version 2.0",
        "- **Modifications Notice:** Source facts and metadata have been normalized, deduplicated, and combined into the IDDb relational schema.",
        "",
        "## Provenance Summary",
        "",
        "| Source Type | Language | SPDX License | Records Tracked | Sample Reference |",
        "| :--- | :--- | :--- | ---: | :--- |",
    ]
    total_records = 0
    for grp in source_groups:
        total_records += int(grp["record_count"])
        sample = grp["sample_url"] or "-"
        lines.append(
            f"| `{grp['source_type']}` | `{grp['source_lang']}` | `{grp['source_license']}` | {grp['record_count']} | {sample} |"
        )
    if not source_groups:
        lines.append("| *(none recorded yet)* | - | - | 0 | - |")

    lines.extend(["", "## Full Attribution Citations (Non-CC0 Sources)", ""])
    if full_attributions:
        for item in full_attributions:
            url_part = f" — {item['source_url']}" if item["source_url"] else ""
            lines.append(f"- `{item['source_type']}` (`{item['source_license']}`): {item['attribution_text']}{url_part}")
    else:
        lines.append("- No non-CC0 provenance entries recorded yet.")

    lines.extend(["", "## Pseudonymous Primary Contributors", ""])
    if contributors:
        for c in contributors:
            lines.append(f"- `{c['public_id']}` (`{c['display_handle']}`)")
    else:
        lines.append("- No primary contributor entries recorded yet.")

    summary = {
        "source_group_count": len(source_groups),
        "total_provenance_records": total_records,
        "citation_count": len(full_attributions),
        "contributor_count": len(contributors),
    }
    return "\n".join(lines) + "\n", summary


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Export compliance-checked ATTRIBUTION.md from IDDb provenance.")
    parser.add_argument("--output", type=Path, default=None, help="Destination path for ATTRIBUTION.md.")
    parser.add_argument("--check-only", action="store_true", help="Validate provenance compliance without writing output.")
    args = parser.parse_args()

    with connect() as conn:
        markdown, summary = build_attribution_markdown(conn)

    if args.check_only:
        print(f"ATTRIBUTION CHECK PASSED: {summary}")
        return

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(markdown, encoding="utf-8")
        print(f"Wrote {args.output} ({summary})")
    else:
        print(markdown)


if __name__ == "__main__":
    main()
