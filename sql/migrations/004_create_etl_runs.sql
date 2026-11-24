-- 004_create_etl_runs.sql
-- Audit log for automated ETL executions and circuit-breaker checks.

CREATE TABLE IF NOT EXISTS public.etl_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ NULL,
    rows_extracted INTEGER NOT NULL DEFAULT 0,
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    rows_updated INTEGER NOT NULL DEFAULT 0,
    rows_discarded INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NULL,

    CONSTRAINT chk_etl_runs_source_not_blank CHECK (btrim(source) <> ''),
    CONSTRAINT chk_etl_runs_status_allowed CHECK (
        status IN ('running', 'succeeded', 'failed', 'aborted_circuit_breaker')
    ),
    CONSTRAINT chk_etl_runs_finished_after_started CHECK (
        finished_at IS NULL OR finished_at >= started_at
    ),
    CONSTRAINT chk_etl_runs_counts_non_negative CHECK (
        rows_extracted >= 0
        AND rows_inserted >= 0
        AND rows_updated >= 0
        AND rows_discarded >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_etl_runs_source_started
    ON public.etl_runs (source, started_at DESC);

COMMENT ON TABLE public.etl_runs IS
    'Execution history, row counts, and error tracking for ETL runs.';
