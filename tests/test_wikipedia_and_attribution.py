"""Unit and PostgreSQL integration tests for Wikipedia ETL and ATTRIBUTION.md exporter."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from etl.db import transaction
from etl.export_attribution import ComplianceExportError, build_attribution_markdown
from etl.run_wikidata_sync import CircuitBreakerError
from etl.run_wikipedia_sync import run_wikipedia_sync
from etl.wikipedia.extract import RawWikipediaPage
from etl.wikipedia.transform import (
    detect_compliance_flags,
    has_dubbing_section,
    parse_itwiki_bio_template,
    transform_wikipedia_pages,
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

    tmp_dir = Path(tempfile.mkdtemp(prefix="iddb_pg_wiki_test_"))
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


def test_parse_bio_and_anti_laundering_flags() -> None:
    sample_wikitext = """
{{F|biografie|maggio 2024}}
{{Bio
|Nome = Mario
|Cognome = Rossi
|Sesso = M
|AnnoNascita = [[1972]]
|Nazionalità = italiano
|Attività = doppiatore
}}
== Doppiaggio ==
* Personaggio da scheda su antoniogenna.net
"""
    flags = detect_compliance_flags(sample_wikitext, "it")
    assert flags == ("third_party_catalog_ref", "unsourced_template")
    assert has_dubbing_section(sample_wikitext) is True

    bio = parse_itwiki_bio_template(sample_wikitext)
    assert bio["given_name"] == "Mario"
    assert bio["surname"] == "Rossi"
    assert bio["gender"] == "male"
    assert bio["birth_year"] == 1972
    assert bio["country_code"] == "IT"

    copyviol_page = RawWikipediaPage(
        wiki_lang="it",
        page_id=999,
        revision_id=5000,
        page_title="Mario Copiato",
        page_url="https://it.wikipedia.org/wiki/Mario_Copiato",
        wikidata_qid=None,
        wikitext="{{Controlcopy|motivo=test}}\n{{Bio|Nome=Mario|Cognome=Copiato}}",
        content_hash="abc",
    )
    records, flags_map, metrics = transform_wikipedia_pages([copyviol_page])
    assert records == []
    assert flags_map[999] == ("copyviol_template",)
    assert metrics["copyviol_blocked"] == 1


def test_wikipedia_sync_idempotency_verified_protection_and_circuit_breaker(
    pg_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with transaction() as conn:
        with conn.cursor() as cur:
            # Existing unverified speaker missing birth_year and sort_name surname
            cur.execute(
                """
                INSERT INTO public.speakers (display_name, sort_name, wikidata_qid)
                VALUES ('Giulia Bianchi', 'Giulia Bianchi', 'Q1001')
                RETURNING id
                """
            )
            giulia_id = cur.fetchone()["id"]
            # Existing Wikidata sitelink stored as normalized page title (matching satellites.py)
            cur.execute(
                """
                INSERT INTO public.speaker_external_ids (speaker_id, provider, external_id, source)
                VALUES (%s, 'itwiki', 'Giulia_Bianchi', 'wikidata')
                """,
                (giulia_id,),
            )
            # Existing verified speaker must NOT be modified
            cur.execute(
                """
                INSERT INTO public.speakers (display_name, sort_name, wikidata_qid, is_verified)
                VALUES ('Carlo Verdi', 'Verdi, Carlo', 'Q1002', TRUE)
                """
            )

    sample_pages = [
        RawWikipediaPage(
            wiki_lang="it",
            page_id=101,
            revision_id=2001,
            page_title="Giulia Bianchi",
            page_url="https://it.wikipedia.org/wiki/Giulia_Bianchi",
            wikidata_qid="Q1001",
            wikitext="{{Bio|Nome=Giulia|Cognome=Bianchi|Sesso=F|AnnoNascita=1985|Nazionalità=italiana}}",
            content_hash="h1",
        ),
        RawWikipediaPage(
            wiki_lang="it",
            page_id=102,
            revision_id=2002,
            page_title="Carlo Verdi (doppiatore)",
            page_url="https://it.wikipedia.org/wiki/Carlo_Verdi_(doppiatore)",
            wikidata_qid="Q1002",
            wikitext="{{Bio|Nome=Carlo|Cognome=Verdi|Sesso=M|AnnoNascita=1960|Nazionalità=italiana}}",
            content_hash="h2",
        ),
        RawWikipediaPage(
            wiki_lang="it",
            page_id=103,
            revision_id=2003,
            page_title="Luca Neri",
            page_url="https://it.wikipedia.org/wiki/Luca_Neri",
            wikidata_qid="Q1003",
            wikitext="{{Bio|Nome=Luca|Cognome=Neri|Sesso=M|AnnoNascita=1978|Nazionalità=italiana}}\n== Doppiaggio ==\n* Ruolo X",
            content_hash="h3",
        ),
        RawWikipediaPage(
            wiki_lang="it",
            page_id=104,
            revision_id=2004,
            page_title="Elena Gialli",
            page_url="https://it.wikipedia.org/wiki/Elena_Gialli",
            wikidata_qid=None,
            wikitext="{{Bio|Nome=Elena|Cognome=Gialli|Sesso=F|AnnoNascita=1992|Nazionalità=italiana}}",
            content_hash="h4",
        ),
        RawWikipediaPage(
            wiki_lang="it",
            page_id=105,
            revision_id=2005,
            page_title="Voce Sospetta",
            page_url="https://it.wikipedia.org/wiki/Voce_Sospetta",
            wikidata_qid="Q1005",
            wikitext="{{ViolazioneCopyright}}\n{{Bio|Nome=Voce|Cognome=Sospetta}}",
            content_hash="h5",
        ),
    ]

    monkeypatch.setattr(
        "etl.run_wikipedia_sync.fetch_category_titles",
        lambda *_a, **_kw: [p.page_title for p in sample_pages],
    )
    monkeypatch.setattr(
        "etl.run_wikipedia_sync.fetch_pages_batch",
        lambda *_a, **_kw: sample_pages,
    )

    first = run_wikipedia_sync(wiki_lang="it")
    assert first["staging"] == {"inserted": 5, "unchanged": 0}
    assert first["load"]["speakers_inserted"] == 2
    assert first["load"]["new_with_qid"] == 1
    assert first["load"]["new_without_qid"] == 1
    assert first["load"]["speakers_updated"] == 1
    assert first["load"]["ext_ids_inserted"] == 3
    assert first["load"]["provenance_inserted"] == 4
    assert first["metrics"]["copyviol_blocked"] == 1

    # Dubbing cast headings stay strictly out of public.dubbing_credits
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM public.dubbing_credits")
            assert cur.fetchone()["cnt"] == 0
            cur.execute("SELECT birth_year, sort_name FROM public.speakers WHERE wikidata_qid = 'Q1001'")
            row_q1001 = cur.fetchone()
            assert row_q1001["birth_year"] == 1985
            assert row_q1001["sort_name"] == "Bianchi, Giulia"
            cur.execute("SELECT birth_year FROM public.speakers WHERE wikidata_qid = 'Q1002'")
            assert cur.fetchone()["birth_year"] is None

    # Second run on unchanged pages must produce zero writes everywhere
    second = run_wikipedia_sync(wiki_lang="it")
    assert second["staging"] == {"inserted": 0, "unchanged": 5}
    assert second["rows_inserted"] == 0
    assert second["rows_updated"] == 0

    # Circuit breaker trip if volume collapses below threshold
    monkeypatch.setattr(
        "etl.run_wikipedia_sync.fetch_pages_batch",
        lambda *_a, **_kw: [sample_pages[0]],
    )
    with pytest.raises(CircuitBreakerError):
        run_wikipedia_sync(wiki_lang="it")


def test_attribution_export_and_compliance_gate(pg_env: None) -> None:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.contributors (display_handle, accepted_cc_by_sa_4_0)
                VALUES ('verified_user', TRUE)
                RETURNING id
                """
            )
            ok_contrib = cur.fetchone()["id"]
            cur.execute(
                """
                INSERT INTO public.provenance_records (
                    entity_table, entity_id, source_type, source_lang,
                    source_license, source_url, source_revision_id, attribution_text
                )
                VALUES (
                    'speakers', 1, 'itwiki', 'it',
                    'CC-BY-SA-4.0', 'https://it.wikipedia.org/w/index.php?oldid=2001',
                    '2001', 'Contributors to Wikimedia projects (it.wikipedia.org, "Giulia Bianchi", rev 2001)'
                )
                """
            )
            cur.execute(
                """
                INSERT INTO public.provenance_records (
                    entity_table, entity_id, source_type, source_license,
                    attribution_text, distribution_edition, contributor_id
                )
                VALUES (
                    'dubbing_credits', 1, 'end_credits', 'CC-BY-SA-4.0',
                    'Verified Blu-ray credits', 'Blu-ray IT', %s
                )
                """,
                (ok_contrib,),
            )

        markdown, summary = build_attribution_markdown(conn)
        assert "CC BY-SA 4.0" in markdown
        assert "https://creativecommons.org/licenses/by-sa/4.0/" in markdown
        assert "Modifications Notice" in markdown
        assert "itwiki" in markdown
        assert "verified_user" in markdown
        assert summary["total_provenance_records"] == 2
        assert summary["citation_count"] == 2

        # Export must fail cleanly if a contributor has not accepted CC BY-SA 4.0
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.contributors
                SET accepted_cc_by_sa_4_0 = FALSE
                WHERE id = %s
                """,
                (ok_contrib,),
            )
        with pytest.raises(ComplianceExportError):
            build_attribution_markdown(conn)
