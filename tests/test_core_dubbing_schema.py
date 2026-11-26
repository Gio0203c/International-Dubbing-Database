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
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
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


def test_studio_aliases_status_and_lineage(pg_env: None) -> None:
    with transaction() as conn:
        # Scenario 1: Single company renaming over time (Gruppo Trenta -> Pumais -> Pumaisdue)
        st_pumaisdue = int(
            conn.execute(
                """
                INSERT INTO public.studios (name, status, country_code, city, founded_year)
                VALUES ('Pumaisdue', 'active', 'IT', 'Rome', 1980)
                RETURNING id
                """
            ).fetchone()["id"]
        )
        conn.execute(
            """
            INSERT INTO public.studio_aliases (studio_id, alias_name, alias_type, start_year, end_year)
            VALUES
                (%s, 'Gruppo Trenta', 'former_name', 1980, 1994),
                (%s, 'Pumais', 'former_name', 1994, 2000)
            """,
            (st_pumaisdue, st_pumaisdue),
        )
        m_retro = int(
            conn.execute(
                "INSERT INTO public.media_sources (original_title, media_type, release_year) VALUES ('Legacy Film', 'movie', 1988) RETURNING id"
            ).fetchone()["id"]
        )
        conn.execute(
            """
            INSERT INTO public.dubbing_editions (media_source_id, studio_id, studio_credited_as, lang_code, edition_type, release_year)
            VALUES (%s, %s, 'Gruppo Trenta', 'it', 'dub', 1988)
            """,
            (m_retro, st_pumaisdue),
        )

        # Scenario 2: Distinct studios merging (CDC + Sefit -> CDC Sefit Group)
        st_cdc = int(
            conn.execute(
                "INSERT INTO public.studios (name, status, founded_year, closed_year) VALUES ('CDC', 'merged', 1932, 1994) RETURNING id"
            ).fetchone()["id"]
        )
        st_sefit = int(
            conn.execute(
                "INSERT INTO public.studios (name, status, founded_year, closed_year) VALUES ('Sefit', 'merged', 1964, 1994) RETURNING id"
            ).fetchone()["id"]
        )
        st_cdc_sefit = int(
            conn.execute(
                "INSERT INTO public.studios (name, status, founded_year) VALUES ('CDC Sefit Group', 'active', 1994) RETURNING id"
            ).fetchone()["id"]
        )
        conn.execute(
            """
            INSERT INTO public.studio_relationships (studio_id, related_studio_id, relationship_type, event_year)
            VALUES
                (%s, %s, 'merged_into', 1994),
                (%s, %s, 'merged_into', 1994)
            """,
            (st_cdc, st_cdc_sefit, st_sefit, st_cdc_sefit),
        )

        # Scenario 3: Multi-hop lineage (SDI Media -> Iyuno-SDI -> Iyuno) + closed without year
        st_sdi = int(
            conn.execute("INSERT INTO public.studios (name, status) VALUES ('SDI Media', 'merged') RETURNING id").fetchone()["id"]
        )
        st_iyuno_sdi = int(
            conn.execute("INSERT INTO public.studios (name, status) VALUES ('Iyuno-SDI', 'merged') RETURNING id").fetchone()["id"]
        )
        st_iyuno = int(
            conn.execute("INSERT INTO public.studios (name, status) VALUES ('Iyuno', 'active') RETURNING id").fetchone()["id"]
        )
        conn.execute("INSERT INTO public.studios (name, status) VALUES ('Defunct Unnamed Studio', 'closed')")
        conn.execute(
            """
            INSERT INTO public.studio_relationships (studio_id, related_studio_id, relationship_type, event_year)
            VALUES
                (%s, %s, 'merged_into', 2021),
                (%s, %s, 'succeeded_by', 2022)
            """,
            (st_sdi, st_iyuno_sdi, st_iyuno_sdi, st_iyuno),
        )

        # Verify alias resolution and recursive multi-hop resolution
        resolved_alias = conn.execute(
            """
            SELECT s.name, s.status
            FROM public.studio_aliases sa
            JOIN public.studios s ON s.id = sa.studio_id
            WHERE lower(sa.alias_name) = lower('Gruppo Trenta')
            """
        ).fetchone()
        resolved_lineage = conn.execute(
            """
            WITH RECURSIVE lineage AS (
                SELECT id, name, status, 0 AS hops, ARRAY[id] AS path
                FROM public.studios
                WHERE name = 'SDI Media'
                UNION ALL
                SELECT nxt.id, nxt.name, nxt.status, l.hops + 1, l.path || nxt.id
                FROM lineage l
                JOIN public.studio_relationships sr ON sr.studio_id = l.id
                JOIN public.studios nxt ON nxt.id = sr.related_studio_id
                WHERE NOT (nxt.id = ANY(l.path))
            )
            SELECT name, status, hops
            FROM lineage
            ORDER BY hops DESC
            LIMIT 1
            """
        ).fetchone()

    assert resolved_alias == {"name": "Pumaisdue", "status": "active"}
    assert resolved_lineage == {"name": "Iyuno", "status": "active", "hops": 2}

    # Check violations (including closed_year with NULL or 'active' status)
    bad_studio_sql = [
        ("INSERT INTO public.studios (name, status, closed_year) VALUES ('Bad 1', NULL, 1994)", ()),
        ("INSERT INTO public.studios (name, status, closed_year) VALUES ('Bad 2', 'active', 1994)", ()),
        ("INSERT INTO public.studios (name, status) VALUES ('Bad 3', 'bankrupt')", ()),
        ("INSERT INTO public.dubbing_editions (media_source_id, lang_code, edition_type, studio_credited_as) VALUES (%s, 'fr', 'dub', '   ')", (m_retro,)),
        ("INSERT INTO public.studio_aliases (studio_id, alias_name, start_year, end_year) VALUES (%s, 'Bad Years', 2000, 1995)", (st_pumaisdue,)),
        ("INSERT INTO public.studio_relationships (studio_id, related_studio_id, relationship_type) VALUES (%s, %s, 'merged_into')", (st_cdc, st_cdc)),
        ("INSERT INTO public.studio_relationships (studio_id, related_studio_id, relationship_type) VALUES (%s, %s, 'spun_off')", (st_cdc, st_iyuno)),
    ]
    for sql, params in bad_studio_sql:
        with pytest.raises(psycopg.errors.CheckViolation):
            with transaction() as conn:
                conn.execute(sql, params)

    # Unique violations and ON DELETE CASCADE
    with pytest.raises(psycopg.errors.UniqueViolation):
        with transaction() as conn:
            conn.execute(
                "INSERT INTO public.studio_aliases (studio_id, alias_name, start_year) VALUES (%s, ' gruppo trenta ', 1980)",
                (st_pumaisdue,),
            )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with transaction() as conn:
            conn.execute(
                "INSERT INTO public.studio_relationships (studio_id, related_studio_id, relationship_type) VALUES (%s, %s, 'merged_into')",
                (st_cdc, st_cdc_sefit),
            )

    with transaction() as conn:
        conn.execute("DELETE FROM public.studios WHERE id = %s", (st_pumaisdue,))
        aliases_left = conn.execute(
            "SELECT COUNT(*) AS n FROM public.studio_aliases WHERE studio_id = %s",
            (st_pumaisdue,),
        ).fetchone()["n"]
    assert aliases_left == 0


