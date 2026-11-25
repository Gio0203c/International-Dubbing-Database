-- 008_create_core_dubbing_model.sql
-- Core dubbing domain tables: media_sources, studios, dubbing_editions,
-- characters, and dubbing_credits.

CREATE TABLE IF NOT EXISTS public.media_sources (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id TEXT GENERATED ALWAYS AS (
        CASE
            WHEN id < 10000000 THEN 'md' || lpad(id::text, 7, '0')
            ELSE 'md' || id::text
        END
    ) STORED NOT NULL,

    original_title TEXT NOT NULL,
    media_type TEXT NOT NULL,
    is_animated BOOLEAN NULL,
    release_year SMALLINT NULL,
    end_year SMALLINT NULL,
    original_lang TEXT NULL,
    country_code TEXT NULL,
    parent_media_id BIGINT NULL,

    wikidata_qid TEXT NULL,
    imdb_tconst TEXT NULL,
    tmdb_media_type TEXT NULL,
    tmdb_id INTEGER NULL,
    anilist_media_id INTEGER NULL,

    primary_source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_media_sources_public_id UNIQUE (public_id),
    CONSTRAINT chk_media_sources_public_id_format CHECK (public_id ~ '^md[0-9]{7,}$'),
    CONSTRAINT chk_media_sources_title_not_blank CHECK (btrim(original_title) <> ''),
    CONSTRAINT chk_media_sources_type_allowed CHECK (
        media_type IN (
            'movie',
            'tv_series',
            'miniseries',
            'ova',
            'short',
            'documentary',
            'video_game',
            'audiobook',
            'web_series',
            'special',
            'other'
        )
    ),
    CONSTRAINT chk_media_sources_release_year_range CHECK (
        release_year IS NULL OR release_year BETWEEN 1880 AND 2100
    ),
    CONSTRAINT chk_media_sources_end_year_range CHECK (
        end_year IS NULL OR end_year BETWEEN 1880 AND 2100
    ),
    CONSTRAINT chk_media_sources_end_after_release CHECK (
        end_year IS NULL OR release_year IS NULL OR end_year >= release_year
    ),
    CONSTRAINT chk_media_sources_original_lang_iso639_1 CHECK (
        original_lang IS NULL OR original_lang ~ '^[a-z]{2}$'
    ),
    CONSTRAINT chk_media_sources_country_iso3166_2 CHECK (
        country_code IS NULL OR country_code ~ '^[A-Z]{2}$'
    ),
    CONSTRAINT fk_media_sources_parent
        FOREIGN KEY (parent_media_id) REFERENCES public.media_sources(id) ON DELETE SET NULL,
    CONSTRAINT chk_media_sources_not_self_parent CHECK (
        parent_media_id IS NULL OR parent_media_id <> id
    ),
    CONSTRAINT uq_media_sources_wikidata_qid UNIQUE (wikidata_qid),
    CONSTRAINT chk_media_sources_wikidata_qid_format CHECK (
        wikidata_qid IS NULL OR wikidata_qid ~ '^Q[1-9][0-9]*$'
    ),
    CONSTRAINT uq_media_sources_imdb_tconst UNIQUE (imdb_tconst),
    CONSTRAINT chk_media_sources_imdb_tconst_format CHECK (
        imdb_tconst IS NULL OR imdb_tconst ~ '^tt[0-9]+$'
    ),
    CONSTRAINT chk_media_sources_tmdb_pair CHECK (
        (tmdb_media_type IS NULL AND tmdb_id IS NULL)
        OR (
            tmdb_media_type IS NOT NULL
            AND tmdb_media_type IN ('movie', 'tv')
            AND tmdb_id IS NOT NULL
            AND tmdb_id > 0
        )
    ),
    CONSTRAINT uq_media_sources_tmdb_pair UNIQUE (tmdb_media_type, tmdb_id),
    CONSTRAINT uq_media_sources_anilist_media_id UNIQUE (anilist_media_id),
    CONSTRAINT chk_media_sources_anilist_positive CHECK (
        anilist_media_id IS NULL OR anilist_media_id > 0
    ),
    CONSTRAINT chk_media_sources_primary_source_not_blank CHECK (btrim(primary_source) <> '')
);

