-- 007_create_speaker_satellites.sql
-- Normalized 1:N satellite tables for public.speakers (aliases, languages,
-- and long-tail external identifiers), keeping public.speakers compact and 1NF-clean.

CREATE TABLE IF NOT EXISTS public.speaker_aliases (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    speaker_id BIGINT NOT NULL,
    alias_name TEXT NOT NULL,
    sort_name TEXT NULL,
    alias_type TEXT NOT NULL DEFAULT 'other',
    locale TEXT NULL,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_speaker_aliases_speaker
        FOREIGN KEY (speaker_id) REFERENCES public.speakers(id) ON DELETE CASCADE,
    CONSTRAINT chk_speaker_aliases_name_not_blank
        CHECK (btrim(alias_name) <> ''),
    CONSTRAINT chk_speaker_aliases_sort_name_not_blank
        CHECK (sort_name IS NULL OR btrim(sort_name) <> ''),
    CONSTRAINT chk_speaker_aliases_type_allowed
        CHECK (
            alias_type IN (
                'stage_name',
                'legal_name',
                'maiden_name',
                'credit_variant',
                'transliteration',
                'other'
            )
        ),
    CONSTRAINT chk_speaker_aliases_locale_format
        CHECK (locale IS NULL OR locale ~ '^[a-z]{2}(-[A-Z][a-z]{3})?(-[A-Z]{2})?$'),
    CONSTRAINT chk_speaker_aliases_source_not_blank
        CHECK (btrim(source) <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_speaker_aliases_speaker_name_locale
    ON public.speaker_aliases (speaker_id, lower(btrim(alias_name)), COALESCE(locale, ''));

CREATE UNIQUE INDEX IF NOT EXISTS uq_speaker_aliases_primary_per_locale
    ON public.speaker_aliases (speaker_id, COALESCE(locale, ''))
    WHERE is_primary IS TRUE;

CREATE INDEX IF NOT EXISTS idx_speaker_aliases_name_lower
    ON public.speaker_aliases (lower(alias_name));

CREATE INDEX IF NOT EXISTS idx_speaker_aliases_last_seen_at
    ON public.speaker_aliases (last_seen_at);

COMMENT ON TABLE public.speaker_aliases IS
    'Alternative names, stage names, credit variants, and transliterations for public.speakers.';


CREATE TABLE IF NOT EXISTS public.speaker_languages (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    speaker_id BIGINT NOT NULL,
    lang_code TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'spoken',
    source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_speaker_languages_speaker
        FOREIGN KEY (speaker_id) REFERENCES public.speakers(id) ON DELETE CASCADE,
    CONSTRAINT chk_speaker_languages_lang_iso639_1
        CHECK (lang_code ~ '^[a-z]{2}$'),
    CONSTRAINT chk_speaker_languages_role_allowed
        CHECK (role IN ('native', 'working', 'spoken')),
    CONSTRAINT uq_speaker_languages_speaker_lang_role
        UNIQUE (speaker_id, lang_code, role),
    CONSTRAINT chk_speaker_languages_source_not_blank
        CHECK (btrim(source) <> '')
);

CREATE INDEX IF NOT EXISTS idx_speaker_languages_lang_role
    ON public.speaker_languages (lang_code, role);

CREATE INDEX IF NOT EXISTS idx_speaker_languages_last_seen_at
    ON public.speaker_languages (last_seen_at);

COMMENT ON TABLE public.speaker_languages IS
    'Multi-valued ISO 639-1 native, working, and spoken languages for public.speakers.';


CREATE TABLE IF NOT EXISTS public.speaker_external_ids (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    speaker_id BIGINT NOT NULL,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_speaker_external_ids_speaker
        FOREIGN KEY (speaker_id) REFERENCES public.speakers(id) ON DELETE CASCADE,
    CONSTRAINT chk_speaker_external_ids_provider_format
        CHECK (provider ~ '^[a-z][a-z0-9_]{1,49}$'),
    CONSTRAINT chk_speaker_external_ids_not_core_bridge
        CHECK (provider NOT IN ('wikidata', 'imdb', 'tmdb')),
    CONSTRAINT chk_speaker_external_ids_value_not_blank
        CHECK (btrim(external_id) <> ''),
    CONSTRAINT uq_speaker_external_ids_provider_value
        UNIQUE (provider, external_id),
    CONSTRAINT chk_speaker_external_ids_source_not_blank
        CHECK (btrim(source) <> '')
);

CREATE INDEX IF NOT EXISTS idx_speaker_external_ids_speaker_provider
    ON public.speaker_external_ids (speaker_id, provider);

CREATE INDEX IF NOT EXISTS idx_speaker_external_ids_last_seen_at
    ON public.speaker_external_ids (last_seen_at);

COMMENT ON TABLE public.speaker_external_ids IS
    'Long-tail external catalog identifiers for public.speakers (excluding core wikidata/imdb/tmdb columns).';
