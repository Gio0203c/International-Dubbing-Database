"""Unit and PostgreSQL integration tests for Wikidata dubbing ETL."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from etl.db import transaction
from etl.run_wikidata_dubbing_sync import run_dubbing_sync
from etl.run_wikidata_sync import CircuitBreakerError
from etl.wikidata.dubbing import (
    load_characters,
    load_dubbing_credits,
    load_media_sources,
    load_studios,
    transform_character_bindings,
    transform_credit_bindings,
    transform_studio_bindings,
    transform_work_bindings,
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
    """Spin up an isolated ephemeral PostgreSQL instance with all project migrations."""
    pg_bin = _find_pg_bin()
    if pg_bin is None:
        pytest.skip("PostgreSQL server binaries (initdb/pg_ctl) not available")

    tmp_dir = Path(tempfile.mkdtemp(prefix="iddb_pg_dub_test_"))
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


def test_transform_dubbing_entities_filters_and_normalizes() -> None:
    studios, studio_discarded = transform_studio_bindings(
        [
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q101"},
                "name": {"value": "Studio Alpha"},
                "countryCodes": {"value": "it"},
                "city": {"value": "Roma"},
                "inceptionDates": {"value": "1980-01-01T00:00:00Z"},
                "dissolvedDates": {"value": "1994-01-01T00:00:00Z"},
                "replacedByQids": {"value": "http://www.wikidata.org/entity/Q102"},
                "followedByQids": {"value": ""},
                "parentOrgQids": {"value": ""},
                "websiteUrl": {"value": "https://example.org"},
                "altLabels": {"value": "Studio Alpha|Gruppo Alpha|S.A."},
            },
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q102"},
                "name": {"value": "Studio Beta"},
                "countryCodes": {"value": "IT"},
                "inceptionDates": {"value": "1994-01-01T00:00:00Z"},
            },
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q103"},
                "name": {"value": "_:blank"},
            },
        ]
    )
    assert studio_discarded == 1
    assert [s.wikidata_qid for s in studios] == ["Q101", "Q102"]
    assert studios[0].status == "merged"
    assert studios[0].closed_year == 1994
    assert studios[0].country_code == "IT"
    assert studios[0].alt_labels == ("Gruppo Alpha", "S.A.")
    assert studios[0].replaced_by_qids == ("Q102",)
    assert studios[1].status == "active"

    works, work_discarded = transform_work_bindings(
        [
            {
                "work": {"value": "http://www.wikidata.org/entity/Q201"},
                "originalTitle": {"value": "Spirited Away"},
                "italianTitle": {"value": "La città incantata"},
                "p31Qids": {"value": "http://www.wikidata.org/entity/Q20650540"},
                "releaseDates": {"value": "2001-07-20T00:00:00Z"},
                "originalLangs": {"value": "ja"},
                "countryCodes": {"value": "JP"},
                "imdbIds": {"value": "tt0245429"},
                "tmdbMovieIds": {"value": "129"},
            },
            {
                "work": {"value": "http://www.wikidata.org/entity/Q202"},
                "originalTitle": {"value": "Manga Only Title"},
                "p31Qids": {"value": "http://www.wikidata.org/entity/Q21198342"},
            },
        ]
    )
    assert work_discarded == 1
    assert len(works) == 1
    assert works[0].media_type == "movie"
    assert works[0].is_animated is True
    assert works[0].tmdb_media_type == "movie"
    assert works[0].tmdb_id == 129

    chars, char_discarded = transform_character_bindings(
        [
            {
                "character": {"value": "http://www.wikidata.org/entity/Q301"},
                "name": {"value": "Chihiro Ogino"},
                "workQids": {"value": "http://www.wikidata.org/entity/Q201"},
                "genderQids": {"value": "http://www.wikidata.org/entity/Q6581072"},
                "anilistCharIds": {"value": "555"},
            },
            {
                "character": {"value": "http://www.wikidata.org/entity/Q302"},
                "name": {"value": "Unmapped Gender Char"},
                "genderQids": {"value": "http://www.wikidata.org/entity/Q999999999"},
            },
            {
                "character": {"value": "http://www.wikidata.org/entity/Q303"},
                "name": {"value": "Missing Gender Char"},
            },
            {
                "character": {"value": "http://www.wikidata.org/entity/Q304"},
                "name": {"value": "Film Mistakenly Linked"},
                "p31Qids": {"value": "http://www.wikidata.org/entity/Q11424"},
            },
        ]
    )
    assert char_discarded == 1
    assert chars[0].gender == "female"
    assert chars[0].anilist_character_id == 555
    assert chars[1].gender == "other"
    assert chars[2].gender is None

    credits, credit_discarded = transform_credit_bindings(
        [
            {
                "work": {"value": "http://www.wikidata.org/entity/Q201"},
                "speaker": {"value": "http://www.wikidata.org/entity/Q501"},
                "character": {"value": "http://www.wikidata.org/entity/Q301"},
                "langCodes": {"value": "en|it"},
                "creditedAs": {"value": "Erica Necci"},
            },
            {
                "work": {"value": "http://www.wikidata.org/entity/Q201"},
                "speaker": {"value": "http://www.wikidata.org/entity/Q201"},
            },
        ]
    )
    assert credit_discarded == 1
    assert len(credits) == 1
    assert credits[0].lang_code == "it"

    # Reversed pipe-delimited multi-values and bilingual label priorities must stay deterministic.
    studios_a, _ = transform_studio_bindings(
        [
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q101"},
                "nameIt": {"value": "Studio Italiano"},
                "nameEn": {"value": "English Studio"},
                "cityIt": {"value": "Roma"},
                "cityEn": {"value": "Rome"},
            }
        ]
    )
    studios_b, _ = transform_studio_bindings(
        [
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q101"},
                "nameEn": {"value": "English Studio"},
                "nameIt": {"value": "Studio Italiano"},
                "cityEn": {"value": "Rome"},
                "cityIt": {"value": "Roma"},
            }
        ]
    )
    assert studios_a == studios_b
    assert studios_a[0].name == "Studio Italiano"
    assert studios_a[0].city == "Roma"

    works_a, _ = transform_work_bindings(
        [
            {
                "work": {"value": "http://www.wikidata.org/entity/Q201"},
                "titleProp": {"value": "Sen to Chihiro"},
                "labelEn": {"value": "Spirited Away"},
                "italianTitle": {"value": "La città incantata"},
                "imdbIds": {"value": "tt0245429|tt0999999"},
                "tmdbMovieIds": {"value": "129|999"},
            }
        ]
    )
    works_b, _ = transform_work_bindings(
        [
            {
                "work": {"value": "http://www.wikidata.org/entity/Q201"},
                "titleProp": {"value": "Sen to Chihiro"},
                "labelEn": {"value": "Spirited Away"},
                "italianTitle": {"value": "La città incantata"},
                "imdbIds": {"value": "tt0999999|tt0245429"},
                "tmdbMovieIds": {"value": "999|129"},
            }
        ]
    )
    assert works_a == works_b
    assert works_a[0].original_title == "Sen to Chihiro"
    assert works_a[0].imdb_tconst == "tt0245429"
    assert works_a[0].tmdb_id == 129

    chars_rev, _ = transform_character_bindings(
        [
            {
                "character": {"value": "http://www.wikidata.org/entity/Q301"},
                "nameIt": {"value": "Chihiro"},
                "nameEn": {"value": "Chihiro EN"},
                "workQids": {"value": "http://www.wikidata.org/entity/Q202|http://www.wikidata.org/entity/Q201"},
            }
        ]
    )
    assert chars_rev[0].work_qids == ("Q201", "Q202")

    credits_rev, _ = transform_credit_bindings(
        [
            {
                "work": {"value": "http://www.wikidata.org/entity/Q201"},
                "speaker": {"value": "http://www.wikidata.org/entity/Q501"},
                "langCodes": {"value": "ja|en"},
            }
        ]
    )
    assert credits_rev[0].lang_code == "en"


def test_load_dubbing_pipeline_idempotency_and_bridge_collision_safety(pg_env: None) -> None:
    studios, _ = transform_studio_bindings(
        [
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q101"},
                "name": {"value": "Gruppo Trenta"},
                "dissolvedDates": {"value": "1994-01-01T00:00:00Z"},
                "replacedByQids": {"value": "http://www.wikidata.org/entity/Q102"},
                "altLabels": {"value": "Pumais"},
            },
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q102"},
                "name": {"value": "PumaisDue"},
                "inceptionDates": {"value": "1994-01-01T00:00:00Z"},
            },
        ]
    )
    works, _ = transform_work_bindings(
        [
            {
                "work": {"value": "http://www.wikidata.org/entity/Q201"},
                "originalTitle": {"value": "Work One"},
                "italianTitle": {"value": "Opera Uno"},
                "p31Qids": {"value": "http://www.wikidata.org/entity/Q5398426"},
                "imdbIds": {"value": "tt0000123"},
                "tmdbTvIds": {"value": "456"},
            },
            {
                "work": {"value": "http://www.wikidata.org/entity/Q202"},
                "originalTitle": {"value": "Work Two Duplicate External"},
                "p31Qids": {"value": "http://www.wikidata.org/entity/Q5398426"},
                "imdbIds": {"value": "tt0000123"},
                "tmdbTvIds": {"value": "456"},
            },
        ]
    )
    chars, _ = transform_character_bindings(
        [
            {
                "character": {"value": "http://www.wikidata.org/entity/Q301"},
                "name": {"value": "Hero"},
                "workQids": {"value": "http://www.wikidata.org/entity/Q201"},
            }
        ]
    )
    credits, _ = transform_credit_bindings(
        [
            {
                "work": {"value": "http://www.wikidata.org/entity/Q201"},
                "speaker": {"value": "http://www.wikidata.org/entity/Q501"},
                "character": {"value": "http://www.wikidata.org/entity/Q301"},
            },
            {
                "work": {"value": "http://www.wikidata.org/entity/Q201"},
                "speaker": {"value": "http://www.wikidata.org/entity/Q9999"},
            },
        ]
    )

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.speakers (display_name, sort_name, wikidata_qid)
                VALUES ('Speaker One', 'One, Speaker', 'Q501')
                """
            )
        st1 = load_studios(conn, studios)
        wk1 = load_media_sources(conn, works)
        ch1 = load_characters(conn, chars)
        cr1 = load_dubbing_credits(conn, credits, italian_titles_by_qid={"Q201": "Opera Uno"})

    assert st1 == {
        "inserted": 2,
        "updated": 0,
        "aliases_inserted": 1,
        "aliases_updated": 0,
        "relationships_inserted": 1,
        "relationships_updated": 0,
    }
    assert wk1 == {"inserted": 2, "updated": 0, "ext_conflicts": 2}
    assert ch1 == {"inserted": 1, "updated": 0, "ext_conflicts": 0}
    assert cr1 == {
        "editions_inserted": 1,
        "editions_updated": 0,
        "credits_inserted": 1,
        "credits_updated": 0,
        "unresolved": 1,
    }

    with transaction() as conn:
        st2 = load_studios(conn, studios)
        wk2 = load_media_sources(conn, works)
        ch2 = load_characters(conn, chars)
        cr2 = load_dubbing_credits(conn, credits, italian_titles_by_qid={"Q201": "Opera Uno"})

    assert st2 == {
        "inserted": 0,
        "updated": 0,
        "aliases_inserted": 0,
        "aliases_updated": 0,
        "relationships_inserted": 0,
        "relationships_updated": 0,
    }
    assert wk2 == {"inserted": 0, "updated": 0, "ext_conflicts": 2}
    assert ch2 == {"inserted": 0, "updated": 0, "ext_conflicts": 0}
    assert cr2 == {
        "editions_inserted": 0,
        "editions_updated": 0,
        "credits_inserted": 0,
        "credits_updated": 0,
        "unresolved": 1,
    }