CREATE INDEX IF NOT EXISTS idx_media_sources_title_lower
    ON public.media_sources (lower(original_title));

CREATE INDEX IF NOT EXISTS idx_media_sources_type_year
    ON public.media_sources (media_type, release_year);

CREATE INDEX IF NOT EXISTS idx_media_sources_parent
    ON public.media_sources (parent_media_id)
    WHERE parent_media_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_media_sources_last_seen_at
    ON public.media_sources (last_seen_at);

COMMENT ON TABLE public.media_sources IS
    'Original audiovisual, interactive, and spoken-word media works across formats and countries.';


CREATE TABLE IF NOT EXISTS public.studios (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id TEXT GENERATED ALWAYS AS (
        CASE
            WHEN id < 10000000 THEN 'st' || lpad(id::text, 7, '0')
            ELSE 'st' || id::text
        END
    ) STORED NOT NULL,

    name TEXT NOT NULL,
    country_code TEXT NULL,
    city TEXT NULL,
    founded_year SMALLINT NULL,
    closed_year SMALLINT NULL,
    wikidata_qid TEXT NULL,
    website_url TEXT NULL,

    primary_source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_studios_public_id UNIQUE (public_id),
    CONSTRAINT chk_studios_public_id_format CHECK (public_id ~ '^st[0-9]{7,}$'),
    CONSTRAINT chk_studios_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT chk_studios_country_iso3166_2 CHECK (
        country_code IS NULL OR country_code ~ '^[A-Z]{2}$'
    ),
    CONSTRAINT chk_studios_city_not_blank CHECK (
        city IS NULL OR btrim(city) <> ''
    ),
    CONSTRAINT chk_studios_founded_year_range CHECK (
        founded_year IS NULL OR founded_year BETWEEN 1880 AND 2100
    ),
    CONSTRAINT chk_studios_closed_year_range CHECK (
        closed_year IS NULL OR closed_year BETWEEN 1880 AND 2100
    ),
    CONSTRAINT chk_studios_closed_after_founded CHECK (
        closed_year IS NULL OR founded_year IS NULL OR closed_year >= founded_year
    ),
    CONSTRAINT uq_studios_wikidata_qid UNIQUE (wikidata_qid),
    CONSTRAINT chk_studios_wikidata_qid_format CHECK (
        wikidata_qid IS NULL OR wikidata_qid ~ '^Q[1-9][0-9]*$'
    ),
    CONSTRAINT chk_studios_website_url_format CHECK (
        website_url IS NULL OR website_url ~ '^https?://\S+$'
    ),
    CONSTRAINT chk_studios_primary_source_not_blank CHECK (btrim(primary_source) <> '')
);

CREATE INDEX IF NOT EXISTS idx_studios_name_lower
    ON public.studios (lower(name));

CREATE INDEX IF NOT EXISTS idx_studios_country_code
    ON public.studios (country_code)
    WHERE country_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_studios_last_seen_at
    ON public.studios (last_seen_at);

COMMENT ON TABLE public.studios IS
    'Dubbing studios, localization cooperatives, and sound recording houses.';


