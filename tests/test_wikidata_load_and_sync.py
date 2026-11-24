"""Integration and unit tests for Wikidata loader, idempotency, and circuit breaker."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from etl.db import transaction
from etl.run_wikidata_sync import (
    CircuitBreakerError,
    check_circuit_breaker,
    run_sync,
)
from etl.wikidata.load import load_speakers
from etl.wikidata.transform import SpeakerRecord

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
    """Spin up a clean ephemeral PostgreSQL instance with migrations 001..004."""
    pg_bin = _find_pg_bin()
    if pg_bin is None:
        pytest.skip("PostgreSQL server binaries (initdb/pg_ctl) not available")

    tmp_dir = Path(tempfile.mkdtemp(prefix="iddb_pg_test_"))
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


def _sample_bindings() -> list[dict[str, object]]:
    return [
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q3870135"},
            "displayName": {"type": "literal", "value": "Nanni Baldini"},
            "familyNames": {"type": "literal", "value": "Baldini"},
            "birthDates": {"type": "literal", "value": "1975-08-13T00:00:00Z@11"},
            "genderUris": {"type": "literal", "value": "http://www.wikidata.org/entity/Q6581097"},
            "countryCodes": {"type": "literal", "value": "IT"},
            "imdbIds": {"type": "literal", "value": "nm1129594"},
        },
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q3713381"},
            "displayName": {"type": "literal", "value": "Domitilla D'Amico"},
            "familyNames": {"type": "literal", "value": "D'Amico"},
            "birthDates": {"type": "literal", "value": "1982-09-05T00:00:00Z@11"},
            "genderUris": {"type": "literal", "value": "http://www.wikidata.org/entity/Q6581072"},
            "countryCodes": {"type": "literal", "value": "IT"},
            "imdbIds": {"type": "literal", "value": "nm0195120"},
            "tmdbIds": {"type": "literal", "value": "1137540"},
        },
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q3297658"},
            "displayName": {"type": "literal", "value": "Perla Liberatori"},
            "birthDates": {"type": "literal", "value": "1981-11-10T00:00:00Z@11"},
            "genderUris": {"type": "literal", "value": "http://www.wikidata.org/entity/Q6581072"},
            "countryCodes": {"type": "literal", "value": "IT"},
            "imdbIds": {"type": "literal", "value": "nm2959823"},
            "tmdbIds": {"type": "literal", "value": "1094814"},
        },
    ]


def test_check_circuit_breaker_thresholds() -> None:
    assert check_circuit_breaker(10, None) is None
    assert check_circuit_breaker(0, None) is not None
    assert check_circuit_breaker(70, 100, min_ratio=0.70) is None
    assert check_circuit_breaker(69, 100, min_ratio=0.70) is not None
    assert check_circuit_breaker(10, 100, min_ratio=0.70, force=True) is None


def test_two_consecutive_syncs_are_idempotent(pg_env: None) -> None:
    bindings = _sample_bindings()

    first = run_sync(bindings_override=bindings)
    assert first.status == "succeeded"
    assert first.rows_extracted == 3
    assert first.rows_inserted == 3
    assert first.rows_updated == 0
    assert first.rows_unchanged == 0

    with transaction() as conn:
        first_timestamps = {
            r["wikidata_qid"]: r["updated_at"]
            for r in conn.execute("SELECT wikidata_qid, updated_at FROM public.speakers").fetchall()
        }

    second = run_sync(bindings_override=bindings)
    assert second.status == "succeeded"
    assert second.rows_inserted == 0
    assert second.rows_updated == 0
    assert second.rows_unchanged == 3

    with transaction() as conn:
        second_timestamps = {
            r["wikidata_qid"]: r["updated_at"]
            for r in conn.execute("SELECT wikidata_qid, updated_at FROM public.speakers").fetchall()
        }
        runs = conn.execute("SELECT status, rows_inserted, rows_updated FROM public.etl_runs ORDER BY id").fetchall()

    assert second_timestamps == first_timestamps
    assert len(runs) == 2
    assert runs[1] == {"status": "succeeded", "rows_inserted": 0, "rows_updated": 0}


def test_verified_rows_are_protected_and_only_nulls_are_filled(pg_env: None) -> None:
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO public.speakers (
                display_name, sort_name, birth_year, wikidata_qid, is_verified
            )
            VALUES ('Curated Name', 'Name, Curated', 1975, 'Q3870135', TRUE)
            """
        )

    rec = SpeakerRecord(
        wikidata_qid="Q3870135",
        display_name="Wikidata Overwrite Attempt",
        sort_name="Attempt, Wikidata Overwrite",
        birth_year=1999,
        gender="male",
        imdb_nconst="nm1129594",
    )
    with transaction() as conn:
        res = load_speakers(conn, [rec])
        row = conn.execute(
            "SELECT display_name, sort_name, birth_year, gender, imdb_nconst FROM public.speakers WHERE wikidata_qid = 'Q3870135'"
        ).fetchone()

    assert res.inserted == 0
    assert res.updated == 1
    assert row == {
        "display_name": "Curated Name",
        "sort_name": "Name, Curated",
        "birth_year": 1975,
        "gender": "male",
        "imdb_nconst": "nm1129594",
    }


