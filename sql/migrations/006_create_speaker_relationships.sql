-- 006_create_speaker_relationships.sql
-- Self-referencing kinship table linking speakers (MusicBrainz l_artist_artist + link pattern).
-- 'parent_of' is directed (speaker_id is parent of related_speaker_id).
-- Symmetric types ('sibling', 'spouse', 'partner', 'relative') require speaker_id < related_speaker_id
-- so each undirected link is stored once without reverse duplicates.

CREATE TABLE IF NOT EXISTS public.speaker_relationships (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    speaker_id BIGINT NOT NULL,
    related_speaker_id BIGINT NOT NULL,
    relationship_type TEXT NOT NULL,
    is_ended BOOLEAN NULL,
    start_year SMALLINT NULL,
    end_year SMALLINT NULL,
    source TEXT NOT NULL DEFAULT 'wikidata',
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_speaker_relationships_speaker
        FOREIGN KEY (speaker_id) REFERENCES public.speakers(id) ON DELETE CASCADE,
    CONSTRAINT fk_speaker_relationships_related
        FOREIGN KEY (related_speaker_id) REFERENCES public.speakers(id) ON DELETE CASCADE,
    CONSTRAINT chk_speaker_relationships_not_self
        CHECK (speaker_id <> related_speaker_id),
    CONSTRAINT chk_speaker_relationships_type_allowed
        CHECK (relationship_type IN ('parent_of', 'sibling', 'spouse', 'partner', 'relative')),
    CONSTRAINT chk_speaker_relationships_symmetric_order
        CHECK (relationship_type = 'parent_of' OR speaker_id < related_speaker_id),
    CONSTRAINT chk_speaker_relationships_start_year_range
        CHECK (start_year IS NULL OR start_year BETWEEN 1850 AND 2100),
    CONSTRAINT chk_speaker_relationships_end_year_range
        CHECK (end_year IS NULL OR end_year BETWEEN 1850 AND 2100),
    CONSTRAINT chk_speaker_relationships_end_after_start
        CHECK (end_year IS NULL OR start_year IS NULL OR end_year >= start_year),
    CONSTRAINT chk_speaker_relationships_end_year_implies_ended
        CHECK (end_year IS NULL OR is_ended IS TRUE),
    CONSTRAINT uq_speaker_relationships_edge
        UNIQUE (speaker_id, related_speaker_id, relationship_type),
    CONSTRAINT chk_speaker_relationships_source_not_blank
        CHECK (btrim(source) <> '')
);

CREATE INDEX IF NOT EXISTS idx_speaker_relationships_related
    ON public.speaker_relationships (related_speaker_id, relationship_type);

CREATE INDEX IF NOT EXISTS idx_speaker_relationships_last_seen_at
    ON public.speaker_relationships (last_seen_at);

COMMENT ON TABLE public.speaker_relationships IS
    'Self-referencing family and kinship links between speakers (MusicBrainz l_artist_artist + link pattern).';