CREATE TABLE IF NOT EXISTS public.dubbing_editions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id TEXT GENERATED ALWAYS AS (
        CASE
            WHEN id < 10000000 THEN 'ed' || lpad(id::text, 7, '0')
            ELSE 'ed' || id::text
        END
    ) STORED NOT NULL,

    media_source_id BIGINT NOT NULL,
    studio_id BIGINT NULL,
    lang_code TEXT NOT NULL,
    region_code TEXT NULL,
    edition_type TEXT NOT NULL DEFAULT 'dub',
    edition_label TEXT NULL,
    release_year SMALLINT NULL,
    localized_title TEXT NULL,

    primary_source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_dubbing_editions_public_id UNIQUE (public_id),
    CONSTRAINT chk_dubbing_editions_public_id_format CHECK (public_id ~ '^ed[0-9]{7,}$'),
    CONSTRAINT fk_dubbing_editions_media_source
        FOREIGN KEY (media_source_id) REFERENCES public.media_sources(id) ON DELETE CASCADE,
    CONSTRAINT fk_dubbing_editions_studio
        FOREIGN KEY (studio_id) REFERENCES public.studios(id) ON DELETE SET NULL,
    CONSTRAINT chk_dubbing_editions_lang_iso639_1 CHECK (lang_code ~ '^[a-z]{2}$'),
    CONSTRAINT chk_dubbing_editions_region_iso3166_2 CHECK (
        region_code IS NULL OR region_code ~ '^[A-Z]{2}$'
    ),
    CONSTRAINT chk_dubbing_editions_type_allowed CHECK (
        edition_type IN ('dub', 'redub', 'original', 'voice_over')
    ),
    CONSTRAINT chk_dubbing_editions_label_not_blank CHECK (
        edition_label IS NULL OR btrim(edition_label) <> ''
    ),
    CONSTRAINT chk_dubbing_editions_release_year_range CHECK (
        release_year IS NULL OR release_year BETWEEN 1880 AND 2100
    ),
    CONSTRAINT chk_dubbing_editions_localized_title_not_blank CHECK (
        localized_title IS NULL OR btrim(localized_title) <> ''
    ),
    CONSTRAINT chk_dubbing_editions_primary_source_not_blank CHECK (btrim(primary_source) <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dubbing_editions_natural
    ON public.dubbing_editions (
        media_source_id,
        lang_code,
        COALESCE(region_code, ''),
        edition_type,
        COALESCE(release_year, 0),
        COALESCE(lower(btrim(edition_label)), '')
    );

CREATE INDEX IF NOT EXISTS idx_dubbing_editions_media_lang
    ON public.dubbing_editions (media_source_id, lang_code);

CREATE INDEX IF NOT EXISTS idx_dubbing_editions_studio
    ON public.dubbing_editions (studio_id)
    WHERE studio_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dubbing_editions_last_seen_at
    ON public.dubbing_editions (last_seen_at);

COMMENT ON TABLE public.dubbing_editions IS
    'Localized audio versions (original voice cast, dubs, redubs, and regional variants) for a media work.';


CREATE TABLE IF NOT EXISTS public.characters (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id TEXT GENERATED ALWAYS AS (
        CASE
            WHEN id < 10000000 THEN 'ch' || lpad(id::text, 7, '0')
            ELSE 'ch' || id::text
        END
    ) STORED NOT NULL,

    name TEXT NOT NULL,
    media_source_id BIGINT NULL,
    gender TEXT NULL,
    wikidata_qid TEXT NULL,
    anilist_character_id INTEGER NULL,

    primary_source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_characters_public_id UNIQUE (public_id),
    CONSTRAINT chk_characters_public_id_format CHECK (public_id ~ '^ch[0-9]{7,}$'),
    CONSTRAINT chk_characters_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT fk_characters_media_source
        FOREIGN KEY (media_source_id) REFERENCES public.media_sources(id) ON DELETE SET NULL,
    CONSTRAINT chk_characters_gender_allowed CHECK (
        gender IS NULL OR gender IN ('female', 'male', 'non_binary', 'other')
    ),
    CONSTRAINT uq_characters_wikidata_qid UNIQUE (wikidata_qid),
    CONSTRAINT chk_characters_wikidata_qid_format CHECK (
        wikidata_qid IS NULL OR wikidata_qid ~ '^Q[1-9][0-9]*$'
    ),
    CONSTRAINT uq_characters_anilist_character_id UNIQUE (anilist_character_id),
    CONSTRAINT chk_characters_anilist_positive CHECK (
        anilist_character_id IS NULL OR anilist_character_id > 0
    ),
    CONSTRAINT chk_characters_primary_source_not_blank CHECK (btrim(primary_source) <> '')
);

CREATE INDEX IF NOT EXISTS idx_characters_name_lower
    ON public.characters (lower(name));

CREATE INDEX IF NOT EXISTS idx_characters_media_source
    ON public.characters (media_source_id)
    WHERE media_source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_characters_last_seen_at
    ON public.characters (last_seen_at);

COMMENT ON TABLE public.characters IS
    'Fictional or real-world characters voiced across dubbing editions, optionally anchored to an origin work.';


CREATE TABLE IF NOT EXISTS public.dubbing_credits (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dubbing_edition_id BIGINT NOT NULL,
    speaker_id BIGINT NOT NULL,
    character_id BIGINT NULL,
    role_type TEXT NOT NULL DEFAULT 'voice',
    credited_as TEXT NULL,
    original_actor_name TEXT NULL,
    original_actor_qid TEXT NULL,
    episode_range TEXT NULL,
    is_main_cast BOOLEAN NULL,
    notes TEXT NULL,

    source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_dubbing_credits_edition
        FOREIGN KEY (dubbing_edition_id) REFERENCES public.dubbing_editions(id) ON DELETE CASCADE,
    CONSTRAINT fk_dubbing_credits_speaker
        FOREIGN KEY (speaker_id) REFERENCES public.speakers(id) ON DELETE CASCADE,
    CONSTRAINT fk_dubbing_credits_character
        FOREIGN KEY (character_id) REFERENCES public.characters(id) ON DELETE SET NULL,
    CONSTRAINT chk_dubbing_credits_role_type_allowed CHECK (
        role_type IN (
            'voice',
            'singing',
            'theme_song',
            'narrator',
            'dubbing_director',
            'dialogue_adapter',
            'assistant_director',
            'sound_engineer',
            'musical_director',
            'additional_voices',
            'other'
        )
    ),
    CONSTRAINT chk_dubbing_credits_credited_as_not_blank CHECK (
        credited_as IS NULL OR btrim(credited_as) <> ''
    ),
    CONSTRAINT chk_dubbing_credits_original_actor_name_not_blank CHECK (
        original_actor_name IS NULL OR btrim(original_actor_name) <> ''
    ),
    CONSTRAINT chk_dubbing_credits_original_actor_qid_format CHECK (
        original_actor_qid IS NULL OR original_actor_qid ~ '^Q[1-9][0-9]*$'
    ),
    CONSTRAINT chk_dubbing_credits_episode_range_not_blank CHECK (
        episode_range IS NULL OR btrim(episode_range) <> ''
    ),
    CONSTRAINT chk_dubbing_credits_notes_not_blank CHECK (
        notes IS NULL OR btrim(notes) <> ''
    ),
    CONSTRAINT chk_dubbing_credits_source_not_blank CHECK (btrim(source) <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dubbing_credits_natural
    ON public.dubbing_credits (
        dubbing_edition_id,
        speaker_id,
        role_type,
        COALESCE(character_id, -1),
        COALESCE(lower(btrim(original_actor_name)), ''),
        COALESCE(lower(btrim(episode_range)), '')
    );

CREATE INDEX IF NOT EXISTS idx_dubbing_credits_speaker_role
    ON public.dubbing_credits (speaker_id, role_type);

CREATE INDEX IF NOT EXISTS idx_dubbing_credits_role_speaker
    ON public.dubbing_credits (role_type, speaker_id);

CREATE INDEX IF NOT EXISTS idx_dubbing_credits_character
    ON public.dubbing_credits (character_id)
    WHERE character_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dubbing_credits_original_actor_qid
    ON public.dubbing_credits (original_actor_qid)
    WHERE original_actor_qid IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dubbing_credits_last_seen_at
    ON public.dubbing_credits (last_seen_at);

COMMENT ON TABLE public.dubbing_credits IS
    'Cast and crew credits linking speakers and characters within a specific dubbing edition.';
