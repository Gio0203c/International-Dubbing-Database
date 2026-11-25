-- 009_add_studio_history_and_aliases.sql
-- Track studio lifecycle status, former names / rebrands (studio_aliases),
-- corporate mergers and lineage (studio_relationships), and on-screen
-- historical studio credits on localized editions.

ALTER TABLE public.studios
    ADD COLUMN IF NOT EXISTS status TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.studios'::regclass
          AND conname = 'chk_studios_status_allowed'
    ) THEN
        ALTER TABLE public.studios
            ADD CONSTRAINT chk_studios_status_allowed CHECK (
                status IS NULL OR status IN ('active', 'closed', 'merged')
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.studios'::regclass
          AND conname = 'chk_studios_closed_year_implies_inactive'
    ) THEN
        ALTER TABLE public.studios
            ADD CONSTRAINT chk_studios_closed_year_implies_inactive CHECK (
                closed_year IS NULL
                OR (status IS NOT NULL AND status IN ('closed', 'merged'))
            );
    END IF;
END;
$$;

ALTER TABLE public.dubbing_editions
    ADD COLUMN IF NOT EXISTS studio_credited_as TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.dubbing_editions'::regclass
          AND conname = 'chk_dubbing_editions_studio_credited_as_not_blank'
    ) THEN
        ALTER TABLE public.dubbing_editions
            ADD CONSTRAINT chk_dubbing_editions_studio_credited_as_not_blank CHECK (
                studio_credited_as IS NULL OR btrim(studio_credited_as) <> ''
            );
    END IF;
END;
$$;


CREATE TABLE IF NOT EXISTS public.studio_aliases (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    studio_id BIGINT NOT NULL,
    alias_name TEXT NOT NULL,
    alias_type TEXT NOT NULL DEFAULT 'former_name',
    start_year SMALLINT NULL,
    end_year SMALLINT NULL,
    source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_studio_aliases_studio
        FOREIGN KEY (studio_id) REFERENCES public.studios(id) ON DELETE CASCADE,
    CONSTRAINT chk_studio_aliases_name_not_blank
        CHECK (btrim(alias_name) <> ''),
    CONSTRAINT chk_studio_aliases_type_allowed
        CHECK (alias_type IN ('former_name', 'legal_name', 'abbreviation', 'other')),
    CONSTRAINT chk_studio_aliases_start_year_range
        CHECK (start_year IS NULL OR start_year BETWEEN 1880 AND 2100),
    CONSTRAINT chk_studio_aliases_end_year_range
        CHECK (end_year IS NULL OR end_year BETWEEN 1880 AND 2100),
    CONSTRAINT chk_studio_aliases_end_after_start
        CHECK (end_year IS NULL OR start_year IS NULL OR end_year >= start_year),
    CONSTRAINT chk_studio_aliases_source_not_blank
        CHECK (btrim(source) <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_studio_aliases_studio_name_start
    ON public.studio_aliases (studio_id, lower(btrim(alias_name)), COALESCE(start_year, 0));

CREATE INDEX IF NOT EXISTS idx_studio_aliases_name_lower
    ON public.studio_aliases (lower(alias_name));

CREATE INDEX IF NOT EXISTS idx_studio_aliases_last_seen_at
    ON public.studio_aliases (last_seen_at);

COMMENT ON TABLE public.studio_aliases IS
    'Historical names, rebrands, legal names, and abbreviations for public.studios.';


CREATE TABLE IF NOT EXISTS public.studio_relationships (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    studio_id BIGINT NOT NULL,
    related_studio_id BIGINT NOT NULL,
    relationship_type TEXT NOT NULL,
    event_year SMALLINT NULL,
    source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_studio_relationships_studio
        FOREIGN KEY (studio_id) REFERENCES public.studios(id) ON DELETE CASCADE,
    CONSTRAINT fk_studio_relationships_related
        FOREIGN KEY (related_studio_id) REFERENCES public.studios(id) ON DELETE CASCADE,
    CONSTRAINT chk_studio_relationships_not_self
        CHECK (studio_id <> related_studio_id),
    CONSTRAINT chk_studio_relationships_type_allowed
        CHECK (
            relationship_type IN (
                'merged_into',
                'succeeded_by',
                'acquired_by',
                'subsidiary_of'
            )
        ),
    CONSTRAINT chk_studio_relationships_event_year_range
        CHECK (event_year IS NULL OR event_year BETWEEN 1880 AND 2100),
    CONSTRAINT uq_studio_relationships_edge
        UNIQUE (studio_id, related_studio_id, relationship_type),
    CONSTRAINT chk_studio_relationships_source_not_blank
        CHECK (btrim(source) <> '')
);

CREATE INDEX IF NOT EXISTS idx_studio_relationships_related
    ON public.studio_relationships (related_studio_id, relationship_type);

CREATE INDEX IF NOT EXISTS idx_studio_relationships_last_seen_at
    ON public.studio_relationships (last_seen_at);

COMMENT ON TABLE public.studio_relationships IS
    'Directed corporate lineage links between studios (mergers, acquisitions, successors, and subsidiaries).';
