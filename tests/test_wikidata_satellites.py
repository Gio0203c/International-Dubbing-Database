"""Unit and PostgreSQL integration tests for speaker satellite tables ETL."""

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
from etl.run_wikidata_satellites_sync import run_satellites_sync
from etl.run_wikidata_sync import CircuitBreakerError
from etl.wikidata.satellites import (
    AliasRecord,
    ExternalIdRecord,
    LanguageRecord,
    load_speaker_aliases,
    transform_alias_bindings,
    transform_external_id_bindings,
    transform_language_bindings,
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
    """Spin up an isolated ephemeral PostgreSQL instance with all migrations."""
    pg_bin = _find_pg_bin()
    if pg_bin is None:
        pytest.skip("PostgreSQL server binaries (initdb/pg_ctl) not available")

    tmp_dir = Path(tempfile.mkdtemp(prefix="iddb_pg_sat_test_"))
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


def test_transform_satellite_bindings_normalization_and_precedence() -> None:
    alias_res = transform_alias_bindings(
        [
            {
                "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
                "aliasType": {"type": "literal", "value": "other"},
                "aliasName": {"type": "literal", "value": "Alvin   Soprano", "xml:lang": "it"},
            },
            {
                "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
                "aliasType": {"type": "literal", "value": "stage_name"},
                "aliasName": {"type": "literal", "value": "Alvin Soprano", "xml:lang": "it"},
            },
            {
                "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
                "aliasType": {"type": "literal", "value": "other"},
                "aliasName": {"type": "literal", "value": "Multilingual Alias", "xml:lang": "mul"},
            },
            {
                "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
                "aliasType": {"type": "literal", "value": "other"},
                "aliasName": {"type": "bnode", "value": "t1"},
            },
        ]
    )
    assert alias_res.discarded_count == 1
    assert alias_res.duplicate_count == 1
    assert alias_res.records == [
        AliasRecord("Q10", "Alvin Soprano", alias_type="stage_name", locale="it"),
        AliasRecord("Q10", "Multilingual Alias", alias_type="other", locale=None),
    ]

    lang_res = transform_language_bindings(
        [
            {
                "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
                "role": {"type": "literal", "value": "native"},
                "langCode": {"type": "literal", "value": "IT"},
            },
            {
                "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
                "role": {"type": "literal", "value": "native"},
                "langCode": {"type": "literal", "value": "it"},
            },
            {
                "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
                "role": {"type": "literal", "value": "spoken"},
                "langCode": {"type": "literal", "value": "ita"},
            },
        ]
    )
    assert lang_res.discarded_count == 1
    assert lang_res.duplicate_count == 1
    assert lang_res.records == [LanguageRecord("Q10", "it", "native")]

    ext_res = transform_external_id_bindings(
        [
            {
                "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
                "provider": {"type": "literal", "value": "itwiki"},
                "externalId": {
                    "type": "literal",
                    "value": "https://it.wikipedia.org/wiki/Domitilla_D%27Amico",
                },
            },
            {
                "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
                "provider": {"type": "literal", "value": "anilist"},
                "externalId": {"type": "literal", "value": "10542"},
            },
            # Core bridge providers are rejected for speaker_external_ids
            {
                "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
                "provider": {"type": "literal", "value": "imdb"},
                "externalId": {"type": "literal", "value": "nm0195120"},
            },
            {
                "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
                "provider": {"type": "literal", "value": "anilist"},
                "externalId": {"type": "literal", "value": "10542"},
            },
        ]
    )
    assert ext_res.discarded_count == 1
    assert ext_res.duplicate_count == 1
    assert ext_res.records == [
        ExternalIdRecord("Q10", "anilist", "10542"),
        ExternalIdRecord("Q10", "itwiki", "Domitilla_D'Amico"),
    ]


def test_satellites_sync_idempotency_conflicts_and_display_name_skip(
    pg_env: None,
) -> None:
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO public.speakers (display_name, sort_name, wikidata_qid)
            VALUES
                ('Tonino Accolla', 'Accolla, Tonino', 'Q10'),
                ('Chiara Fabiano', 'Fabiano, Chiara', 'Q20')
            """
        )

    aliases_bindings = [
        # Duplicate of speaker's own display_name must be skipped on load
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
            "aliasType": {"type": "literal", "value": "other"},
            "aliasName": {"type": "literal", "value": "tonino accolla", "xml:lang": "it"},
        },
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
            "aliasType": {"type": "literal", "value": "stage_name"},
            "aliasName": {"type": "literal", "value": "Antonio Accolla", "xml:lang": "it"},
        },
        # Unmatched QID skipped cleanly
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q999999"},
            "aliasType": {"type": "literal", "value": "other"},
            "aliasName": {"type": "literal", "value": "Unknown Person", "xml:lang": "it"},
        },
    ]
    languages_bindings = [
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
            "role": {"type": "literal", "value": "spoken"},
            "langCode": {"type": "literal", "value": "it"},
        },
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q20"},
            "role": {"type": "literal", "value": "native"},
            "langCode": {"type": "literal", "value": "it"},
        },
    ]
    ext_bindings = [
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q10"},
            "provider": {"type": "literal", "value": "anilist"},
            "externalId": {"type": "literal", "value": "12345"},
        },
        # Same (provider, external_id) claimed by second speaker skips cleanly
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q20"},
            "provider": {"type": "literal", "value": "anilist"},
            "externalId": {"type": "literal", "value": "12345"},
        },
    ]

    first = run_satellites_sync(
        aliases_bindings_override=aliases_bindings,
        languages_bindings_override=languages_bindings,
        external_ids_bindings_override=ext_bindings,
    )
    assert first.results["aliases"].rows_inserted == 1
    assert first.results["aliases"].unmatched_speakers == 1
    assert first.results["languages"].rows_inserted == 2
    assert first.results["external_ids"].rows_inserted == 1
    assert first.results["external_ids"].conflicts_skipped == 1

    with transaction() as conn:
        alias_ts1 = conn.execute(
            "SELECT updated_at FROM public.speaker_aliases ORDER BY id"
        ).fetchall()
        lang_ts1 = conn.execute(
            "SELECT updated_at FROM public.speaker_languages ORDER BY id"
        ).fetchall()
        ext_ts1 = conn.execute(
            "SELECT updated_at FROM public.speaker_external_ids ORDER BY id"
        ).fetchall()

    second = run_satellites_sync(
        aliases_bindings_override=aliases_bindings,
        languages_bindings_override=languages_bindings,
        external_ids_bindings_override=ext_bindings,
    )
    for key, expected_unchanged in (("aliases", 1), ("languages", 2), ("external_ids", 1)):
        assert second.results[key].rows_inserted == 0
        assert second.results[key].rows_updated == 0
        assert second.results[key].rows_unchanged == expected_unchanged

    with transaction() as conn:
        assert (
            conn.execute(
                "SELECT updated_at FROM public.speaker_aliases ORDER BY id"
            ).fetchall()
            == alias_ts1
        )
        assert (
            conn.execute(
                "SELECT updated_at FROM public.speaker_languages ORDER BY id"
            ).fetchall()
            == lang_ts1
        )
        assert (
            conn.execute(
                "SELECT updated_at FROM public.speaker_external_ids ORDER BY id"
            ).fetchall()
            == ext_ts1
        )


def test_satellites_verified_alias_protection_and_circuit_breaker(pg_env: None) -> None:
    with transaction() as conn:
        s1 = int(
            conn.execute(
                "INSERT INTO public.speakers (display_name, sort_name, wikidata_qid) VALUES ('Speaker Verified', 'Verified, Speaker', 'Q100') RETURNING id"
            ).fetchone()["id"]
        )
        conn.execute(
            """
            INSERT INTO public.speaker_aliases (speaker_id, alias_name, alias_type, locale, is_verified)
            VALUES
                (%s, 'Locked Alias', 'credit_variant', 'it', TRUE),
                (%s, 'Editable Alias', 'other', 'it', FALSE)
            """,
            (s1, s1),
        )

    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(seconds=5)
    with transaction() as conn:
        res = load_speaker_aliases(
            conn,
            [
                AliasRecord("Q100", "Locked Alias", alias_type="stage_name", locale="it"),
                AliasRecord("Q100", "Editable Alias", alias_type="stage_name", locale="it"),
            ],
            seen_at=t1,
        )
        rows = conn.execute(
            "SELECT alias_name, alias_type FROM public.speaker_aliases ORDER BY id"
        ).fetchall()
        res_unchanged = load_speaker_aliases(
            conn,
            [AliasRecord("Q100", "Locked Alias", alias_type="stage_name", locale="it")],
            seen_at=t2,
        )

    assert res.updated == 1
    assert rows == [
        {"alias_name": "Locked Alias", "alias_type": "credit_variant"},
        {"alias_name": "Editable Alias", "alias_type": "stage_name"},
    ]
    assert res_unchanged.unchanged == 1

    baseline_langs = [
        {
            "person": {"type": "uri", "value": "http://www.wikidata.org/entity/Q100"},
            "role": {"type": "literal", "value": "spoken"},
            "langCode": {"type": "literal", "value": code},
        }
        for code in ("it", "en", "fr", "es")
    ]
    run_satellites_sync(targets=("languages",), languages_bindings_override=baseline_langs)

    with pytest.raises(CircuitBreakerError):
        run_satellites_sync(
            targets=("languages",),
            languages_bindings_override=baseline_langs[:1],
        )

    with transaction() as conn:
        last_run = conn.execute(
            "SELECT source, status FROM public.etl_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert last_run == {
        "source": "wikidata_languages",
        "status": "aborted_circuit_breaker",
    }
