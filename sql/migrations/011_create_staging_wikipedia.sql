-- 011_create_staging_wikipedia.sql
-- Raw landing table for multi-language Wikipedia API and XML-dump extractions.
-- Archives wikitext per page revision alongside anti-data-laundering flags
-- before selective promotion to public.speakers and public.provenance_records.

CREATE TABLE IF NOT EXISTS staging.wikipedia_raw (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NULL,
    wiki_lang TEXT NOT NULL,
    page_id BIGINT NOT NULL,
    revision_id BIGINT NOT NULL,
    page_title TEXT NOT NULL,
    page_url TEXT NOT NULL,
    wikidata_qid TEXT NULL,
    wikitext TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    has_copyviol_or_unsourced_flag BOOLEAN NOT NULL DEFAULT FALSE,
    compliance_flags TEXT[] NOT NULL DEFAULT '{}',
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_wikipedia_raw_lang_page_rev
    ON staging.wikipedia_raw (wiki_lang, page_id, revision_id);

CREATE INDEX IF NOT EXISTS idx_wikipedia_raw_lang_qid
    ON staging.wikipedia_raw (wiki_lang, wikidata_qid)
    WHERE wikidata_qid IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_wikipedia_raw_run_id
    ON staging.wikipedia_raw (run_id)
    WHERE run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_wikipedia_raw_lang_title
    ON staging.wikipedia_raw (wiki_lang, lower(page_title));

COMMENT ON TABLE staging.wikipedia_raw IS
    'Unmodified Wikipedia page revisions archived by wiki_lang and revision_id for reproducible parsing and anti-data-laundering screening.';
