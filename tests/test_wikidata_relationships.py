"""Unit and PostgreSQL integration tests for speaker family relationships ETL."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from etl.db import transaction
from etl.run_wikidata_relationships_sync import run_relationships_sync
from etl.run_wikidata_sync import CircuitBreakerError
from etl.wikidata.relationships import (
    RelationshipRecord,
    load_relationships,
    transform_relationship_bindings,
)

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
    """Spin up an isolated ephemeral PostgreSQL 17 instance with all migrations."""
    pg_bin = _find_pg_bin()
    if pg_bin is None:
        pytest.skip("PostgreSQL server binaries (initdb/pg_ctl) not available")

    tmp_dir = Path(tempfile.mkdtemp(prefix="iddb_pg_rel_test_"))
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
        )
        for sql_file in sorted(MIGRATIONS_DIR.glob("00*.sql")):
            subprocess.run(
                [
                    "psql",
                    "-h",
                    str(pg_sock),
                    "-d",
                    "iddb_test",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-q",
                    "-f",
                    str(sql_file),
                ],
                check=True,
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


def _sample_relationship_bindings() -> list[dict[str, object]]:
    return [
        # Sibling declared in both directions merges into one canonical edge
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q920348"},
            "prop": {"type": "literal", "value": "P3373"},
            "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q3870135"},
        },
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q3870135"},
            "prop": {"type": "literal", "value": "P3373"},
            "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q920348"},
        },
        # Father (P22) and child (P40) flip/align into one directed parent_of edge (Q10 -> Q20)
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q20"},
            "prop": {"type": "literal", "value": "P22"},
            "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
        },
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
            "prop": {"type": "literal", "value": "P40"},
            "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q20"},
        },
        # Spouse with start_year on one side and unknown endDate (bnode) + divorce cause on the other
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q30"},
            "prop": {"type": "literal", "value": "P26"},
            "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q40"},
            "startDate": {"type": "literal", "value": "1990-06-15T00:00:00Z"},
        },
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q40"},
            "prop": {"type": "literal", "value": "P26"},
            "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q30"},
            "endDate": {"type": "bnode", "value": "t999"},
            "endCause": {"type": "uri", "value": "http://www.wikidata.org/entity/Q93190"},
        },
        # Unmarried partner with explicit start and end years
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q50"},
            "prop": {"type": "literal", "value": "P451"},
            "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q60"},
            "startDate": {"type": "literal", "value": "2001-01-01T00:00:00Z"},
            "endDate": {"type": "literal", "value": "2005-12-31T00:00:00Z"},
        },
        # Invalid/self/unknown-prop rows must be discarded
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
            "prop": {"type": "literal", "value": "P3373"},
            "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
        },
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q0"},
            "prop": {"type": "literal", "value": "P26"},
            "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
        },
    ]


def test_transform_relationship_bindings_merges_directions_and_qualifiers() -> None:
    res = transform_relationship_bindings(_sample_relationship_bindings())
    assert res.discarded_count == 2
    assert res.duplicate_count == 3
    assert len(res.records) == 4

    by_key = {
        (r.subject_qid, r.related_qid, r.relationship_type): r
        for r in res.records
    }
    assert by_key[("Q10", "Q20", "parent_of")] == RelationshipRecord(
        subject_qid="Q10",
        related_qid="Q20",
        relationship_type="parent_of",
    )
    assert by_key[("Q3870135", "Q920348", "sibling")] == RelationshipRecord(
        subject_qid="Q3870135",
        related_qid="Q920348",
        relationship_type="sibling",
    )
    # Former spouse with unknown endDate (bnode) sets is_ended=True and keeps start_year
    assert by_key[("Q30", "Q40", "spouse")] == RelationshipRecord(
        subject_qid="Q30",
        related_qid="Q40",
        relationship_type="spouse",
        is_ended=True,
        start_year=1990,
        end_year=None,
    )
    assert by_key[("Q50", "Q60", "partner")] == RelationshipRecord(
        subject_qid="Q50",
        related_qid="Q60",
        relationship_type="partner",
        is_ended=True,
        start_year=2001,
        end_year=2005,
    )


def test_transform_clears_inverted_relationship_years_preserving_ended_flag() -> None:
    res = transform_relationship_bindings(
        [
            {
                "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
                "prop": {"type": "literal", "value": "P26"},
                "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q20"},
                "startDate": {"type": "literal", "value": "2005-01-01T00:00:00Z"},
                "endDate": {"type": "literal", "value": "1999-01-01T00:00:00Z"},
            }
        ]
    )
    assert len(res.records) == 1
    assert res.records[0].is_ended is True
    assert res.records[0].start_year is None
    assert res.records[0].end_year is None


def test_relationships_sync_idempotency_canonical_order_and_unmatched(pg_env: None) -> None:
    # Insert speakers where Q920348 gets smaller id than Q3870135's order to test DB-ID canonicalization
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO public.speakers (display_name, sort_name, wikidata_qid)
            VALUES
                ('Antonella Baldini', 'Baldini, Antonella', 'Q920348'),
                ('Nanni Baldini', 'Baldini, Nanni', 'Q3870135'),
                ('Parent Speaker', 'Speaker, Parent', 'Q10'),
                ('Child Speaker', 'Speaker, Child', 'Q20'),
                ('Spouse A', 'A, Spouse', 'Q30'),
                ('Spouse B', 'B, Spouse', 'Q40')
            """
        )

    # Note: Q50/Q60 are NOT in public.speakers and must be skipped cleanly via unmatched_speakers
    bindings = _sample_relationship_bindings()

    first = run_relationships_sync(bindings_override=bindings)
    assert first.status == "succeeded"
    assert first.rows_inserted == 3
    assert first.rows_updated == 0
    assert first.unmatched_speakers == 1

    with transaction() as conn:
        rows_after_1 = conn.execute(
            """
            SELECT speaker_id, related_speaker_id, relationship_type, is_ended, start_year, end_year, updated_at
            FROM public.speaker_relationships
            ORDER BY id
            """
        ).fetchall()

    assert len(rows_after_1) == 3
    # Symmetric pairs obey speaker_id < related_speaker_id
    for r in rows_after_1:
        if r["relationship_type"] != "parent_of":
            assert r["speaker_id"] < r["related_speaker_id"]

    second = run_relationships_sync(bindings_override=bindings)
    assert second.status == "succeeded"
    assert second.rows_inserted == 0
    assert second.rows_updated == 0
    assert second.rows_unchanged == 3

    with transaction() as conn:
        rows_after_2 = conn.execute(
            "SELECT id, updated_at FROM public.speaker_relationships ORDER BY id"
        ).fetchall()
    assert [r["updated_at"] for r in rows_after_2] == [
        r["updated_at"] for r in rows_after_1
    ]


