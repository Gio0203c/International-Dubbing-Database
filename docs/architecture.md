# Architecture and Schema Decisions

This document explains how International Dubbing Database (IDDB) is structured and records the key design decisions behind the initial `staging` and `public.speakers` schema.

## Repository and Data Separation

IDDB follows the MusicBrainz model of separating the database engine from live data. This Git repository contains only versioned SQL migrations, schema documentation, standalone SPARQL queries, Python ETL code, and unit tests with small sample fixtures. Live data stays inside PostgreSQL. Future public APIs or downloadable snapshots are distributed outside Git history.

Every change to the PostgreSQL schema must be defined in a numbered migration file under `sql/migrations/`. Tables and constraints are never created manually without a matching migration script, so anyone can clone the repository and rebuild an identical empty database from scratch.

## Two-Schema Database Model

IDDB uses two schemas inside the same PostgreSQL database: `staging` and `public`.

The `staging` schema acts as a landing zone for raw upstream payloads. For Wikidata, `staging.wikidata_raw` stores the unmodified payload in a `JSONB` column alongside `fetched_at` and a content hash, with no strict domain constraints. This keeps extraction resilient against messy upstream data, lets the transform step be re-run locally without hitting external APIs again, and preserves any new properties added upstream in Wikidata without running automatic `ALTER TABLE` statements during ETL runs.

The `public` schema holds normalized, strictly typed domain tables and the `etl_runs` audit table. The first domain table is `public.speakers`. Future migrations will add works, dubbing editions, casts, and supporting link tables. When a new upstream property proves stable and useful across sources, it is promoted into `public` through a deliberate, versioned SQL migration.

## ADR-001: Surrogate Primary Key and Prefixed Public ID

### Context

A speaker table needs a primary key that is unique, non-null, and immutable because future tables such as casts and external-ID links will reference it with foreign keys. Natural keys fail this requirement: `display_name` is neither unique nor permanent, and external IDs such as `wikidata_qid` tie the database to a single provider. A voice actor sourced from TMDB, AniList, or closing credits may not yet have a Wikidata item, and Wikidata items are periodically merged or redirected.

### Decision

`public.speakers` uses a dual-key pattern combining an internal integer primary key (like MusicBrainz) with a readable entity-prefixed public identifier (like IMDb):

- `id` is `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`. All internal foreign keys and joins reference `id`.
- `public_id` is a stored generated column (`TEXT UNIQUE NOT NULL`) derived from `id` with the prefix `sp` and zero-padded to at least 7 digits (`sp0000001` through `sp9999999`). Above `9999999`, it expands naturally to 8 or more digits (`sp10000000`, ...) using a conditional expression rather than bare `lpad`, which would otherwise truncate values.
- External identifiers (`wikidata_qid`, `imdb_nconst`, `tmdb_person_id`) are stored as `UNIQUE NULLABLE` bridge columns.

Future entity tables will follow the same convention with distinct two-letter prefixes (for example `md` for media works, `ed` for dubbing editions, `ch` for characters, and `st` for studios).

### Consequences

Internal joins stay compact and fast on 64-bit integers. Upstream redirects on Wikidata require updating a single nullable column on one row without cascading changes to foreign keys. Speakers without a Wikidata item can still be inserted cleanly from other legal sources. External consumers, URLs, and exports get a stable, unambiguous identifier whose prefix immediately signals the entity type.

## ADR-002: Global-First `public.speakers` Columns and Nullability

### Context

Empirical profiling of the Wikidata Italian voice actor slice (`wdt:P106/wdt:P279* wd:Q2405480`, 996 entities) showed that almost every biographical attribute can be absent or multi-valued upstream: birth date (`P569`) is present on 96.6% of entries, IMDb ID (`P345`) on 80.1%, TMDB person ID (`P4985`) on 45.4%, death date (`P570`) on 31.9%, birth name (`P1477`) on 5.9%, and native language (`P103`) on 3.7%. Spoken languages (`P1412`, 97.2%) and citizenship (`P27`) frequently contain multiple values per person, and over 30 distinct external ID properties appear across a small sample of speakers.

Splitting personal names into Western first-name and last-name columns also breaks international coverage for mononyms, stage names, compound surnames, and Eastern name orders.

### Decision

Only `display_name`, `sort_name`, primary/public keys, and technical audit fields are `NOT NULL`. All other biographical attributes and bridge IDs are `NULLABLE`.

Name representation follows MusicBrainz: `display_name` holds the public credited name (for example `Domitilla D'Amico`) and `sort_name` holds the alphabetical sorting form (for example `D'Amico, Domitilla`, falling back to `display_name` when no inverted form is known or applicable). This allows fast indexed sorting by surname without forcing a Western first/last split.