def test_cascading_bridge_match_and_cross_qid_conflict_safety(pg_env: None) -> None:
    # Pre-existing row from TMDB/manual with no wikidata_qid yet
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO public.speakers (
                display_name, sort_name, tmdb_person_id, primary_source
            )
            VALUES ('Domitilla D''Amico', 'D''Amico, Domitilla', 1137540, 'tmdb')
            """
        )

    # Two incoming Wikidata records: first matches existing TMDB row via cascade,
    # second erroneously claims the same IMDb ID as the first.
    rec1 = SpeakerRecord(
        wikidata_qid="Q3713381",
        display_name="Domitilla D'Amico",
        sort_name="D'Amico, Domitilla",
        imdb_nconst="nm0195120",
        tmdb_person_id=1137540,
    )
    rec2 = SpeakerRecord(
        wikidata_qid="Q999888",
        display_name="Duplicate IMDb Claimant",
        sort_name="Claimant, Duplicate IMDb",
        imdb_nconst="nm0195120",
    )

    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(seconds=10)
    with transaction() as conn:
        res = load_speakers(conn, [rec1, rec2], seen_at=t1)
        rows = conn.execute(
            "SELECT id, wikidata_qid, imdb_nconst, tmdb_person_id FROM public.speakers ORDER BY id"
        ).fetchall()

    assert res.inserted == 1
    assert res.updated == 1
    assert res.bridge_conflicts_skipped == 1
    assert len(rows) == 2
    assert rows[0]["wikidata_qid"] == "Q3713381"
    assert rows[0]["tmdb_person_id"] == 1137540
    assert rows[0]["imdb_nconst"] == "nm0195120"
    assert rows[1]["wikidata_qid"] == "Q999888"
    assert rows[1]["imdb_nconst"] is None

    # Confirm unchanged re-load updates last_seen_at without counting an update
    with transaction() as conn:
        res2 = load_speakers(conn, [rec1], seen_at=t2)
        seen = conn.execute(
            "SELECT last_seen_at FROM public.speakers WHERE wikidata_qid = 'Q3713381'"
        ).fetchone()
    assert res2.updated == 0
    assert res2.unchanged == 1
    assert seen is not None and seen["last_seen_at"] == t2


def test_circuit_breaker_aborts_write_and_audits_run(pg_env: None) -> None:
    full_batch = _sample_bindings()
    run_sync(bindings_override=full_batch)

    # Drop from 3 to 1 (33% < 70%) must abort before touching public.speakers
    partial_batch = [
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q9999999"},
            "displayName": {"type": "literal", "value": "Should Not Be Loaded"},
        }
    ]
    with pytest.raises(CircuitBreakerError):
        run_sync(bindings_override=partial_batch)

    with transaction() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM public.speakers").fetchone()["n"]
        last_run = conn.execute(
            "SELECT status, rows_extracted, rows_inserted, error_message FROM public.etl_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert count == 3
    assert last_run["status"] == "aborted_circuit_breaker"
    assert last_run["rows_extracted"] == 1
    assert last_run["rows_inserted"] == 0
    assert "Volume drop detected" in last_run["error_message"]


def test_partial_dates_constraints(pg_env: None) -> None:
    valid_cases = [
        ("Year Only", "Only, Year", 1975, None, None),
        ("Year Month", "Month, Year", 1975, 8, None),
        ("Month Day Only", "Only, Month Day", None, 5, 14),
        ("Leap Day No Year", "Year, Leap Day No", None, 2, 29),
        ("Leap Full 2000", "2000, Leap Full", 2000, 2, 29),
    ]
    with transaction() as conn:
        for display_name, sort_name, year, month, day in valid_cases:
            conn.execute(
                """
                INSERT INTO public.speakers (
                    display_name, sort_name, birth_year, birth_month, birth_day
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (display_name, sort_name, year, month, day),
            )
        count = conn.execute("SELECT COUNT(*) AS n FROM public.speakers").fetchone()["n"]
    assert count == len(valid_cases)

    invalid_cases = [
        (1975, None, 15, None, None, None),
        (1980, 4, 31, None, None, None),
        (1999, 2, 29, None, None, None),
        (1900, 2, 29, None, None, None),
        (1980, 5, 10, 1980, 3, 1),
    ]
    for b_y, b_m, b_d, d_y, d_m, d_d in invalid_cases:
        with pytest.raises(psycopg.errors.CheckViolation):
            with transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO public.speakers (
                        display_name, sort_name,
                        birth_year, birth_month, birth_day,
                        death_year, death_month, death_day
                    )
                    VALUES ('Invalid', 'Invalid', %s, %s, %s, %s, %s, %s)
                    """,
                    (b_y, b_m, b_d, d_y, d_m, d_d),
                )


def test_partial_dates_sync_backfill_and_leap_year_safety(pg_env: None) -> None:
    bindings = _sample_bindings()
    first = run_sync(bindings_override=bindings)
    assert first.rows_inserted == 3

    second = run_sync(bindings_override=bindings)
    assert second.rows_inserted == 0
    assert second.rows_updated == 0
    assert second.rows_unchanged == 3

    with transaction() as conn:
        nanni = conn.execute(
            "SELECT birth_year, birth_month, birth_day FROM public.speakers WHERE wikidata_qid = 'Q3870135'"
        ).fetchone()
        # Verified row with non-leap year 1999 must not graft Feb 29 from an incoming 2000-02-29
        conn.execute(
            """
            INSERT INTO public.speakers (
                display_name, sort_name, birth_year, wikidata_qid, is_verified
            )
            VALUES ('Verified 1999', '1999, Verified', 1999, 'Q9990010', TRUE)
            """
        )
        # Verified row with matching year 1980 backfills missing month and day cleanly
        conn.execute(
            """
            INSERT INTO public.speakers (
                display_name, sort_name, birth_year, wikidata_qid, is_verified
            )
            VALUES ('Verified 1980', '1980, Verified', 1980, 'Q9990011', TRUE)
            """
        )

    assert nanni == {"birth_year": 1975, "birth_month": 8, "birth_day": 13}

    incoming = [
        SpeakerRecord(
            wikidata_qid="Q9990010",
            display_name="Verified 1999",
            sort_name="1999, Verified",
            birth_year=2000,
            birth_month=2,
            birth_day=29,
        ),
        SpeakerRecord(
            wikidata_qid="Q9990011",
            display_name="Verified 1980",
            sort_name="1980, Verified",
            birth_year=1980,
            birth_month=6,
            birth_day=15,
        ),
    ]
    with transaction() as conn:
        load_speakers(conn, incoming)
        r1 = conn.execute(
            "SELECT birth_year, birth_month, birth_day FROM public.speakers WHERE wikidata_qid = 'Q9990010'"
        ).fetchone()
        r2 = conn.execute(
            "SELECT birth_year, birth_month, birth_day FROM public.speakers WHERE wikidata_qid = 'Q9990011'"
        ).fetchone()

    assert r1 == {"birth_year": 1999, "birth_month": None, "birth_day": None}
    assert r2 == {"birth_year": 1980, "birth_month": 6, "birth_day": 15}