def test_expanded_dubbing_crew_roles_and_edition_studios(pg_env: None) -> None:
    """Verify migration 012 expanded crew roles, edition studio bridge, and provenance."""
    with transaction() as conn:
        sp_id = conn.execute(
            "INSERT INTO public.speakers (display_name, sort_name) VALUES ('Marco Tecnico', 'Tecnico, Marco') RETURNING id"
        ).fetchone()["id"]
        media_id = conn.execute(
            "INSERT INTO public.media_sources (original_title, media_type) VALUES ('Multi-Studio Film', 'movie') RETURNING id"
        ).fetchone()["id"]
        st_dub = conn.execute(
            "INSERT INTO public.studios (name, country_code) VALUES ('Pumaisdue', 'IT') RETURNING id"
        ).fetchone()["id"]
        st_mix = conn.execute(
            "INSERT INTO public.studios (name, country_code) VALUES ('Technicolor Sound Services', 'IT') RETURNING id"
        ).fetchone()["id"]
        ed_id = conn.execute(
            """
            INSERT INTO public.dubbing_editions (media_source_id, studio_id, lang_code, edition_type)
            VALUES (%s, %s, 'it', 'dub') RETURNING id
            """,
            (media_id, st_dub),
        ).fetchone()["id"]

        new_roles = (
            "recording_engineer",
            "mixing_engineer",
            "translator",
            "lyricist",
            "chorus",
            "casting_director",
            "production_manager",
            "audio_editor",
            "creative_supervisor",
            "subtitler",
        )
        for role in new_roles:
            conn.execute(
                """
                INSERT INTO public.dubbing_credits (dubbing_edition_id, speaker_id, role_type)
                VALUES (%s, %s, %s)
                """,
                (ed_id, sp_id, role),
            )

        es_dub = conn.execute(
            """
            INSERT INTO public.dubbing_edition_studios (dubbing_edition_id, studio_id, role_type, credited_as)
            VALUES (%s, %s, 'dubbing_studio', 'Pumaisdue S.r.l.') RETURNING id
            """,
            (ed_id, st_dub),
        ).fetchone()["id"]
        es_mix = conn.execute(
            """
            INSERT INTO public.dubbing_edition_studios (dubbing_edition_id, studio_id, role_type, credited_as)
            VALUES (%s, %s, 'mixing_studio', 'Technicolor') RETURNING id
            """,
            (ed_id, st_mix),
        ).fetchone()["id"]

        # Verify provenance_records and audit_log accept 'dubbing_edition_studios'
        prov_id = conn.execute(
            """
            INSERT INTO public.provenance_records (
                entity_table, entity_id, source_type, source_license, source_url
            )
            VALUES ('dubbing_edition_studios', %s, 'wikidata', 'CC0-1.0', 'https://www.wikidata.org/wiki/Q1')
            RETURNING id
            """,
            (es_dub,),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO public.audit_log (entity_table, entity_id, action, changed_data, provenance_id)
            VALUES ('dubbing_edition_studios', %s, 'INSERT', '{"role_type": "mixing_studio"}'::jsonb, %s)
            """,
            (es_mix, prov_id),
        )

    # Check violations and unique constraint on dubbing_edition_studios
    with pytest.raises(psycopg.errors.CheckViolation):
        with transaction() as conn:
            conn.execute(
                "INSERT INTO public.dubbing_edition_studios (dubbing_edition_id, studio_id, role_type) VALUES (%s, %s, 'invalid_role')",
                (ed_id, st_dub),
            )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with transaction() as conn:
            conn.execute(
                "INSERT INTO public.dubbing_edition_studios (dubbing_edition_id, studio_id, role_type) VALUES (%s, %s, 'mixing_studio')",
                (ed_id, st_mix),
            )

    with transaction() as conn:
        conn.execute("DELETE FROM public.dubbing_editions WHERE id = %s", (ed_id,))
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM public.dubbing_edition_studios WHERE dubbing_edition_id = %s",
            (ed_id,),
        ).fetchone()["n"]
    assert remaining == 0