Years (`birth_year`, `death_year`) are stored as `SMALLINT` rather than full `DATE` values to handle partial date precision cleanly without inventing placeholder `YYYY-01-01` dates. Multi-valued attributes such as spoken languages (`P1412`) and the long tail of external IDs (AniList, Anime News Network, MusicBrainz, and others) remain preserved in `staging.wikidata_raw` and will be modeled in dedicated normalized bridge tables (`speaker_languages`, `speaker_external_ids`) in future migrations rather than widening `public.speakers`.

### Column Specification

| Column | Type and Constraints | Purpose |
| :--- | :--- | :--- |
| `id` | `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` | Internal immutable surrogate key |
| `public_id` | `TEXT GENERATED ALWAYS AS (...) STORED UNIQUE NOT NULL` | Public identifier (`sp0000001`, 7+ digits, regex `^sp[0-9]{7,}$`) |
| `display_name` | `TEXT NOT NULL CHECK (btrim(display_name) <> '')` | Credited public name |
| `sort_name` | `TEXT NOT NULL CHECK (btrim(sort_name) <> '')` | Alphabetical sort key (MusicBrainz style) |
| `legal_name` | `TEXT NULL` | Legal or birth name when different from `display_name` |
| `birth_year` | `SMALLINT NULL CHECK (birth_year BETWEEN 1850 AND 2100)` | Year of birth |
| `death_year` | `SMALLINT NULL CHECK (death_year BETWEEN 1850 AND 2100)` + `>= birth_year` | Year of death |
| `gender` | `TEXT NULL CHECK (gender IN ('female', 'male', 'non_binary', 'other'))` | Controlled vocabulary using `TEXT` + `CHECK` for easy evolution |
| `native_lang` | `TEXT NULL CHECK (native_lang ~ '^[a-z]{2}$')` | ISO 639-1 native language code (`P103`) |
| `country_code` | `TEXT NULL CHECK (country_code ~ '^[A-Z]{2}$')` | Primary ISO 3166-1 alpha-2 country code |
| `wikidata_qid` | `TEXT UNIQUE NULL CHECK (wikidata_qid ~ '^Q[1-9][0-9]*$')` | Wikidata bridge identifier |
| `imdb_nconst` | `TEXT UNIQUE NULL CHECK (imdb_nconst ~ '^nm[0-9]+$')` | IMDb person bridge identifier (ID only, never IMDb dumps) |
| `tmdb_person_id` | `INTEGER UNIQUE NULL CHECK (tmdb_person_id > 0)` | TMDB person bridge identifier |
| `primary_source` | `TEXT NOT NULL DEFAULT 'wikidata'` | Source that originated or owns the record |
| `is_verified` | `BOOLEAN NOT NULL DEFAULT FALSE` | Protects manually verified rows from automated overwrites |
| `last_seen_at` | `TIMESTAMPTZ NULL` | Last timestamp when an automated ETL run confirmed the record |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT NOW()` | Row creation timestamp |
| `updated_at` | `TIMESTAMPTZ NOT NULL DEFAULT NOW()` | Last timestamp when a column value actually changed |

The entity-relationship diagram for the initial tables is in [speakers_er.puml](diagrams/speakers_er.puml).

## ADR-003: Idempotent Sync, Source Precedence, and Safety Guards

### Context

IDDB will sync periodically from Wikidata and later ingest additional legal sources (such as TMDB API, AniList GraphQL, and Wikipedia categories via the MediaWiki API). Automated pipelines must avoid duplicate records, unnecessary writes, accidental deletion of valid entries during upstream outages, and overwriting manual corrections.

### Decision

Loading into `public.speakers` runs inside a transaction using idempotent UPSERT logic guarded by `IS DISTINCT FROM` comparisons so `updated_at` changes only when at least one attribute changes. Running the pipeline twice on unchanged data produces zero row modifications on the second run.

To protect curated data, rows with `is_verified = TRUE` only receive missing values through `COALESCE` during automated syncs; existing verified fields are never overwritten. When future sources are integrated, the loader resolves existing speakers by cascading across bridge identifiers (`wikidata_qid`, then `imdb_nconst`, then `tmdb_person_id`) before inserting a new row, avoiding `UNIQUE` constraint collisions across sources.

Automated syncs never delete rows from `public.speakers` if an entry disappears upstream. Instead, confirmed rows update `last_seen_at`, making upstream drift or Wikidata redirects easy to detect and review safely. Before writing to `public`, the sync runner applies a volume circuit breaker: if the extracted count drops abnormally compared to the previous successful run (for example below 70%), the write is aborted and logged. Every execution records its status and row counts in `public.etl_runs`.
