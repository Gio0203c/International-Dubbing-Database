# Architecture and Schema Decisions

This document explains how International Dubbing Database (IDDb) is structured and records the key design decisions behind the initial `staging` and `public.speakers` schema.

## Repository and Data Separation

IDDb follows the MusicBrainz model of separating the database engine from live data. This Git repository contains only versioned SQL migrations, schema documentation, standalone SPARQL queries, Python ETL code, and unit tests with small sample fixtures. Live data stays inside PostgreSQL. Future public APIs or downloadable snapshots are distributed outside Git history.

Every change to the PostgreSQL schema must be defined in a numbered migration file under `sql/migrations/`. Tables and constraints are never created manually without a matching migration script, so anyone can clone the repository and rebuild an identical empty database from scratch.

## Licensing and Data Governance

IDDb separates software licensing from database licensing: repository code, SQL migrations, SPARQL queries, and tests are distributed under **Apache 2.0**, while the unified public database and future dataset exports are licensed under **CC BY-SA 4.0**. Only open-license or primary-verified sources compatible with **CC BY-SA 4.0** (such as Wikidata `CC0`, official Wikipedia APIs/dumps `CC BY-SA 4.0`, Wikimedia Commons per-file open assets, TVmaze textual metadata `CC BY-SA`, MusicBrainz Core Data `CC0`, and primary-verified end credits) may enter `public.*`. External IDs for proprietary services (`IMDb`, `TMDB`, `AniList`, `MyAnimeList`, `AniDB`, `BTVA`) are harvested solely from `CC0` Wikidata under a strict **Bridge-IDs-Only** rule for deduplication and outbound links.

## Two-Schema Database Model

IDDb uses two schemas inside the same PostgreSQL database: `staging` and `public`.

The `staging` schema acts as a landing zone for raw upstream payloads. For Wikidata, `staging.wikidata_raw` stores the unmodified payload in a `JSONB` column alongside `fetched_at` and a content hash, with no strict domain constraints. This keeps extraction resilient against messy upstream data, lets the transform step be re-run locally without hitting external APIs again, and preserves any new properties added upstream in Wikidata without running automatic `ALTER TABLE` statements during ETL runs.

The `public` schema holds normalized, strictly typed domain tables and the `etl_runs` audit table (`speakers` and its satellite tables in ADR-001 through ADR-006, and works, studios, dubbing editions, characters, and credits in ADR-007). When a new upstream property proves stable and useful across sources, it is promoted into `public` through a deliberate, versioned SQL migration.

## ADR-001: Surrogate Primary Key and Prefixed Public ID

### Context

A speaker table needs a primary key that is unique, non-null, and immutable because future tables such as casts and external-ID links will reference it with foreign keys. Natural keys fail this requirement: `display_name` is neither unique nor permanent, and external IDs such as `wikidata_qid` tie the database to a single provider. A voice actor sourced from official closing credits, studio press releases, or Wikipedia may not yet have a Wikidata item, and Wikidata items are periodically merged or redirected.

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

Birth and death dates are stored as separate `SMALLINT` components (`birth_year`, `birth_month`, `birth_day` and `death_year`, `death_month`, `death_day`) rather than SQL `DATE` values to handle partial precision cleanly without inventing placeholder dates (see ADR-004). Multi-valued attributes such as spoken languages (`P1412`) and the long tail of external IDs (AniList, Anime News Network, MusicBrainz, and others) remain preserved in `staging.wikidata_raw` and will be modeled in dedicated normalized bridge tables (`speaker_languages`, `speaker_external_ids`) in future migrations rather than widening `public.speakers`.

### Column Specification