def test_relationships_verified_protection_and_circuit_breaker(pg_env: None) -> None:
    with transaction() as conn:
        s1 = int(
            conn.execute(
                "INSERT INTO public.speakers (display_name, sort_name, wikidata_qid) VALUES ('A', 'A', 'Q100') RETURNING id"
            ).fetchone()["id"]
        )
        s2 = int(
            conn.execute(
                "INSERT INTO public.speakers (display_name, sort_name, wikidata_qid) VALUES ('B', 'B', 'Q200') RETURNING id"
            ).fetchone()["id"]
        )
        s3 = int(
            conn.execute(
                "INSERT INTO public.speakers (display_name, sort_name, wikidata_qid) VALUES ('C', 'C', 'Q300') RETURNING id"
            ).fetchone()["id"]
        )
        # Verified still-married row (is_ended=False) must NOT be overwritten by incoming ended info
        conn.execute(
            """
            INSERT INTO public.speaker_relationships (
                speaker_id, related_speaker_id, relationship_type, is_ended, is_verified
            )
            VALUES (%s, %s, 'spouse', FALSE, TRUE)
            """,
            (s1, s2),
        )
        # Verified row with NULL lifecycle fills missing start_year safely
        conn.execute(
            """
            INSERT INTO public.speaker_relationships (
                speaker_id, related_speaker_id, relationship_type, is_ended, is_verified
            )
            VALUES (%s, %s, 'partner', NULL, TRUE)
            """,
            (s2, s3),
        )

    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(seconds=10)
    incoming = [
        RelationshipRecord("Q100", "Q200", "spouse", is_ended=True, start_year=1990, end_year=2001),
        RelationshipRecord("Q200", "Q300", "partner", is_ended=True, start_year=2010, end_year=None),
    ]
    with transaction() as conn:
        res = load_relationships(conn, incoming, seen_at=t1)
        rows = conn.execute(
            "SELECT speaker_id, related_speaker_id, is_ended, start_year, end_year FROM public.speaker_relationships ORDER BY id"
        ).fetchall()

    assert res.updated == 2
    # Verified is_ended=False keeps False and drops conflicting end_year, while backfilling start_year
    assert rows[0]["is_ended"] is False
    assert rows[0]["start_year"] == 1990
    assert rows[0]["end_year"] is None
    # Verified NULL fills is_ended=True and start_year=2010
    assert rows[1]["is_ended"] is True
    assert rows[1]["start_year"] == 2010

    # Establish successful baseline of 4 bindings, then verify drop to 1 aborts via circuit breaker
    baseline = [
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q100"},
            "prop": {"type": "literal", "value": "P3373"},
            "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q200"},
        },
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q200"},
            "prop": {"type": "literal", "value": "P3373"},
            "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q300"},
        },
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q100"},
            "prop": {"type": "literal", "value": "P1038"},
            "related": {"type": "uri", "value": "http://www.wikidata.org/entity/Q300"},
        },
    ]
    run_relationships_sync(bindings_override=baseline)

    with pytest.raises(CircuitBreakerError):
        run_relationships_sync(bindings_override=baseline[:1])

    with transaction() as conn:
        last_run = conn.execute(
            "SELECT source, status, error_message FROM public.etl_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        # Verify unchanged reload updates last_seen_at cleanly
        r_unchanged = load_relationships(conn, incoming, seen_at=t2)
    assert last_run["source"] == "wikidata_relationships"
    assert last_run["status"] == "aborted_circuit_breaker"
    assert r_unchanged.unchanged == 2
