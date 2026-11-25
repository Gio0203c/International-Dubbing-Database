# IDDB Data Governance & Legal Compliance Rule

These workspace constraints govern every migration, ETL connector, script, UI integration, and documentation update in International Dubbing Database (`IDDB`).

## Mandatory Project Boundaries

1. **Licensing split:** Repository code, SQL migrations, SPARQL queries, and tests are **Apache 2.0**. The unified database and future public snapshots are **CC BY-SA 4.0**. Never introduce `CC BY-NC-*` or proprietary datasets into Core DB (`public.*` or `staging.*`) or open dumps.
2. **Bridge-IDs-Only rule for proprietary platforms:** External IDs already sourced from `CC0` Wikidata (`imdb_nconst`, `imdb_tconst`, `tmdb_person_id`, `tmdb_id`, `anilist_media_id`, `anilist_character_id`, etc.) are stored strictly for deduplication and outbound hyperlinks. Never query TMDb, IMDb, AniList, MyAnimeList, AniDB, BTVA, IGDB, Meta/Instagram, or *antoniogenna.net* to persist bios, credits, synopses, or images into Core DB or open dumps.
3. **Permitted Core DB sources:** Wikidata (`CC0-1.0`), it.Wikipedia via official Wikimedia XML dumps or Action/REST API (`CC-BY-SA-4.0`, with `page_id` and `revision_id`), Wikimedia Commons via `wdt:P18` (tracking author, URL, and SPDX license per file), TVmaze text metadata only (`CC-BY-SA-4.0`, no images), MusicBrainz Core Data only (`CC0-1.0`, no Supplementary Data), and clean-room verified factual credits from primary sources.
4. **Ephemeral domestic OCR only:** Any OCR pipeline for end-credit photos taken domestically must operate strictly in memory or a temporary folder wiped in `finally`, persist zero images anywhere (DB, Git, backups, object storage), never promote DRM circumvention or cinema camcording, and require human review before database insertion.
5. **Pre-implementation gate:** Before implementing or proposing a new external data source, ETL pipeline, scraper, OCR workflow, provenance schema change, asset store, or site monetization feature, run or consult the `legal-compliance-reviewer` skill (`.agents/skills/legal-compliance-reviewer/SKILL.md`).