| Column | Type and Constraints | Purpose |
| :--- | :--- | :--- |
| `id` | `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` | Internal immutable surrogate key |
| `public_id` | `TEXT GENERATED ALWAYS AS (...) STORED UNIQUE NOT NULL` | Public identifier (`sp0000001`, 7+ digits, regex `^sp[0-9]{7,}$`) |
| `display_name` | `TEXT NOT NULL CHECK (btrim(display_name) <> '')` | Credited public name |
| `sort_name` | `TEXT NOT NULL CHECK (btrim(sort_name) <> '')` | Alphabetical sort key (MusicBrainz style) |
| `legal_name` | `TEXT NULL CHECK (legal_name IS NULL OR btrim(legal_name) <> '')` | Legal or birth name when different from `display_name` |
| `birth_year` | `SMALLINT NULL CHECK (birth_year BETWEEN 1850 AND 2100)` | Year of birth |
| `birth_month` | `SMALLINT NULL CHECK (birth_month BETWEEN 1 AND 12)` | Month of birth (only when upstream precision includes month) |
| `birth_day` | `SMALLINT NULL CHECK (birth_day BETWEEN 1 AND 31)` | Day of birth (requires `birth_month`; calendar-validated) |
| `death_year` | `SMALLINT NULL CHECK (death_year BETWEEN 1850 AND 2100)` + `>= birth_year` (full chronological check across year/month/day in ADR-004) | Year of death |
| `death_month` | `SMALLINT NULL CHECK (death_month BETWEEN 1 AND 12)` | Month of death (only when upstream precision includes month) |
| `death_day` | `SMALLINT NULL CHECK (death_day BETWEEN 1 AND 31)` | Day of death (requires `death_month`; calendar-validated) |
| `gender` | `TEXT NULL CHECK (gender IN ('female', 'male', 'non_binary', 'other'))` | Controlled vocabulary using `TEXT` + `CHECK` for easy evolution |
| `native_lang` | `TEXT NULL CHECK (native_lang ~ '^[a-z]{2}$')` | ISO 639-1 native language code (`P103`) |
| `country_code` | `TEXT NULL CHECK (country_code ~ '^[A-Z]{2}$')` | Primary ISO 3166-1 alpha-2 country code |
| `wikidata_qid` | `TEXT UNIQUE NULL CHECK (wikidata_qid ~ '^Q[1-9][0-9]*$')` | Wikidata bridge identifier |
| `imdb_nconst` | `TEXT UNIQUE NULL CHECK (imdb_nconst ~ '^nm[0-9]+$')` | IMDb person bridge identifier (ID only, never IMDb dumps) |
| `tmdb_person_id` | `INTEGER UNIQUE NULL CHECK (tmdb_person_id > 0)` | TMDB person bridge identifier (ID only, never TMDB API payloads) |
| `primary_source` | `TEXT NOT NULL DEFAULT 'wikidata' CHECK (btrim(primary_source) <> '')` | Source that originated or owns the record |
| `is_verified` | `BOOLEAN NOT NULL DEFAULT FALSE` | Protects manually verified rows from automated overwrites |
| `last_seen_at` | `TIMESTAMPTZ NULL` | Last timestamp when an automated ETL run confirmed the record |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT NOW()` | Row creation timestamp |
| `updated_at` | `TIMESTAMPTZ NOT NULL DEFAULT NOW()` | Last timestamp when a column value actually changed |

The entity-relationship diagram for the initial tables is in [speakers_er.puml](diagrams/speakers_er.puml).

## ADR-003: Idempotent Sync, Source Precedence, and Safety Guards

### Context

IDDb will sync periodically from Wikidata and later ingest additional open-license or primary sources compatible with CC BY-SA 4.0 (such as Wikipedia via the MediaWiki API or official dumps, TVmaze textual metadata, MusicBrainz Core Data CC0, and primary-verified closing credits), while restricting proprietary catalogs (TMDB, IMDb, AniList, MyAnimeList, AniDB, BTVA) strictly to CC0 Wikidata bridge identifiers. Automated pipelines must avoid duplicate records, unnecessary writes, accidental deletion of valid entries during upstream outages, and overwriting manual corrections.

### Decision

Loading into `public.speakers` runs inside a transaction using idempotent UPSERT logic guarded by `IS DISTINCT FROM` comparisons so `updated_at` changes only when at least one attribute changes. Running the pipeline twice on unchanged data produces zero row modifications on the second run.

To protect curated data, rows with `is_verified = TRUE` only receive missing values through `COALESCE` during automated syncs; existing verified fields are never overwritten. When future sources are integrated, the loader resolves existing speakers by cascading across bridge identifiers (`wikidata_qid`, then `imdb_nconst`, then `tmdb_person_id`) before inserting a new row, avoiding `UNIQUE` constraint collisions across sources.

Automated syncs never delete rows from `public.speakers` if an entry disappears upstream. Instead, confirmed rows update `last_seen_at`, making upstream drift or Wikidata redirects easy to detect and review safely. Before writing to `public`, the sync runner applies a volume circuit breaker: if the extracted count drops abnormally compared to the previous successful run (for example below 70%), the write is aborted and logged. Every execution records its status and row counts in `public.etl_runs`.

## ADR-004: MusicBrainz-Style Partial Birth and Death Dates

### Context

Biographical dates for voice actors frequently have incomplete precision across public sources. A performer may have a year only, a year and month, a full date, or a month and day without a birth year (very common among Japanese seiyū and performers who keep their age private). Standard SQL `DATE` columns cannot represent partial dates without inventing placeholder values such as `YYYY-01-01` or a dummy year. In addition, Wikidata direct claims (`wdt:P569`, `wdt:P570`) serialize year-only and month-only dates as `YYYY-01-01T00:00:00Z`, which creates false January 1st birthdays if parsed naively.

### Decision

`public.speakers` stores `birth_year`, `birth_month`, and `birth_day` (and symmetrically `death_year`, `death_month`, and `death_day`) as separate `SMALLINT NULL` columns, following MusicBrainz. Database `CHECK` constraints enforce valid ranges, prevent a day without a month, validate calendar month lengths including Gregorian leap years, and enforce chronological order between birth and death even within the same year. A partial index `idx_speakers_birthday` on `(birth_month, birth_day)` supports fast day-and-month lookups.

The Wikidata ETL will query `wikibase:timePrecision` (`11` for day, `10` for month, `9` for year) alongside the timestamp before populating month or day columns.

### Consequences

All four partial-date shapes (year only, year and month, month and day without year, and full date) are stored cleanly with zero placeholder values. Birthday lookups remain fast even when birth years are unknown, and false January 1st dates are prevented at extraction time.

## ADR-005: Self-Referencing Family Relationships Table

### Context

Dubbing frequently runs across multi-generational families, and Wikidata already documents kinship properties between voice actors (`P22` father, `P25` mother, `P40` child, `P3373` sibling, `P26` spouse, `P451` unmarried partner, and `P1038` relative). Adding fixed relationship columns directly to `public.speakers` (such as `father_id` or `spouse_id`) breaks down as soon as a person has multiple siblings, multiple marriages, or broader family links.

### Decision

Family links live in a dedicated self-referencing link table `public.speaker_relationships` (mirroring MusicBrainz's `l_artist_artist` and `link` pattern), where both `speaker_id` and `related_speaker_id` reference `public.speakers(id)` with `ON DELETE CASCADE`.

The `relationship_type` vocabulary distinguishes directed parenthood (`parent_of`, where `speaker_id` is the parent of `related_speaker_id`) from symmetric relations (`sibling`, `spouse`, `partner`, `relative`). A `CHECK` constraint forbids self-links (`speaker_id <> related_speaker_id`), for symmetric relations requires canonical ordering (`speaker_id < related_speaker_id`), and `UNIQUE (speaker_id, related_speaker_id, relationship_type)` prevents reverse or duplicate edges.

Following MusicBrainz, each edge also carries optional lifecycle columns (`is_ended BOOLEAN NULL`, `start_year SMALLINT NULL`, and `end_year SMALLINT NULL`). This allows representing ended relationships such as former spouses or partners (`is_ended = TRUE`) even when divorce or end dates are unknown (`end_year = NULL`), while still storing `P580`/`P582` years when available and enforcing `end_year IS NULL OR is_ended IS TRUE`.

Like `public.speakers`, rows track `source`, `is_verified`, `last_seen_at`, `created_at`, and `updated_at` so automated syncs protect manually curated entries and avoid silent deletions.

### Consequences

`public.speakers` stays clean and normalized while supporting arbitrarily complex family graphs. Because relationships only reference existing rows in `public.speakers` via immutable surrogate IDs, links survive upstream Wikidata redirects, only connect public voice actors already in the database, and are directly queryable with PostgreSQL `WITH RECURSIVE` family tree queries.

## ADR-006: Normalized Speaker Satellite Tables (`speaker_aliases`, `speaker_languages`, `speaker_external_ids`)

### Context

Voice actor profiles accumulate three inherently multi-valued or long-tail attributes that do not fit inside single scalar columns on `public.speakers`:

1. Alternative names, closing-credit variants, former legal or maiden names, and cross-script transliterations (for example Japanese kanji and romaji variants).
2. Multiple native, working, or spoken languages beyond the single scalar `native_lang` hint.
3. Dozens of regional or domain-specific external identifiers (such as AniList, MyAnimeList, Anime News Network, Behind The Voice Actors, MusicBrainz, and Wikipedia sitelinks).

Storing arrays or delimited strings in `public.speakers` breaks First Normal Form and complicates indexed lookups, while adding dedicated columns for every external catalog requires schema migrations whenever a new source is integrated.

### Decision

Multi-valued speaker metadata is stored in three normalized `1:N` satellite tables in `sql/migrations/007_create_speaker_satellites.sql`, each referencing `public.speakers(id)` with `ON DELETE CASCADE` and carrying the standard curation and audit metadata (`source`, `is_verified`, `last_seen_at`, `created_at`, `updated_at`):

- `public.speaker_aliases` records `alias_name`, optional `sort_name`, controlled `alias_type` (`stage_name`, `legal_name`, `maiden_name`, `credit_variant`, `transliteration`, `other`), optional BCP-47 `locale` (`^[a-z]{2}(-[A-Z][a-z]{3})?(-[A-Z]{2})?$`), and `is_primary`. Functional unique indexes enforce case-insensitive, whitespace-trimmed, NULL-safe uniqueness `(speaker_id, lower(btrim(alias_name)), COALESCE(locale, ''))` and at most one primary alias per `(speaker_id, COALESCE(locale, ''))`.
- `public.speaker_languages` records two-letter ISO 639-1 `lang_code` values paired with `role` (`native`, `working`, `spoken`), enforcing `UNIQUE (speaker_id, lang_code, role)`.
- `public.speaker_external_ids` stores `(provider, external_id)` pairs with a lowercase slug check on `provider` (`^[a-z][a-z0-9_]{1,49}$`), `UNIQUE (provider, external_id)` so one external catalog ID cannot map to two different speakers, and `CHECK (provider NOT IN ('wikidata', 'imdb', 'tmdb'))` to preserve a Single Source of Truth with the core bridge columns on `public.speakers`.

### Consequences

`public.speakers` remains compact and schema-stable as new providers and international scripts are added. Alias search stays fast via `lower(alias_name)`, primary bridge matching (`wikidata_qid`, `imdb_nconst`, `tmdb_person_id`) has no conflicting duplicate representation, and future ETL connectors can populate all three satellite tables idempotently without database schema changes.

## ADR-007: Core Dubbing and Media Domain Model

### Context

Modeling dubbing casts globally requires separating an original work from its localized audio versions, tracking both animation and live-action dubs, supporting multiple historical re-dubs or regional language variants (such as European vs. Latin American Spanish), capturing non-acting localization crew roles (dubbing directors, dialogue adapters, assistant directors, sound engineers, musical directors, and theme-song performers), and handling episodic voice recasts.

Upstream media catalogs also use incompatible identifier namespaces: for example, TMDB assigns independent integer IDs to movies and TV series, so a movie and a series can share the same numeric `tmdb_id`.

### Decision

`sql/migrations/008_create_core_dubbing_model.sql` introduces five normalized domain tables in `public`, linked cleanly to `public.speakers`:

- `public.media_sources` (`md` prefixed `public_id`) stores original works across `media_type` values (`movie`, `tv_series`, `miniseries`, `ova`, `short`, `documentary`, `video_game`, `audiobook`, `web_series`, `special`, `other`), orthogonal to `is_animated BOOLEAN NULL`, with an optional self-referencing `parent_media_id` for sequels, specials, or spin-offs. Upstream bridges include `wikidata_qid`, `imdb_tconst` (`^tt[0-9]+$`), `anilist_media_id`, and the composite pair `(tmdb_media_type, tmdb_id)` enforced as a paired `CHECK` and `UNIQUE (tmdb_media_type, tmdb_id)`.
- `public.studios` (`st` prefixed `public_id`) models dubbing studios and localization houses with optional country, city, founding/closure years, `wikidata_qid`, and `website_url`.
- `public.dubbing_editions` (`ed` prefixed `public_id`) models each audio version of a work by `lang_code` (ISO 639-1), optional `region_code` (ISO 3166-1 alpha-2), `edition_type` (`dub`, `redub`, `original`, `voice_over`), optional `edition_label`, `release_year`, `localized_title`, and optional `studio_id`. A NULL-safe functional unique index (`uq_dubbing_editions_natural`) on `(media_source_id, lang_code, COALESCE(region_code, ''), edition_type, COALESCE(release_year, 0), COALESCE(lower(btrim(edition_label)), ''))` allows multiple re-dubs and regional variants while preventing exact duplicates.
- `public.characters` (`ch` prefixed `public_id`) models voiced characters with an optional `media_source_id` (`ON DELETE SET NULL`) so recurring franchise characters can exist independently of a single film, alongside optional `gender`, `wikidata_qid`, and `anilist_character_id`.
- `public.dubbing_credits` links a `dubbing_edition_id` (`ON DELETE CASCADE`), `speaker_id` (`ON DELETE CASCADE`), and optional `character_id` (`ON DELETE SET NULL`), covering voice and crew `role_type` values (`voice`, `singing`, `theme_song`, `narrator`, `dubbing_director`, `dialogue_adapter`, `assistant_director`, `sound_engineer`, `musical_director`, `additional_voices`, `other`), optional `credited_as` for on-screen pseudonyms, optional `episode_range` for mid-series voice changes, optional live-action performer references (`original_actor_name`, `original_actor_qid`), plus optional `is_main_cast` and `notes`. The functional unique index `uq_dubbing_credits_natural` enforces uniqueness on `(dubbing_edition_id, speaker_id, role_type, COALESCE(character_id, -1), COALESCE(lower(btrim(original_actor_name)), ''), COALESCE(lower(btrim(episode_range)), ''))`.

The entity-relationship diagram for these tables is in [dubbing_er.puml](diagrams/dubbing_er.puml).

### Consequences

Re-dubs, regional localizations, original voice casts, non-acting crew credits, episodic voice changes, and live-action character-actor-speaker triples (`character | original_actor | dubbing_speaker`) are all queryable in normalized SQL without duplicating works or characters. Indexes on `dubbing_credits (speaker_id, role_type)` and `(role_type, speaker_id)` support fast filtering by localization role (such as listing all dubbing directors or dialogue adapters).

## ADR-008: Studio Lifecycle, Former Names, and Corporate Lineage

### Context

Dubbing studios frequently rename over time (for instance, a single company rebranding from `Gruppo Trenta` to `Pumais` and later `Pumaisdue`) or cease independent operation through corporate mergers and acquisitions (for instance, `CDC` and `Sefit` merging into `CDC Sefit Group`, or multi-step acquisitions such as `SDI Media` -> `Iyuno-SDI` -> `Iyuno`). Older dubbing editions still credit the historical studio name used at release time, and users searching a legacy name need to understand both its current operational status and its modern successor.

### Decision

`sql/migrations/009_add_studio_history_and_aliases.sql` models studio rebrands, lifecycle state, and corporate lineage cleanly across two mechanisms:

- **Same studio, historical rebrands:** `public.studio_aliases` stores `alias_name`, controlled `alias_type` (`former_name`, `legal_name`, `abbreviation`, `other`), and optional `start_year`/`end_year` bounds, with a NULL-safe functional unique index `uq_studio_aliases_studio_name_start` on `(studio_id, lower(btrim(alias_name)), COALESCE(start_year, 0))`. `public.dubbing_editions` adds an optional `studio_credited_as` column so an edition can retain the historical screen credit alongside `studio_id`.
- **Distinct entities merging or closing:** `public.studios` adds `status TEXT NULL` (`CHECK (status IS NULL OR status IN ('active', 'closed', 'merged'))`) paired with `chk_studios_closed_year_implies_inactive` (`closed_year IS NULL OR (status IS NOT NULL AND status IN ('closed', 'merged'))`), allowing inactive studios to be flagged even when `closed_year` is unknown. `public.studio_relationships` models directed corporate transitions (`merged_into`, `succeeded_by`, `acquired_by`, `subsidiary_of`) with optional `event_year`, non-self check, and `UNIQUE (studio_id, related_studio_id, relationship_type)`.

### Consequences

Historical edition credits stay intact under either historical aliases or predecessor entities without losing attribution. Searching any former or merged studio immediately resolves its status (`active`, `closed`, `merged`), legacy time span, and current corporate successor via single-hop joins or recursive lineage traversal (`WITH RECURSIVE`), reflected in [dubbing_er.puml](diagrams/dubbing_er.puml).
