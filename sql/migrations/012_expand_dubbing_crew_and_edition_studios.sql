-- 012_expand_dubbing_crew_and_edition_studios.sql
-- Expand controlled dubbing crew roles on public.dubbing_credits (splitting
-- recording vs mixing engineers, translator vs dialogue adapter, lyricist,
-- chorus, casting, production, audio editing, creative supervision, subtitling)
-- and add public.dubbing_edition_studios for multi-studio editions (dubbing
-- house, recording facility, mixing studio, production supervisor).

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.dubbing_credits'::regclass
          AND conname = 'chk_dubbing_credits_role_type_allowed'
    ) THEN
        ALTER TABLE public.dubbing_credits
            DROP CONSTRAINT chk_dubbing_credits_role_type_allowed;
    END IF;

    ALTER TABLE public.dubbing_credits
        ADD CONSTRAINT chk_dubbing_credits_role_type_allowed CHECK (
            role_type IN (
                'voice',
                'singing',
                'theme_song',
                'chorus',
                'narrator',
                'dubbing_director',
                'assistant_director',
                'dialogue_adapter',
                'translator',
                'lyricist',
                'sound_engineer',
                'recording_engineer',
                'mixing_engineer',
                'audio_editor',
                'musical_director',
                'casting_director',
                'production_manager',
                'creative_supervisor',
                'subtitler',
                'additional_voices',
                'other'
            )
        );
END;
$$;


CREATE TABLE IF NOT EXISTS public.dubbing_edition_studios (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dubbing_edition_id BIGINT NOT NULL,
    studio_id BIGINT NOT NULL,
    role_type TEXT NOT NULL DEFAULT 'dubbing_studio',
    credited_as TEXT NULL,
    notes TEXT NULL,

    source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_dubbing_edition_studios_edition
        FOREIGN KEY (dubbing_edition_id) REFERENCES public.dubbing_editions(id) ON DELETE CASCADE,
    CONSTRAINT fk_dubbing_edition_studios_studio
        FOREIGN KEY (studio_id) REFERENCES public.studios(id) ON DELETE CASCADE,
    CONSTRAINT chk_dubbing_edition_studios_role_allowed CHECK (
        role_type IN (
            'dubbing_studio',
            'recording_studio',
            'mixing_studio',
            'production_supervisor',
            'other'
        )
    ),
    CONSTRAINT chk_dubbing_edition_studios_credited_as_not_blank CHECK (
        credited_as IS NULL OR btrim(credited_as) <> ''
    ),
    CONSTRAINT chk_dubbing_edition_studios_notes_not_blank CHECK (
        notes IS NULL OR btrim(notes) <> ''
    ),
    CONSTRAINT chk_dubbing_edition_studios_source_not_blank CHECK (btrim(source) <> ''),
    CONSTRAINT uq_dubbing_edition_studios_role UNIQUE (dubbing_edition_id, studio_id, role_type)
);

CREATE INDEX IF NOT EXISTS idx_dubbing_edition_studios_studio_role
    ON public.dubbing_edition_studios (studio_id, role_type);

CREATE INDEX IF NOT EXISTS idx_dubbing_edition_studios_last_seen_at
    ON public.dubbing_edition_studios (last_seen_at);

COMMENT ON TABLE public.dubbing_edition_studios IS
    'N:M studio roles per dubbing edition (dubbing studio, recording facility, mixing studio, production supervisor).';


DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.provenance_records'::regclass
          AND conname = 'chk_provenance_entity_table_allowed'
    ) THEN
        ALTER TABLE public.provenance_records
            DROP CONSTRAINT chk_provenance_entity_table_allowed;
    END IF;

    ALTER TABLE public.provenance_records
        ADD CONSTRAINT chk_provenance_entity_table_allowed CHECK (
            entity_table IN (
                'speakers',
                'speaker_relationships',
                'speaker_aliases',
                'speaker_languages',
                'speaker_external_ids',
                'media_sources',
                'studios',
                'studio_aliases',
                'studio_relationships',
                'dubbing_editions',
                'dubbing_edition_studios',
                'characters',
                'dubbing_credits'
            )
        );

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.audit_log'::regclass
          AND conname = 'chk_audit_log_entity_table_allowed'
    ) THEN
        ALTER TABLE public.audit_log
            DROP CONSTRAINT chk_audit_log_entity_table_allowed;
    END IF;

    ALTER TABLE public.audit_log
        ADD CONSTRAINT chk_audit_log_entity_table_allowed CHECK (
            entity_table IN (
                'speakers',
                'speaker_relationships',
                'speaker_aliases',
                'speaker_languages',
                'speaker_external_ids',
                'media_sources',
                'studios',
                'studio_aliases',
                'studio_relationships',
                'dubbing_editions',
                'dubbing_edition_studios',
                'characters',
                'dubbing_credits',
                'contributors',
                'provenance_records'
            )
        );
END;
$$;