def test_verified_protection_and_circuit_breaker(pg_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    sample_studios = [
        {
            "studio": {"value": "http://www.wikidata.org/entity/Q101"},
            "name": {"value": "Original Studio"},
        },
        {
            "studio": {"value": "http://www.wikidata.org/entity/Q102"},
            "name": {"value": "Second Studio"},
        },
        {
            "studio": {"value": "http://www.wikidata.org/entity/Q103"},
            "name": {"value": "Third Studio"},
        },
    ]

    monkeypatch.setattr("etl.run_wikidata_dubbing_sync.fetch_sparql_bindings", lambda _q: sample_studios)
    first = run_dubbing_sync(only="studios")
    assert first["studios"]["rows_inserted"] == 3

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.studios
                SET name = 'Verified Manual Name', is_verified = TRUE
                WHERE wikidata_qid = 'Q101'
                """
            )

    monkeypatch.setattr(
        "etl.run_wikidata_dubbing_sync.fetch_sparql_bindings",
        lambda _q: [
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q101"},
                "name": {"value": "Different Wikidata Name"},
            },
            sample_studios[1],
            sample_studios[2],
        ],
    )
    second = run_dubbing_sync(only="studios")
    assert second["studios"]["rows_updated"] == 0

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM public.studios WHERE wikidata_qid = 'Q101'")
            assert cur.fetchone()["name"] == "Verified Manual Name"

    monkeypatch.setattr(
        "etl.run_wikidata_dubbing_sync.fetch_sparql_bindings",
        lambda _q: [sample_studios[0]],
    )
    with pytest.raises(CircuitBreakerError):
        run_dubbing_sync(only="studios")


def test_inverse_studio_lineage_and_wikipedia_backfill_preservation(pg_env: None) -> None:
    studios, _ = transform_studio_bindings(
        [
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q701"},
                "name": {"value": "Old Cooperative"},
                "countryCodes": {"value": "IT"},
            },
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q702"},
                "name": {"value": "Modern Group"},
                "countryCodes": {"value": "IT"},
                "inceptionDates": {"value": "1994-01-01T00:00:00Z"},
                "replacesQids": {"value": "http://www.wikidata.org/entity/Q701"},
                "childOrgQids": {"value": "http://www.wikidata.org/entity/Q703"},
            },
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q703"},
                "name": {"value": "Local Branch"},
                "countryCodes": {"value": "IT"},
            },
        ]
    )
    assert studios[1].replaces_qids == ("Q701",)
    assert studios[1].child_org_qids == ("Q703",)

    with transaction() as conn:
        first = load_studios(conn, studios)
        assert first["relationships_inserted"] == 2
        with conn.cursor() as cur:
            # Simulate Wikipedia backfilling city and a former_name alias on Q701
            cur.execute(
                "UPDATE public.studios SET city = 'Roma' WHERE wikidata_qid = 'Q701'"
            )
            cur.execute(
                """
                INSERT INTO public.studio_aliases (studio_id, alias_name, alias_type, source)
                SELECT id, 'OldCo', 'former_name', 'itwiki'
                FROM public.studios
                WHERE wikidata_qid = 'Q701'
                """
            )

    # Re-running Wikidata loader with altLabels matching OldCo and city=None must not clobber either
    studios_with_alt, _ = transform_studio_bindings(
        [
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q701"},
                "name": {"value": "Old Cooperative"},
                "countryCodes": {"value": "IT"},
                "altLabels": {"value": "OldCo"},
            },
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q702"},
                "name": {"value": "Modern Group"},
                "countryCodes": {"value": "IT"},
                "inceptionDates": {"value": "1994-01-01T00:00:00Z"},
                "replacesQids": {"value": "http://www.wikidata.org/entity/Q701"},
                "childOrgQids": {"value": "http://www.wikidata.org/entity/Q703"},
            },
            {
                "studio": {"value": "http://www.wikidata.org/entity/Q703"},
                "name": {"value": "Local Branch"},
                "countryCodes": {"value": "IT"},
            },
        ]
    )
    with transaction() as conn:
        second = load_studios(conn, studios_with_alt)
        assert second == {
            "inserted": 0,
            "updated": 0,
            "aliases_inserted": 0,
            "aliases_updated": 0,
            "relationships_inserted": 0,
            "relationships_updated": 0,
        }
        with conn.cursor() as cur:
            cur.execute("SELECT city FROM public.studios WHERE wikidata_qid = 'Q701'")
            assert cur.fetchone()["city"] == "Roma"
            cur.execute("SELECT alias_type FROM public.studio_aliases WHERE alias_name = 'OldCo'")
            assert cur.fetchone()["alias_type"] == "former_name"
            cur.execute(
                """
                SELECT s1.name AS src, sr.relationship_type, sr.event_year, s2.name AS dst
                FROM public.studio_relationships sr
                JOIN public.studios s1 ON s1.id = sr.studio_id
                JOIN public.studios s2 ON s2.id = sr.related_studio_id
                ORDER BY src, sr.relationship_type
                """
            )
            edges = cur.fetchall()
            assert len(edges) == 2
            assert (edges[0]["src"], edges[0]["relationship_type"], edges[0]["dst"]) == (
                "Local Branch",
                "subsidiary_of",
                "Modern Group",
            )
            assert (edges[1]["src"], edges[1]["relationship_type"], edges[1]["event_year"], edges[1]["dst"]) == (
                "Old Cooperative",
                "merged_into",
                1994,
                "Modern Group",
            )
