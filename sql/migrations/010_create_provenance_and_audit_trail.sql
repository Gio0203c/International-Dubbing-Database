-- 010_create_provenance_and_audit_trail.sql
-- Provenance tracking, open-data license governance (CC0 / CC BY / CC BY-SA 4.0),
-- pseudonymous contributor identities, primary-source evidence fields, and an
-- append-only audit trail across core public domain entities.

CREATE TABLE IF NOT EXISTS public.contributors (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id TEXT GENERATED ALWAYS AS (
        CASE
            WHEN id < 10000000 THEN 'ct' || lpad(id::text, 7, '0')
            ELSE 'ct' || id::text
        END
    ) STORED NOT NULL,

    display_handle TEXT NOT NULL,
    accepted_cc_by_sa_4_0 BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_contributors_public_id UNIQUE (public_id),
    CONSTRAINT chk_contributors_public_id_format CHECK (public_id ~ '^ct[0-9]{7,}$'),
    CONSTRAINT chk_contributors_handle_format CHECK (
        btrim(display_handle) <> '' AND display_handle ~ '^[A-Za-z0-9._-]{2,50}$'
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_contributors_handle_lower
    ON public.contributors (lower(display_handle));

COMMENT ON TABLE public.contributors IS
    'Pseudonymous public contributor handles for clean-room primary verification and CC BY-SA 4.0 attribution (no private PII).';


CREATE TABLE IF NOT EXISTS public.provenance_records (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_table TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    field_name TEXT NULL,

    source_type TEXT NOT NULL,
    source_lang TEXT NULL,
    source_license TEXT NOT NULL,
    source_url TEXT NULL,
    source_revision_id TEXT NULL,
    attribution_text TEXT NULL,

    distribution_edition TEXT NULL,
    credit_timecode TEXT NULL,
    episode_or_cut TEXT NULL,

    contributor_id BIGINT NULL,
    etl_run_id BIGINT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_provenance_entity_table_allowed CHECK (
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
            'characters',
            'dubbing_credits'
        )
    ),
    CONSTRAINT chk_provenance_entity_id_positive CHECK (entity_id > 0),
    CONSTRAINT chk_provenance_field_name_format CHECK (
        field_name IS NULL OR field_name ~ '^[a-z][a-z0-9_]{0,62}$'
    ),
    CONSTRAINT chk_provenance_source_type_allowed CHECK (
        source_type IN (
            'wikidata',
            'itwiki',
            'wikipedia',
            'wikimedia_commons',
            'tvmaze',
            'musicbrainz_core',
            'end_credits',
            'official_studio_post',
            'studio_press_release',
            'community_primary_verified'
        )
    ),
    CONSTRAINT chk_provenance_source_lang_iso639_1 CHECK (
        source_lang IS NULL OR source_lang ~ '^[a-z]{2}$'
    ),
    CONSTRAINT chk_provenance_source_license_open CHECK (
        source_license IN (
            'CC0-1.0',
            'PDM-1.0',
            'CC-BY-1.0',
            'CC-BY-2.0',
            'CC-BY-3.0',
            'CC-BY-4.0',
            'CC-BY-SA-1.0',
            'CC-BY-SA-2.0',
            'CC-BY-SA-3.0',
            'CC-BY-SA-4.0'
        )
        AND source_license !~* '(^|-)(NC|ND)(-|$)'
    ),
    CONSTRAINT chk_provenance_source_url_format CHECK (
        source_url IS NULL OR source_url ~ '^https?://\S+$'
    ),
    CONSTRAINT chk_provenance_revision_not_blank CHECK (
        source_revision_id IS NULL OR btrim(source_revision_id) <> ''
    ),
    CONSTRAINT chk_provenance_attribution_not_blank CHECK (
        attribution_text IS NULL OR btrim(attribution_text) <> ''
    ),
    CONSTRAINT chk_provenance_distribution_edition_safe CHECK (
        distribution_edition IS NULL
        OR (
            btrim(distribution_edition) <> ''
            AND distribution_edition !~* '(theatrical_cam|camcord|camrip|cinema_cam)'
        )
    ),
    CONSTRAINT chk_provenance_timecode_format CHECK (
        credit_timecode IS NULL OR credit_timecode ~ '^[0-9]{1,2}:[0-5][0-9](:[0-5][0-9])?$'
    ),
    CONSTRAINT chk_provenance_episode_or_cut_not_blank CHECK (
        episode_or_cut IS NULL OR btrim(episode_or_cut) <> ''
    ),
    CONSTRAINT fk_provenance_contributor
        FOREIGN KEY (contributor_id) REFERENCES public.contributors(id) ON DELETE SET NULL,
    CONSTRAINT fk_provenance_etl_run
        FOREIGN KEY (etl_run_id) REFERENCES public.etl_runs(id) ON DELETE SET NULL,
    CONSTRAINT chk_provenance_web_url_required CHECK (
        source_type NOT IN (
            'wikidata',
            'itwiki',
            'wikipedia',
            'wikimedia_commons',
            'tvmaze',
            'musicbrainz_core',
            'official_studio_post',
            'studio_press_release'
        )
        OR source_url IS NOT NULL
    ),
    CONSTRAINT chk_provenance_wiki_revision_required CHECK (
        source_type NOT IN ('itwiki', 'wikipedia') OR source_revision_id IS NOT NULL
    ),
    CONSTRAINT chk_provenance_attribution_required CHECK (
        source_license IN ('CC0-1.0', 'PDM-1.0') OR attribution_text IS NOT NULL
    ),
    CONSTRAINT chk_provenance_primary_fields_required CHECK (
        source_type NOT IN ('end_credits', 'community_primary_verified')
        OR (distribution_edition IS NOT NULL AND contributor_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_provenance_records_entity
    ON public.provenance_records (entity_table, entity_id);

CREATE INDEX IF NOT EXISTS idx_provenance_records_source_license
    ON public.provenance_records (source_type, source_license);

CREATE INDEX IF NOT EXISTS idx_provenance_records_etl_run
    ON public.provenance_records (etl_run_id)
    WHERE etl_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_provenance_records_contributor
    ON public.provenance_records (contributor_id)
    WHERE contributor_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_provenance_records_natural
    ON public.provenance_records (
        entity_table,
        entity_id,
        COALESCE(field_name, ''),
        source_type,
        COALESCE(source_url, ''),
        COALESCE(source_revision_id, ''),
        COALESCE(distribution_edition, ''),
        COALESCE(credit_timecode, '')
    );

COMMENT ON TABLE public.provenance_records IS
    'Granular per-record and per-field provenance, SPDX open license, revision IDs, attribution text, and primary-source verification details.';


CREATE TABLE IF NOT EXISTS public.audit_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_table TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    changed_data JSONB NOT NULL,
    provenance_id BIGINT NULL,
    contributor_id BIGINT NULL,
    etl_run_id BIGINT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_audit_log_entity_table_allowed CHECK (
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
            'characters',
            'dubbing_credits',
            'contributors',
            'provenance_records'
        )
    ),
    CONSTRAINT chk_audit_log_entity_id_positive CHECK (entity_id > 0),
    CONSTRAINT chk_audit_log_action_allowed CHECK (
        action IN ('INSERT', 'UPDATE', 'DELETE', 'VERIFY')
    ),
    CONSTRAINT chk_audit_log_changed_data_object CHECK (
        jsonb_typeof(changed_data) = 'object'
    ),
    CONSTRAINT fk_audit_log_provenance
        FOREIGN KEY (provenance_id) REFERENCES public.provenance_records(id) ON DELETE SET NULL,
    CONSTRAINT fk_audit_log_contributor
        FOREIGN KEY (contributor_id) REFERENCES public.contributors(id) ON DELETE SET NULL,
    CONSTRAINT fk_audit_log_etl_run
        FOREIGN KEY (etl_run_id) REFERENCES public.etl_runs(id) ON DELETE SET NULL
);

CREATE OR REPLACE FUNCTION public.prevent_audit_log_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'public.audit_log is append-only; % is not permitted', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_log_append_only ON public.audit_log;
CREATE TRIGGER trg_audit_log_append_only
    BEFORE UPDATE OR DELETE ON public.audit_log
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_audit_log_mutation();

CREATE INDEX IF NOT EXISTS idx_audit_log_entity_time
    ON public.audit_log (entity_table, entity_id, changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_log_etl_run
    ON public.audit_log (etl_run_id)
    WHERE etl_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_log_contributor
    ON public.audit_log (contributor_id)
    WHERE contributor_id IS NOT NULL;

COMMENT ON TABLE public.audit_log IS
    'Append-only modification ledger recording structured diffs linked to provenance records, contributors, and ETL runs.';
