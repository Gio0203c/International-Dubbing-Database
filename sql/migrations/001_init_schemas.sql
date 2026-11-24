-- 001_init_schemas.sql
-- Initialize the two-schema layout: staging for raw upstream payloads and
-- public for normalized domain tables and ETL audit records.

CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS public;

COMMENT ON SCHEMA staging IS
    'Raw upstream source payloads stored as JSONB without rigid domain constraints.';
