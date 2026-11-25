"""PostgreSQL integration tests for provenance, contributors, and append-only audit log."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from etl.db import transaction

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "sql" / "migrations"


def _find_pg_bin() -> Path | None:
    for candidate in (
        Path("/usr/lib/postgresql/17/bin"),
        Path("/usr/lib/postgresql/16/bin"),
        Path("/usr/lib/postgresql/15/bin"),
    ):
        if (candidate / "initdb").exists():
            return candidate
    initdb = shutil.which("initdb")
    return Path(initdb).parent if initdb else None


@pytest.fixture
def pg_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Spin up an isolated ephemeral PostgreSQL instance with all project migrations."""
    pg_bin = _find_pg_bin()
    if pg_bin is None:
        pytest.skip("PostgreSQL server binaries (initdb/pg_ctl) not available")

    tmp_dir = Path(tempfile.mkdtemp(prefix="iddb_pg_prov_test_"))
    pg_data = tmp_dir / "data"
    pg_sock = tmp_dir / "sock"
    pg_log = tmp_dir / "pg.log"
    pg_sock.mkdir()

    subprocess.run(
        [
            str(pg_bin / "initdb"),
            "-D",
            str(pg_data),
            "--auth=trust",
            "--encoding=UTF8",
            "--locale=C.UTF-8",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            str(pg_bin / "pg_ctl"),
            "-D",
            str(pg_data),
            "-l",
            str(pg_log),
            "-o",
            f"-k {pg_sock} -c listen_addresses=''",
            "start",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("PGHOST", str(pg_sock))
        monkeypatch.delenv("PGPORT", raising=False)
        monkeypatch.setenv("PGDATABASE", "iddb_test")
        monkeypatch.setenv("PGUSER", os.getenv("USER", "postgres"))
        monkeypatch.delenv("PGPASSWORD", raising=False)

        subprocess.run(
            [str(pg_bin / "createdb"), "-h", str(pg_sock), "iddb_test"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            subprocess.run(
                [
                    str(pg_bin / "psql"),
                    "-h",
                    str(pg_sock),
                    "-d",
                    "iddb_test",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-f",
                    str(migration),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        yield
    finally:
        subprocess.run(
            [str(pg_bin / "pg_ctl"), "-D", str(pg_data), "-m", "immediate", "stop"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_contributors_and_provenance_realistic_scenarios(pg_env: None) -> None:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.contributors (display_handle, accepted_cc_by_sa_4_0)
                VALUES ('dubbing_curator_1', TRUE)
                RETURNING id, public_id
                """
            )
            contrib = cur.fetchone()
            assert contrib["public_id"] == "ct0000001"

            cur.execute(
                """
                INSERT INTO public.speakers (display_name, sort_name, wikidata_qid)
                VALUES ('Speaker Provenance', 'Provenance, Speaker', 'Q990001')
                RETURNING id
                """
            )
            speaker_id = cur.fetchone()["id"]

            cur.execute(
                """
                INSERT INTO public.etl_runs (source, status)
                VALUES ('itwiki_speakers', 'succeeded')
                RETURNING id
                """
            )
            run_id = cur.fetchone()["id"]

            # 1. Wikidata CC0 provenance (no attribution_text required)
            cur.execute(
                """
                INSERT INTO public.provenance_records (
                    entity_table, entity_id, source_type, source_license,
                    source_url, source_revision_id, etl_run_id
                )
                VALUES (
                    'speakers', %s, 'wikidata', 'CC0-1.0',
                    'https://www.wikidata.org/wiki/Q990001', 'Q990001', %s
                )
                """,
                (speaker_id, run_id),
            )

            # 2. Language-agnostic Wikipedia CC BY-SA 4.0 provenance with revision ID
            cur.execute(
                """
                INSERT INTO public.provenance_records (
                    entity_table, entity_id, field_name, source_type, source_lang,
                    source_license, source_url, source_revision_id, attribution_text, etl_run_id
                )
                VALUES (
                    'speakers', %s, 'birth_year', 'wikipedia', 'it',
                    'CC-BY-SA-4.0', 'https://it.wikipedia.org/w/index.php?oldid=139000001',
                    '139000001', 'Contributors to Wikimedia projects (it.wikipedia.org)', %s
                )
                """,
                (speaker_id, run_id),
            )

            # 3. Primary end-credits verified provenance
            cur.execute(
                """
                INSERT INTO public.provenance_records (
                    entity_table, entity_id, source_type, source_license,
                    attribution_text, distribution_edition, credit_timecode,
                    episode_or_cut, contributor_id
                )
                VALUES (
                    'dubbing_credits', 1, 'end_credits', 'CC-BY-SA-4.0',
                    'Verified from domestic Blu-ray closing credits by ct0000001',
                    'Blu-ray IT (2024)', '01:54:12', 'Theatrical Cut', %s
                )
                """,
                (contrib["id"],),
            )

            # Aggregation query matching ATTRIBUTION.md export grouping
            cur.execute(
                """
                SELECT source_type, source_license, COUNT(*) AS rows
                FROM public.provenance_records
                GROUP BY source_type, source_license
                ORDER BY source_type
                """
            )
            grouped = cur.fetchall()
            assert len(grouped) == 3

            # Case-insensitive handle uniqueness check
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    """
                    INSERT INTO public.contributors (display_handle, accepted_cc_by_sa_4_0)
                    VALUES ('DUBBING_CURATOR_1', TRUE)
                    """
                )


def test_provenance_compliance_constraints(pg_env: None) -> None:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.contributors (display_handle, accepted_cc_by_sa_4_0)
                VALUES ('reviewer_a', TRUE)
                RETURNING id
                """
            )
            contrib_id = cur.fetchone()["id"]

            invalid_statements = [
                # Reject NC license
                (
                    """
                    INSERT INTO public.provenance_records (
                        entity_table, entity_id, source_type, source_license,
                        source_url, attribution_text
                    )
                    VALUES ('speakers', 1, 'tvmaze', 'CC-BY-NC-SA-4.0', 'https://api.tvmaze.com/shows/1', 'TVmaze')
                    """,
                    (),
                ),
                # Reject web source missing source_url
                (
                    """
                    INSERT INTO public.provenance_records (
                        entity_table, entity_id, source_type, source_license
                    )
                    VALUES ('speakers', 1, 'wikidata', 'CC0-1.0')
                    """,
                    (),
                ),
                # Reject wikipedia entry missing source_revision_id
                (
                    """
                    INSERT INTO public.provenance_records (
                        entity_table, entity_id, source_type, source_license,
                        source_url, attribution_text
                    )
                    VALUES (
                        'speakers', 1, 'wikipedia', 'CC-BY-SA-4.0',
                        'https://it.wikipedia.org/wiki/Doppiaggio', 'Wikimedia contributors'
                    )
                    """,
                    (),
                ),
                # Reject CC-BY-SA-4.0 missing attribution_text
                (
                    """
                    INSERT INTO public.provenance_records (
                        entity_table, entity_id, source_type, source_license,
                        source_url, source_revision_id
                    )
                    VALUES (
                        'speakers', 1, 'itwiki', 'CC-BY-SA-4.0',
                        'https://it.wikipedia.org/w/index.php?oldid=123', '123'
                    )
                    """,
                    (),
                ),
                # Reject end_credits missing distribution_edition
                (
                    """
                    INSERT INTO public.provenance_records (
                        entity_table, entity_id, source_type, source_license,
                        attribution_text, contributor_id
                    )
                    VALUES ('dubbing_credits', 1, 'end_credits', 'CC-BY-SA-4.0', 'Verified end credits', %s)
                    """,
                    (contrib_id,),
                ),
                # Reject camcording edition labels
                (
                    """
                    INSERT INTO public.provenance_records (
                        entity_table, entity_id, source_type, source_license,
                        attribution_text, distribution_edition, contributor_id
                    )
                    VALUES (
                        'dubbing_credits', 1, 'end_credits', 'CC-BY-SA-4.0',
                        'Invalid cinema capture', 'theatrical_cam IT', %s
                    )
                    """,
                    (contrib_id,),
                ),
                # Reject malformed timecode
                (
                    """
                    INSERT INTO public.provenance_records (
                        entity_table, entity_id, source_type, source_license,
                        attribution_text, distribution_edition, credit_timecode, contributor_id
                    )
                    VALUES (
                        'dubbing_credits', 1, 'end_credits', 'CC-BY-SA-4.0',
                        'Bad timecode', 'Blu-ray IT', '99:99:99', %s
                    )
                    """,
                    (contrib_id,),
                ),
                # Reject invalid entity_table
                (
                    """
                    INSERT INTO public.provenance_records (
                        entity_table, entity_id, source_type, source_license, source_url
                    )
                    VALUES ('etl_runs', 1, 'wikidata', 'CC0-1.0', 'https://www.wikidata.org/wiki/Q1')
                    """,
                    (),
                ),
            ]

            for sql_stmt, params in invalid_statements:
                cur.execute("SAVEPOINT chk_sp")
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(sql_stmt, params)
                cur.execute("ROLLBACK TO SAVEPOINT chk_sp")


def test_audit_log_append_only_enforcement(pg_env: None) -> None:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.contributors (display_handle, accepted_cc_by_sa_4_0)
                VALUES ('auditor_1', TRUE)
                RETURNING id
                """
            )
            contrib_id = cur.fetchone()["id"]

            cur.execute(
                """
                INSERT INTO public.provenance_records (
                    entity_table, entity_id, source_type, source_license,
                    attribution_text, distribution_edition, contributor_id
                )
                VALUES (
                    'dubbing_credits', 42, 'community_primary_verified', 'CC-BY-SA-4.0',
                    'Verified by auditor_1', 'Rai 2 TV HD', %s
                )
                RETURNING id
                """,
                (contrib_id,),
            )
            prov_id = cur.fetchone()["id"]

            # Non-object JSONB must fail
            cur.execute("SAVEPOINT bad_json_sp")
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    """
                    INSERT INTO public.audit_log (entity_table, entity_id, action, changed_data)
                    VALUES ('dubbing_credits', 42, 'INSERT', '["not-an-object"]'::jsonb)
                    """
                )
            cur.execute("ROLLBACK TO SAVEPOINT bad_json_sp")

            cur.execute(
                """
                INSERT INTO public.audit_log (
                    entity_table, entity_id, action, changed_data, provenance_id, contributor_id
                )
                VALUES (
                    'dubbing_credits', 42, 'VERIFY',
                    '{"is_verified": true, "role_type": "voice"}'::jsonb,
                    %s, %s
                )
                RETURNING id
                """,
                (prov_id, contrib_id),
            )
            log_id = cur.fetchone()["id"]

            # Append-only trigger must reject both UPDATE and DELETE
            cur.execute("SAVEPOINT update_sp")
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                cur.execute(
                    "UPDATE public.audit_log SET action = 'DELETE' WHERE id = %s",
                    (log_id,),
                )
            cur.execute("ROLLBACK TO SAVEPOINT update_sp")

            cur.execute("SAVEPOINT delete_sp")
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                cur.execute("DELETE FROM public.audit_log WHERE id = %s", (log_id,))
            cur.execute("ROLLBACK TO SAVEPOINT delete_sp")
