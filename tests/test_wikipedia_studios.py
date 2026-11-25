"""Unit and PostgreSQL integration tests for Wikipedia studio enrichment ETL."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from etl.db import transaction
from etl.export_attribution import build_attribution_markdown
from etl.run_wikidata_sync import CircuitBreakerError
from etl.run_wikipedia_studios_sync import run_wikipedia_studios_sync
from etl.wikidata.dubbing import load_studios, transform_studio_bindings
from etl.wikipedia.extract import RawWikipediaPage
from etl.wikipedia.studios import (
    RawWikipediaStudioPage,
    parse_studio_infobox,
    transform_wikipedia_studio_pages,
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

    tmp_dir = Path(tempfile.mkdtemp(prefix="iddb_pg_wiki_studios_test_"))
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


def test_parse_studio_infobox_it_and_en() -> None:
    it_wikitext = """
{{Azienda
|nome = SEDIF
|data fondazione = 1974
|luogo fondazione = Roma
|data chiusura = 2022
|causa chiusura = Acquisita dalla multinazionale francese [[Dubbing Brothers]]
|nazione = ITA
|sede = Roma
|sito = {{URL|https://www.example.it/}}
}}
La '''SEDIF''' è stata una società di doppiaggio italiana.
"""
    it_parsed = parse_studio_infobox(it_wikitext, "it")
    assert it_parsed["has_infobox"] is True
    assert it_parsed["founded_year"] == 1974
    assert it_parsed["closed_year"] == 2022
    assert it_parsed["country_code"] == "IT"
    assert it_parsed["city"] == "Roma"
    assert it_parsed["website_url"] == "https://www.example.it/"
    assert it_parsed["closure_targets"] == ("Dubbing Brothers",)

    en_wikitext = """
{{Infobox company
| name = Example Dubs
| hq_location = Paris, France<br>With 4 facilities
| hq_location_country = France
| founded = {{Start date|1989}}
| parent = Parent Media
| owner = [[Parent Media]]
| website = [https://www.dub-example.com/ Website]
}}
"""
    en_parsed = parse_studio_infobox(en_wikitext, "en")
    assert en_parsed["has_infobox"] is True
    assert en_parsed["founded_year"] == 1989
    assert en_parsed["country_code"] == "FR"
    assert en_parsed["city"] is None
    assert en_parsed["parent_targets"] == ("Parent Media",)
    assert en_parsed["website_url"] == "https://www.dub-example.com/"


def test_transform_studio_compliance_and_alias_classification() -> None:
    pages = [
        RawWikipediaStudioPage(
            page=RawWikipediaPage(
                wiki_lang="it",
                page_id=10,
                revision_id=1010,
                page_title="Pumais Due",
                page_url="https://it.wikipedia.org/wiki/Pumais_Due",
                wikidata_qid="Q3925552",
                wikitext=(
                    "{{Azienda|nome=Pumais Due|nazione=ITA|sede=Roma|data fondazione=1980}}\n"
                    "'''Pumais Due''', anche nota come '''Pumaisdue''', è un'azienda italiana, "
                    "attuale incarnazione della società di doppiaggio '''Gruppo Trenta'''. "
                    "Ha cambiato il proprio nome in '''Pumais'''."
                ),
                content_hash="h10",
            ),
            redirect_titles=("Gruppo Trenta", "Pumais", "Pumaisdue", "pumais due"),
            from_category=True,
        ),
        RawWikipediaStudioPage(
            page=RawWikipediaPage(
                wiki_lang="it",
                page_id=11,
                revision_id=1011,
                page_title="Società Attori Sincronizzatori",
                page_url="https://it.wikipedia.org/wiki/Societ%C3%A0_Attori_Sincronizzatori",
                wikidata_qid="Q86672487",
                wikitext=(
                    "{{Azienda|nome=Società Attori Sincronizzatori|nazione=ITA|luogo fondazione=[[Roma]]|"
                    "data fondazione=1958|data chiusura=[[2008]]}}\n"
                    "La '''Società Attori Sincronizzatori''', spesso abbreviata in '''S.A.S.''', "
                    "è stata una società di doppiaggio italiana."
                ),
                content_hash="h11",
            ),
            redirect_titles=(),
            from_category=True,
        ),
        RawWikipediaStudioPage(
            page=RawWikipediaPage(
                wiki_lang="it",
                page_id=12,
                revision_id=1012,
                page_title="Unsourced Studio",
                page_url="https://it.wikipedia.org/wiki/Unsourced_Studio",
                wikidata_qid="Q999012",
                wikitext=(
                    "{{F|aziende|2025}}\n"
                    "{{Azienda|nome=Unsourced Studio|nazione=ITA|sede=Milano|data fondazione=1999}}\n"
                    "La '''Unsourced Studio''' è una società di doppiaggio."
                ),
                content_hash="h12",
            ),
            redirect_titles=("Old Unsourced Name",),
            from_category=True,
        ),
        RawWikipediaStudioPage(
            page=RawWikipediaPage(
                wiki_lang="it",
                page_id=13,
                revision_id=1013,
                page_title="Copyvio Studio",
                page_url="https://it.wikipedia.org/wiki/Copyvio_Studio",
                wikidata_qid="Q999013",
                wikitext="{{Controlcopy}}\n{{Azienda|nome=Copyvio Studio|nazione=ITA}}",
                content_hash="h13",
            ),
            redirect_titles=(),
            from_category=True,
        ),
        RawWikipediaStudioPage(
            page=RawWikipediaPage(
                wiki_lang="it",
                page_id=14,
                revision_id=1014,
                page_title="Generic Audio Company",
                page_url="https://it.wikipedia.org/wiki/Generic_Audio_Company",
                wikidata_qid=None,
                wikitext="{{Azienda|nome=Generic Audio|settore=televisione}}\nSocietà televisiva generalista.",
                content_hash="h14",
            ),
            redirect_titles=(),
            from_category=False,
        ),
    ]
    wd_entities = {
        "Q3925552": {
            "labels": {"it": {"value": "Pumaisdue"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q4830453"}}}}],
                "P17": [{"mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q38"}}}}],
                "P159": [{"mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q220"}}}}],
                "P571": [{"mainsnak": {"datavalue": {"type": "time", "value": {"time": "+1980-00-00T00:00:00Z"}}}}],
            },
        },
        "Q999012": {
            "labels": {"it": {"value": "Unsourced Studio"}},
            "aliases": {"it": [{"value": "WD Alias"}]},
            "claims": {
                "P17": [{"mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q38"}}}}],
                "P571": [{"mainsnak": {"datavalue": {"type": "time", "value": {"time": "+1994-00-00T00:00:00Z"}}}}],
            },
        },
    }
    aux_entities = {
        "Q38": {
            "labels": {"it": {"value": "Italia"}},
            "claims": {"P297": [{"mainsnak": {"datavalue": {"type": "string", "value": "IT"}}}]},
        },
        "Q220": {"labels": {"it": {"value": "Roma"}}},
    }

    records, flags_map, metrics = transform_wikipedia_studio_pages(pages, wd_entities, aux_entities)
    assert metrics["copyviol_blocked"] == 1
    assert metrics["unsourced_flagged"] == 1
    assert metrics["non_studio_skipped"] == 1
    assert flags_map[13] == ("copyviol_template",)
    assert len(records) == 3

    pumais = next(r for r in records if r.wikidata_qid == "Q3925552")
    alias_map = {a.alias_name: a.alias_type for a in pumais.aliases}
    assert alias_map["Gruppo Trenta"] == "former_name"
    assert alias_map["Pumais"] == "former_name"
    assert "Pumaisdue" not in alias_map  # matches canonical Wikidata name
    assert alias_map["Pumais Due"] == "other"
    assert pumais.missing_wikidata_studio_type is True

    sas = next(r for r in records if r.wikidata_qid == "Q86672487")
    assert sas.status == "closed"
    assert sas.closed_year == 2008
    assert sas.aliases[0].alias_name == "S.A.S."
    assert sas.aliases[0].alias_type == "abbreviation"

    unsourced = next(r for r in records if r.wikidata_qid == "Q999012")
    assert unsourced.allow_wikitext_backfill is False
    # Wikidata CC0 attributes kept, wikitext city='Milano' and redirect suppressed
    assert unsourced.founded_year == 1994
    assert unsourced.city is None
    assert [a.alias_name for a in unsourced.aliases] == ["WD Alias"]


def test_wikipedia_studios_sync_idempotency_verified_protection_and_attribution(
    pg_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with transaction() as conn:
        with conn.cursor() as cur:
            # Pre-seed an existing Wikidata studio missing city, plus one verified studio
            cur.execute(
                """
                INSERT INTO public.studios (wikidata_qid, name, status, country_code, founded_year, primary_source)
                VALUES ('Q24933910', 'Cine Video Doppiatori', 'active', 'IT', 1970, 'wikidata'),
                       ('Q888', 'Verified Dub Studio', 'active', 'IT', 1990, 'wikidata')
                """
            )
            cur.execute("UPDATE public.studios SET is_verified = TRUE WHERE wikidata_qid = 'Q888'")

    sample_pages = [
        RawWikipediaStudioPage(
            page=RawWikipediaPage(
                wiki_lang="it",
                page_id=201,
                revision_id=5201,
                page_title="Cine Video Doppiatori",
                page_url="https://it.wikipedia.org/wiki/Cine_Video_Doppiatori",
                wikidata_qid="Q24933910",
                wikitext=(
                    "{{Azienda|nome=Cine Video Doppiatori|nazione=ITA|luogo fondazione=[[Roma]]|data fondazione=1970}}\n"
                    "La '''Cine Video Doppiatori''' ('''C.V.D.''') è una società di doppiaggio."
                ),
                content_hash="sh1",
            ),
            redirect_titles=(),
            from_category=True,
        ),
        RawWikipediaStudioPage(
            page=RawWikipediaPage(
                wiki_lang="it",
                page_id=202,
                revision_id=5202,
                page_title="Pumais Due",
                page_url="https://it.wikipedia.org/wiki/Pumais_Due",
                wikidata_qid="Q3925552",
                wikitext=(
                    "{{Azienda|nome=Pumais Due|nazione=ITA|sede=Roma|data fondazione=1980}}\n"
                    "'''Pumais Due''' è l'attuale incarnazione di '''Gruppo Trenta'''."
                ),
                content_hash="sh2",
            ),
            redirect_titles=("Gruppo Trenta", "Pumais"),
            from_category=True,
        ),
        RawWikipediaStudioPage(
            page=RawWikipediaPage(
                wiki_lang="it",
                page_id=203,
                revision_id=5203,
                page_title="Legacy Studio",
                page_url="https://it.wikipedia.org/wiki/Legacy_Studio",
                wikidata_qid="Q3943567",
                wikitext=(
                    "{{Azienda|nome=Legacy Studio|nazione=ITA|data fondazione=1974|data chiusura=2022|"
                    "causa chiusura=Acquisita da [[Pumais Due]]}}\n"
                    "Società di doppiaggio acquisita."
                ),
                content_hash="sh3",
            ),
            redirect_titles=(),
            from_category=True,
        ),
        RawWikipediaStudioPage(
            page=RawWikipediaPage(
                wiki_lang="it",
                page_id=204,
                revision_id=5204,
                page_title="Verified Dub Studio",
                page_url="https://it.wikipedia.org/wiki/Verified_Dub_Studio",
                wikidata_qid="Q888",
                wikitext="{{Azienda|nome=Verified Dub Studio|sede=Torino|nazione=IT}}",
                content_hash="sh4",
            ),
            redirect_titles=(),
            from_category=True,
        ),
    ]
    sample_wd = {
        "Q24933910": {
            "labels": {"it": {"value": "Cine Video Doppiatori"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q1298801"}}}}],
                "P17": [{"mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q38"}}}}],
            },
        },
        "Q3925552": {
            "labels": {"it": {"value": "Pumaisdue"}},
            "claims": {
                "P17": [{"mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q38"}}}}],
                "P159": [{"mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q220"}}}}],
                "P571": [{"mainsnak": {"datavalue": {"type": "time", "value": {"time": "+1980-00-00T00:00:00Z"}}}}],
            },
        },
    }
    sample_aux = {
        "Q38": {"labels": {"it": {"value": "Italia"}}, "claims": {"P297": [{"mainsnak": {"datavalue": {"type": "string", "value": "IT"}}}]}},
        "Q220": {"labels": {"it": {"value": "Roma"}}},
    }

    monkeypatch.setattr(
        "etl.run_wikipedia_studios_sync.discover_studio_titles",
        lambda *_a, **_kw: ([p.page.page_title for p in sample_pages], {p.page.page_title for p in sample_pages}),
    )
    monkeypatch.setattr(
        "etl.run_wikipedia_studios_sync.fetch_studio_pages_batch",
        lambda *_a, **_kw: sample_pages,
    )
    monkeypatch.setattr(
        "etl.run_wikipedia_studios_sync.fetch_wikidata_studio_entities",
        lambda *_a, **_kw: (sample_wd, sample_aux),
    )

    first = run_wikipedia_studios_sync(wiki_lang="it")
    assert first["staging"] == {"inserted": 4, "unchanged": 0}
    assert first["load"]["studios_inserted"] == 2
    assert first["load"]["studios_updated"] == 1  # only Cine Video Doppiatori city='Roma'
    assert first["load"]["relationships_inserted"] == 1
    assert first["missing_wikidata_studio_type_qids"] == [("Q3925552", "Pumaisdue")]

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT city FROM public.studios WHERE wikidata_qid = 'Q888'")
            assert cur.fetchone()["city"] is None  # verified studio untouched
            cur.execute("SELECT city FROM public.studios WHERE wikidata_qid = 'Q24933910'")
            assert cur.fetchone()["city"] == "Roma"
            cur.execute(
                """
                SELECT st.name, sa.alias_type
                FROM public.studios st
                JOIN public.studio_aliases sa ON sa.studio_id = st.id
                WHERE sa.alias_name = 'Gruppo Trenta'
                """
            )
            gt_row = cur.fetchone()
            assert (gt_row["name"], gt_row["alias_type"]) == ("Pumaisdue", "former_name")

        markdown, summary = build_attribution_markdown(conn)
        assert "oldid=5202" in markdown
        assert summary["citation_count"] >= 4

    # Pass 2 idempotency
    second = run_wikipedia_studios_sync(wiki_lang="it")
    assert second["staging"] == {"inserted": 0, "unchanged": 4}
    assert second["rows_inserted"] == 0
    assert second["rows_updated"] == 0

    # Cross-pipeline idempotency with Wikidata load_studios
    wd_records, _ = transform_studio_bindings(
        [
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q24933910"},
                "nameIt": {"value": "Cine Video Doppiatori"},
                "countryCodes": {"value": "IT"},
                "inceptionDates": {"value": "1970-01-01T00:00:00Z"},
            }
        ]
    )
    with transaction() as conn:
        cross = load_studios(conn, wd_records)
        assert cross == {
            "inserted": 0,
            "updated": 0,
            "aliases_inserted": 0,
            "aliases_updated": 0,
            "relationships_inserted": 0,
            "relationships_updated": 0,
        }

    # Circuit breaker test
    monkeypatch.setattr(
        "etl.run_wikipedia_studios_sync.fetch_studio_pages_batch",
        lambda *_a, **_kw: [sample_pages[0]],
    )
    with pytest.raises(CircuitBreakerError):
        run_wikipedia_studios_sync(wiki_lang="it")


def test_homonym_qids_do_not_merge_or_oscillate(
    pg_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.studios (wikidata_qid, name, status, country_code, primary_source)
                VALUES ('Q135224615', 'Iyuno', 'closed', 'DK', 'wikidata'),
                       ('Q11854634', 'Iyuno', 'active', NULL, 'wikidata')
                """
            )

    en_pages = [
        RawWikipediaStudioPage(
            page=RawWikipediaPage(
                wiki_lang="en",
                page_id=301,
                revision_id=6301,
                page_title="Iyuno",
                page_url="https://en.wikipedia.org/wiki/Iyuno",
                wikidata_qid="Q7389448",
                wikitext="{{Infobox company|name=Iyuno|hq_location_country=United States|founded=1974}}",
                content_hash="eh1",
            ),
            redirect_titles=("SDI Media",),
            from_category=True,
        )
    ]
    en_wd = {
        "Q7389448": {
            "labels": {"en": {"value": "Iyuno"}},
            "claims": {
                "P17": [{"mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q30"}}}}],
            },
        }
    }
    en_aux = {
        "Q30": {"labels": {"en": {"value": "United States"}}, "claims": {"P297": [{"mainsnak": {"datavalue": {"type": "string", "value": "US"}}}]}},
    }

    monkeypatch.setattr(
        "etl.run_wikipedia_studios_sync.discover_studio_titles",
        lambda *_a, **_kw: (["Iyuno"], {"Iyuno"}),
    )
    monkeypatch.setattr(
        "etl.run_wikipedia_studios_sync.fetch_studio_pages_batch",
        lambda *_a, **_kw: en_pages,
    )
    monkeypatch.setattr(
        "etl.run_wikipedia_studios_sync.fetch_wikidata_studio_entities",
        lambda *_a, **_kw: (en_wd, en_aux),
    )

    first = run_wikipedia_studios_sync(wiki_lang="en")
    assert first["load"]["studios_inserted"] == 1
    assert first["load"]["studios_updated"] == 0

    second = run_wikipedia_studios_sync(wiki_lang="en")
    assert second["rows_inserted"] == 0
    assert second["rows_updated"] == 0

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT wikidata_qid, country_code FROM public.studios WHERE name = 'Iyuno' ORDER BY wikidata_qid")
            rows = cur.fetchall()
            assert [(r["wikidata_qid"], r["country_code"]) for r in rows] == [
                ("Q11854634", None),
                ("Q135224615", "DK"),
                ("Q7389448", "US"),
            ]
