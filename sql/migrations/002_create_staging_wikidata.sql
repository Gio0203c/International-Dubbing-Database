-- 002_create_staging_wikidata.sql
-- Raw landing table for Wikidata Query Service (WDQS) payloads.
-- Kept intentionally free of rigid domain constraints and cross-schema foreign keys.

CREATE TABLE IF NOT EXISTS staging.wikidata_raw (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NULL,
    wikidata_qid TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wikidata_raw_qid_fetched
    ON staging.wikidata_raw (wikidata_qid, fetched_at DESC);

CREATE INDEX IF NOT EXISTS idx_wikidata_raw_run_id
    ON staging.wikidata_raw (run_id)
    WHERE run_id IS NOT NULL;

COMMENT ON TABLE staging.wikidata_raw IS
    'Unmodified Wikidata payloads archived per extraction run for reproducible transforms.';
