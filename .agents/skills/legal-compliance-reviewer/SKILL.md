---
name: legal-compliance-reviewer
description: >-
  Audits data sources, ETL pipelines, scrapers, ephemeral OCR scripts, external API
  integrations, image persistence, PostgreSQL migrations, licensing headers, `.gitignore`
  guards, and monetization features for International Dubbing Database (IDDb) against EU/Italian
  database law, DRM rules, open-data license compatibility, third-party ToS, and privacy rules.
  Use this skill before introducing any new data source, extractor, schema provenance change,
  image storage workflow, or commercial site feature, or whenever the user asks for a compliance review.
---

# IDDb Legal & Compliance Reviewer

Follow this procedure whenever reviewing existing repository state or evaluating a proposed feature, data source, extractor, schema change, or monetization workflow in IDDb.

## Steps

1. Read the normative checklist in [references/legal_framework.md](./references/legal_framework.md) before making any judgment.
2. Inspect only the relevant repository targets read-only (`docs/architecture.md`, `.gitignore`, `LICENSE`, `README.md`, `sql/migrations/`, `etl/`, `ops/`, UI attribution templates, or proposed diffs/specs).
3. Verify external identifier handling against the **Bridge-IDs-Only Rule**:
   - External IDs harvested from `CC0` Wikidata (`imdb_nconst`, `imdb_tconst`, `tmdb_person_id`, `tmdb_id`, `anilist_media_id`, `anilist_character_id`, etc.) are compliant when stored solely for deduplication and outbound links.
   - Any code calling TMDb, IMDb, AniList, MyAnimeList, AniDB, BTVA, Instagram/Meta, or `antoniogenna.net` to persist biographies, cast credits, synopses, or images into Core DB (`public.*` or `staging.*`) or open dumps is blocking (`🛑 NON LEGALE / VIETATO (Bloccante)`).
4. Verify image, DRM, and ephemeral OCR rules:
   - Check that no script bypasses DRM (e.g., Widevine/PlayReady), promotes cinema recording, or redistributes copyrighted film stills/posters/social graphics.
   - Check that domestic end-credit OCR workflows process frames only in memory or in a temporary directory cleaned in a `finally` block, write zero binary images to PostgreSQL/Git/backups/storage, enforce human-in-the-loop review before DB `INSERT`, and are backed by `.gitignore` exclusions.
   - Verify `Wikimedia Commons` assets (`wdt:P18`) capture author, source URL, exact SPDX license, and attribution text per file.
5. Check license compatibility and provenance tracking (`Apache-2.0` code, `CC-BY-SA-4.0` unified DB, Section 4 *sui generis* obligations, no `NC` restrictions, polite Wikimedia `User-Agent` and rate limits, GDPR/NDA safeguards, and fiscal flag on recurring monetization).
6. If running inside the main agent, delegate complex multi-file audits to a read-only `legal-compliance-reviewer` subagent (`EnableWriteTools: false`) if available.
7. Produce the final report using the mandatory four-part structure below (output in Italian when reviewing in Italian):
   - **Verdetto sintetico:** `✅ LEGALE (OK)` | `⚠️ ZONA GRIGIA / A RISCHIO (Richiede accorgimenti)` | `🛑 NON LEGALE / VIETATO (Bloccante)`
   - **Analisi puntuale:** exact files, tables, APIs, or behaviors examined and which law, license clause, or ToS applies.
   - **Rimedio concreto:** specific architectural, SQL, code, or configuration changes needed to stay 100% compliant without stalling the project.
   - **Disclaimer standard:** explicit note that the output is a technical-informational compliance check and does not replace formal legal counsel from a qualified lawyer.
