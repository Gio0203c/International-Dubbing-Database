-- 003_create_public_speakers.sql
-- Core global-first voice actor table with an internal surrogate key and an
-- IMDb-style prefixed public identifier (sp0000001, expanding beyond 7 digits).

CREATE TABLE IF NOT EXISTS public.speakers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id TEXT GENERATED ALWAYS AS (
        CASE
            WHEN id < 10000000 THEN 'sp' || lpad(id::text, 7, '0')
            ELSE 'sp' || id::text
        END
    ) STORED NOT NULL,

    display_name TEXT NOT NULL,
    sort_name TEXT NOT NULL,
    legal_name TEXT NULL,
    birth_year SMALLINT NULL,
    death_year SMALLINT NULL,
    gender TEXT NULL,
    native_lang TEXT NULL,
    country_code TEXT NULL,

    wikidata_qid TEXT NULL,
    imdb_nconst TEXT NULL,
    tmdb_person_id INTEGER NULL,

    primary_source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_speakers_public_id UNIQUE (public_id),
    CONSTRAINT chk_speakers_public_id_format CHECK (public_id ~ '^sp[0-9]{7,}$'),

    CONSTRAINT chk_speakers_display_name_not_blank CHECK (btrim(display_name) <> ''),
    CONSTRAINT chk_speakers_sort_name_not_blank CHECK (btrim(sort_name) <> ''),
    CONSTRAINT chk_speakers_legal_name_not_blank CHECK (
        legal_name IS NULL OR btrim(legal_name) <> ''
    ),

    CONSTRAINT chk_speakers_birth_year_range CHECK (
        birth_year IS NULL OR birth_year BETWEEN 1850 AND 2100
    ),
    CONSTRAINT chk_speakers_death_year_range CHECK (
        death_year IS NULL OR death_year BETWEEN 1850 AND 2100
    ),
    CONSTRAINT chk_speakers_death_after_birth CHECK (
        death_year IS NULL OR birth_year IS NULL OR death_year >= birth_year
    ),

    CONSTRAINT chk_speakers_gender_allowed CHECK (
        gender IS NULL OR gender IN ('female', 'male', 'non_binary', 'other')
    ),
    CONSTRAINT chk_speakers_native_lang_iso639_1 CHECK (
        native_lang IS NULL OR native_lang ~ '^[a-z]{2}$'
    ),
    CONSTRAINT chk_speakers_country_code_iso3166_2 CHECK (
        country_code IS NULL OR country_code ~ '^[A-Z]{2}$'
    ),

    CONSTRAINT uq_speakers_wikidata_qid UNIQUE (wikidata_qid),
    CONSTRAINT chk_speakers_wikidata_qid_format CHECK (
        wikidata_qid IS NULL OR wikidata_qid ~ '^Q[1-9][0-9]*$'
    ),
    CONSTRAINT uq_speakers_imdb_nconst UNIQUE (imdb_nconst),
    CONSTRAINT chk_speakers_imdb_nconst_format CHECK (
        imdb_nconst IS NULL OR imdb_nconst ~ '^nm[0-9]+$'
    ),
    CONSTRAINT uq_speakers_tmdb_person_id UNIQUE (tmdb_person_id),
    CONSTRAINT chk_speakers_tmdb_person_id_positive CHECK (
        tmdb_person_id IS NULL OR tmdb_person_id > 0
    ),

    CONSTRAINT chk_speakers_primary_source_not_blank CHECK (btrim(primary_source) <> '')
);

CREATE INDEX IF NOT EXISTS idx_speakers_sort_name
    ON public.speakers (sort_name);

CREATE INDEX IF NOT EXISTS idx_speakers_country_code
    ON public.speakers (country_code)
    WHERE country_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_speakers_last_seen_at
    ON public.speakers (last_seen_at);

COMMENT ON TABLE public.speakers IS
    'Normalized voice actor records across languages and countries.';
