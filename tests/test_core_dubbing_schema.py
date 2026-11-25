"""PostgreSQL schema and constraint tests for the core dubbing model (migration 008)."""

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
    """Spin up an isolated ephemeral PostgreSQL instance with all migrations."""
    pg_bin = _find_pg_bin()
    if pg_bin is None:
        pytest.skip("PostgreSQL server binaries (initdb/pg_ctl) not available")

    tmp_dir = Path(tempfile.mkdtemp(prefix="iddb_pg_dubbing_test_"))
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


def test_core_dubbing_generated_ids_and_realistic_scenarios(pg_env: None) -> None:
    with transaction() as conn:
        sp_chiara = int(
            conn.execute(
                "INSERT INTO public.speakers (display_name, sort_name) VALUES ('Chiara Fabiano', 'Fabiano, Chiara') RETURNING id"
            ).fetchone()["id"]
        )
        sp_ernesto = int(
            conn.execute(
                "INSERT INTO public.speakers (display_name, sort_name) VALUES ('Ermavilo', 'Ermavilo') RETURNING id"
            ).fetchone()["id"]
        )
        sp_goku_1 = int(
            conn.execute(
                "INSERT INTO public.speakers (display_name, sort_name) VALUES ('Andrea Ward', 'Ward, Andrea') RETURNING id"
            ).fetchone()["id"]
        )
        sp_goku_2 = int(
            conn.execute(
                "INSERT INTO public.speakers (display_name, sort_name) VALUES ('Paolo Torrisi', 'Torrisi, Paolo') RETURNING id"
            ).fetchone()["id"]
        )

        # Prefixed public_ids and TMDB dual-namespace support (movie 550 vs tv 550)
        klara = conn.execute(
            """
            INSERT INTO public.media_sources (
                original_title, media_type, is_animated, release_year, original_lang,
                country_code, tmdb_media_type, tmdb_id, imdb_tconst
            )
            VALUES ('Klara and the Sun', 'movie', FALSE, 2026, 'en', 'US', 'movie', 550, 'tt12650000')
            RETURNING id, public_id
            """
        ).fetchone()
        series_same_tmdb = conn.execute(
            """
            INSERT INTO public.media_sources (
                original_title, media_type, is_animated, release_year, end_year,
                original_lang, country_code, tmdb_media_type, tmdb_id, anilist_media_id
            )
            VALUES ('Sample Animated Series', 'tv_series', TRUE, 1986, 1989, 'ja', 'JP', 'tv', 550, 223)
            RETURNING id, public_id
            """
        ).fetchone()
        ova_child = conn.execute(
            """
            INSERT INTO public.media_sources (
                original_title, media_type, is_animated, release_year,
                parent_media_id, wikidata_qid
            )
            VALUES ('Sample Special OVA', 'ova', TRUE, 1990, %s, 'Q123456')
            RETURNING id, public_id
            """,
            (series_same_tmdb["id"],),
        ).fetchone()
        # Documentary and audiobook formats are accepted cleanly
        conn.execute(
            """
            INSERT INTO public.media_sources (original_title, media_type, is_animated, release_year)
            VALUES
                ('Wildlife Documentary', 'documentary', FALSE, 2024),
                ('Classic Novel Audio', 'audiobook', NULL, 2023)
            """
        )

        studio = conn.execute(
            """
            INSERT INTO public.studios (name, country_code, city, founded_year, website_url)
            VALUES ('CDC Sefit Group', 'IT', 'Rome', 1932, 'https://example.org')
            RETURNING id, public_id
            """
        ).fetchone()

        assert klara["public_id"] == "md0000001"
        assert series_same_tmdb["public_id"] == "md0000002"
        assert ova_child["public_id"] == "md0000003"
        assert studio["public_id"] == "st0000001"

        # Editions: live-action Italian dub, historic redub, regional variants (es-ES vs es-MX)
        ed_klara_it = conn.execute(
            """
            INSERT INTO public.dubbing_editions (
                media_source_id, studio_id, lang_code, region_code, edition_type,
                release_year, localized_title
            )
            VALUES (%s, %s, 'it', NULL, 'dub', 2026, 'Klara e il sole')
            RETURNING id, public_id
            """,
            (klara["id"], studio["id"]),
        ).fetchone()
        ed_klara_redub = conn.execute(
            """
            INSERT INTO public.dubbing_editions (
                media_source_id, studio_id, lang_code, region_code, edition_type,
                edition_label, release_year
            )
            VALUES (%s, NULL, 'it', NULL, 'redub', 'Director Cut', 2028)
            RETURNING id, public_id
            """,
            (klara["id"],),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO public.dubbing_editions (media_source_id, lang_code, region_code, edition_type)
            VALUES
                (%s, 'es', 'ES', 'dub'),
                (%s, 'es', 'MX', 'dub'),
                (%s, 'it', NULL, 'dub')
            """,
            (klara["id"], klara["id"], series_same_tmdb["id"]),
        )
        ed_series_it = conn.execute(
            "SELECT id FROM public.dubbing_editions WHERE media_source_id = %s AND lang_code = 'it'",
            (series_same_tmdb["id"],),
        ).fetchone()["id"]

        assert ed_klara_it["public_id"] == "ed0000001"
        assert ed_klara_redub["public_id"] == "ed0000002"

        # Characters: work-linked character and franchise-level character (media_source_id IS NULL)
        ch_klara = conn.execute(
            """
            INSERT INTO public.characters (name, media_source_id, gender)
            VALUES ('Klara', %s, 'female')
            RETURNING id, public_id
            """,
            (klara["id"],),
        ).fetchone()
        ch_pumbaa = conn.execute(
            """
            INSERT INTO public.characters (name, media_source_id, gender, anilist_character_id)
            VALUES ('Shared Franchise Hero', NULL, 'male', 42)
            RETURNING id, public_id
            """
        ).fetchone()
        assert ch_klara["public_id"] == "ch0000001"
        assert ch_pumbaa["public_id"] == "ch0000002"

        # Realistic dubbing_credits:
        # 1) Live-action character + original_actor_name/QID (Klara | Jenna Ortega | Chiara Fabiano)
        # 2) Same speaker performing multiple roles in one edition + credited_as
        # 3) Voice recast via episode_range + all crew roles
        conn.execute(
            """
            INSERT INTO public.dubbing_credits (
                dubbing_edition_id, speaker_id, character_id, role_type,
                credited_as, original_actor_name, original_actor_qid, episode_range, is_main_cast
            )
            VALUES
                (%s, %s, %s, 'voice', NULL, 'Jenna Ortega', 'Q19866940', NULL, TRUE),
                (%s, %s, %s, 'voice', 'Daniele Viri', NULL, NULL, NULL, TRUE),
                (%s, %s, NULL, 'dubbing_director', 'Ernesto Brancucci', NULL, NULL, NULL, NULL),
                (%s, %s, NULL, 'dialogue_adapter', NULL, NULL, NULL, NULL, NULL),
                (%s, %s, NULL, 'assistant_director', NULL, NULL, NULL, NULL, NULL),
                (%s, %s, NULL, 'sound_engineer', NULL, NULL, NULL, NULL, NULL),
                (%s, %s, NULL, 'musical_director', NULL, NULL, NULL, NULL, NULL),
                (%s, %s, NULL, 'theme_song', NULL, NULL, NULL, 'ep 1-52', NULL),
                (%s, %s, %s, 'voice', NULL, NULL, NULL, 'ep 1-54', TRUE),
                (%s, %s, %s, 'voice', NULL, NULL, NULL, 'ep 55-153', TRUE)
            """,
            (
                ed_klara_it["id"], sp_chiara, ch_klara["id"],
                ed_series_it, sp_ernesto, ch_pumbaa["id"],
                ed_series_it, sp_ernesto,
                ed_series_it, sp_ernesto,
                ed_series_it, sp_ernesto,
                ed_series_it, sp_ernesto,
                ed_series_it, sp_ernesto,
                ed_series_it, sp_ernesto,
                ed_series_it, sp_goku_1, ch_pumbaa["id"],
                ed_series_it, sp_goku_2, ch_pumbaa["id"],
            ),
        )

        # Query directors directly and inspect live-action projection
        directors = conn.execute(
            """
            SELECT DISTINCT s.display_name
            FROM public.dubbing_credits dc
            JOIN public.speakers s ON s.id = dc.speaker_id
            WHERE dc.role_type = 'dubbing_director'
            """
        ).fetchall()
        klara_row = conn.execute(
            """
            SELECT ch.name AS character_name, dc.original_actor_name, dc.original_actor_qid, s.display_name AS speaker_name
            FROM public.dubbing_credits dc
            JOIN public.characters ch ON ch.id = dc.character_id
            JOIN public.speakers s ON s.id = dc.speaker_id
            WHERE dc.dubbing_edition_id = %s
            """,
            (ed_klara_it["id"],),
        ).fetchall()

    assert directors == [{"display_name": "Ermavilo"}]
    assert klara_row == [
        {
            "character_name": "Klara",
            "original_actor_name": "Jenna Ortega",
            "original_actor_qid": "Q19866940",
            "speaker_name": "Chiara Fabiano",
        }
    ]


def test_core_dubbing_constraints_and_cascades(pg_env: None) -> None:
    with transaction() as conn:
        sp_id = int(
            conn.execute(
                "INSERT INTO public.speakers (display_name, sort_name) VALUES ('Test Actor', 'Actor, Test') RETURNING id"
            ).fetchone()["id"]
        )
        m1 = int(
            conn.execute(
                "INSERT INTO public.media_sources (original_title, media_type, release_year, tmdb_media_type, tmdb_id) VALUES ('Work 1', 'movie', 2020, 'movie', 100) RETURNING id"
            ).fetchone()["id"]
        )
        st1 = int(
            conn.execute(
                "INSERT INTO public.studios (name) VALUES ('Studio Temp') RETURNING id"
            ).fetchone()["id"]
        )
        ed1 = int(
            conn.execute(
                "INSERT INTO public.dubbing_editions (media_source_id, studio_id, lang_code, edition_type) VALUES (%s, %s, 'it', 'dub') RETURNING id",
                (m1, st1),
            ).fetchone()["id"]
        )
        ch1 = int(
            conn.execute(
                "INSERT INTO public.characters (name, media_source_id) VALUES ('Char Temp', %s) RETURNING id",
                (m1,),
            ).fetchone()["id"]
        )
        conn.execute(
            "INSERT INTO public.dubbing_credits (dubbing_edition_id, speaker_id, character_id, role_type) VALUES (%s, %s, %s, 'voice')",
            (ed1, sp_id, ch1),
        )

    # Check violations on media_sources
    bad_media_sql = [
        ("INSERT INTO public.media_sources (original_title, media_type) VALUES ('   ', 'movie')", ()),
        ("INSERT INTO public.media_sources (original_title, media_type) VALUES ('X', 'podcast')", ()),
        ("INSERT INTO public.media_sources (original_title, media_type, release_year, end_year) VALUES ('X', 'tv_series', 2022, 2020)", ()),
        ("INSERT INTO public.media_sources (original_title, media_type, tmdb_id) VALUES ('X', 'movie', 999)", ()),
        ("INSERT INTO public.media_sources (original_title, media_type, tmdb_media_type) VALUES ('X', 'movie', 'movie')", ()),
        ("INSERT INTO public.media_sources (original_title, media_type, imdb_tconst) VALUES ('X', 'movie', 'nm123')", ()),
    ]
    for sql, params in bad_media_sql:
        with pytest.raises(psycopg.errors.CheckViolation):
            with transaction() as conn:
                conn.execute(sql, params)

    with pytest.raises(psycopg.errors.CheckViolation):
        with transaction() as conn:
            conn.execute(
                "UPDATE public.media_sources SET parent_media_id = id WHERE id = %s",
                (m1,),
            )

    # Check violations on studios, editions, and credits
    bad_other = [
        ("INSERT INTO public.studios (name, website_url) VALUES ('S', 'ftp://bad.example')", ()),
        ("INSERT INTO public.dubbing_editions (media_source_id, lang_code, edition_type) VALUES (%s, 'ita', 'dub')", (m1,)),
        ("INSERT INTO public.dubbing_editions (media_source_id, lang_code, edition_type) VALUES (%s, 'it', 'fansub')", (m1,)),
        ("INSERT INTO public.dubbing_credits (dubbing_edition_id, speaker_id, role_type) VALUES (%s, %s, 'producer')", (ed1, sp_id)),
        ("INSERT INTO public.dubbing_credits (dubbing_edition_id, speaker_id, credited_as) VALUES (%s, %s, '  ')", (ed1, sp_id)),
        ("INSERT INTO public.dubbing_credits (dubbing_edition_id, speaker_id, episode_range) VALUES (%s, %s, '')", (ed1, sp_id)),
        ("INSERT INTO public.dubbing_credits (dubbing_edition_id, speaker_id, original_actor_name) VALUES (%s, %s, ' ')", (ed1, sp_id)),
        ("INSERT INTO public.dubbing_credits (dubbing_edition_id, speaker_id, original_actor_qid) VALUES (%s, %s, 'P123')", (ed1, sp_id)),
    ]
    for sql, params in bad_other:
        with pytest.raises(psycopg.errors.CheckViolation):
            with transaction() as conn:
                conn.execute(sql, params)

    # Unique violations across (tmdb_media_type, tmdb_id), dubbing_editions, and dubbing_credits
    with pytest.raises(psycopg.errors.UniqueViolation):
        with transaction() as conn:
            conn.execute(
                "INSERT INTO public.media_sources (original_title, media_type, tmdb_media_type, tmdb_id) VALUES ('Dup TMDB', 'movie', 'movie', 100)"
            )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with transaction() as conn:
            conn.execute(
                "INSERT INTO public.dubbing_editions (media_source_id, lang_code, edition_type) VALUES (%s, 'it', 'dub')",
                (m1,),
            )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with transaction() as conn:
            conn.execute(
                "INSERT INTO public.dubbing_credits (dubbing_edition_id, speaker_id, character_id, role_type) VALUES (%s, %s, %s, 'voice')",
                (ed1, sp_id, ch1),
            )

    # ON DELETE SET NULL for studios and characters; ON DELETE CASCADE from media_source -> editions -> credits
    with transaction() as conn:
        conn.execute("DELETE FROM public.studios WHERE id = %s", (st1,))
        conn.execute("DELETE FROM public.characters WHERE id = %s", (ch1,))
        ed_row = conn.execute(
            "SELECT studio_id FROM public.dubbing_editions WHERE id = %s", (ed1,)
        ).fetchone()
        cr_row = conn.execute(
            "SELECT character_id FROM public.dubbing_credits WHERE dubbing_edition_id = %s",
            (ed1,),
        ).fetchone()
    assert ed_row == {"studio_id": None}
    assert cr_row == {"character_id": None}

    with transaction() as conn:
        conn.execute("DELETE FROM public.media_sources WHERE id = %s", (m1,))
        eds_left = conn.execute("SELECT COUNT(*) AS n FROM public.dubbing_editions WHERE id = %s", (ed1,)).fetchone()["n"]
        credits_left = conn.execute("SELECT COUNT(*) AS n FROM public.dubbing_credits WHERE dubbing_edition_id = %s", (ed1,)).fetchone()["n"]
    assert (eds_left, credits_left) == (0, 0)
