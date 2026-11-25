# IDDb Legal & Data Compliance Reference

Authoritative checklist for **International Dubbing Database (IDDb)**.
Repository source code, SQL migrations, SPARQL queries, and tests are licensed under **Apache 2.0**. The unified public database and exports are licensed under **CC BY-SA 4.0**.

## 1. EU & Italian Database Law

- **Raw facts vs. protected databases:** Individual historical facts (e.g., an actor voicing a character in a title) are uncopyrightable facts, whereas substantial or systematic/repeated extraction from protected compilations triggers copyright and EU *sui generis* database rights (**Directive 96/9/EC**; **Italian Law 633/1941, Arts. 102-bis and 102-ter**).
- **Zero third-party DB scraping:** Never scrape, mirror, or systematically ingest from proprietary/curated dubbing databases (such as *antoniogenna.net*, BTVA, AniDB, MyAnimeList) or proprietary entertainment catalogs.
- **No Wikipedia data laundering:** Never copy or extract content from Wikipedia pages suspected of verbatim third-party database copying without verifying independent primary attribution or discarding suspicious edits.

## 2. DRM, Ephemeral OCR, and Text & Data Mining (TDM)

- **Strict anti-circumvention ban (Art. 102-quater LdA):** Never implement or document tools that circumvent DRM/TPM (Widevine, PlayReady, FairPlay, encrypted streams) on streaming services.
- **Strict cinema anti-camcording ban:** Never accept or capture photos/recordings taken inside movie theaters.
- **Ephemeral domestic end-credit OCR rules:** Local OCR on photographs of official end credits viewed domestically (TV broadcast, consumer streaming screen, Blu-ray/DVD) is permitted strictly to transcribe uncopyrightable factual credits under these mandatory safeguards:
  1. Photos/frames must stay **ephemeral**: processed solely in memory or inside a `tempfile` directory cleaned unconditionally inside a `finally` block.
  2. **Zero persistence:** Binary images or cropped frames must NEVER be stored in PostgreSQL (`staging.*` or `public.*`), Git history, website object storage, or backups. Enforce `.gitignore` blocks for local photo/OCR capture patterns.
  3. **Mandatory human-in-the-loop review:** OCR must emit a structured review draft (JSON/YAML or moderation form) checked and corrected by a human contributor before definitive PostgreSQL insertion, recording exact primary citation metadata (`distribution_edition`, `credit_timecode`, `episode_or_cut`, contributor ID, timestamps).
  4. **Consumer streaming ToS nuance:** Note as a gray-area operational caution that some consumer streaming platform Terms of Service restrict using their service outputs for commercial activity beyond private personal viewing; rely strictly on factual transcription (not screen-capture redistribution) and prefer optical media/studio announcements where possible.
- **TDM limits (Arts. 70-ter and 70-quater LdA):** Statutory TDM exemptions do not override anti-TPM rules, cannot legitimize redistributing protected third-party databases in a public `CC BY-SA 4.0` dump, and yield to machine-readable opt-outs.

## 3. Open-Data License Compatibility

- **Allowed Core DB (`public.*`) & `CC-BY-SA-4.0` dump sources:**
  - **Wikidata (`CC0-1.0`):** Entities, QIDs, family relations, partial dates, and external bridge IDs. Compatible with both `CC0-1.0` and `CC-BY-SA-4.0`.
  - **it.Wikipedia (`CC-BY-SA-4.0`):** Official Wikimedia XML dumps (`dumps.wikimedia.org`) or Action/REST API using a compliant Wikimedia `User-Agent`, storing `page_id` and `revision_id` for attribution and history. Textual/table extraction must respect Section 4 (*Sui Generis Database Rights*) and attribute contributors/revision URLs.
  - **Wikimedia Commons (`wdt:P18`):** Allowed only when tracking per-file `commons_filename`, author, source URL, exact SPDX license (`CC0-1.0`, `CC-BY-4.0`, `CC-BY-SA-4.0`, etc. — reject `NC` or non-free media), and attribution text.
  - **TVmaze (`CC-BY-SA-4.0`):** Allowed only for textual metadata/original TV cast with attribution and share-alike; **images/posters from TVmaze are excluded**.
  - **MusicBrainz (`CC0-1.0`):** Allowed only for **Core Data** (artists, recordings, release credits such as theme-song singers/narrators); **exclude Supplementary Data (`CC-BY-NC-SA`)**.
  - **Primary/Community Verified Facts:** Clean-room factual entries from official end credits, official studio posts/press releases, and actor announcements, released by contributors under `CC-BY-SA-4.0` / project-compatible open license without copying copyrighted social graphics or prose.
- **Share-Alike & Anti-NC integrity:** Never mix `CC BY-NC-*` datasets into the `CC-BY-SA-4.0` DB, because `NC` restrictions conflict with Wikimedia `BY-SA` compatibility and block commercial site monetization.

## 4. Third-Party API & Platform ToS Matrix

- **Bridge-IDs-Only Rule:** Keeping external identifiers harvested from `CC0` Wikidata (`imdb_nconst`, `imdb_tconst`, `tmdb_person_id`, `tmdb_media_type`/`tmdb_id`, `anilist_media_id`, `anilist_character_id`, `myanimelist`, `btva`, etc.) for deduplication and outbound links is **compliant**. Calling proprietary APIs/dumps to persist biographies, plot summaries, cast credits, or images in Core DB or open dumps is **forbidden**.
- **Specific platform rules:**
  - **TMDb API:** Forbids open-source redistribution, caps cache at 6 months, and treats ad-supported/subscription/sponsored sites (even cost-recovery only) as commercial usage requiring a paid license (`sales@themoviedb.org`). Zero TMDb content in Core DB.
  - **IMDb Non-Commercial Datasets:** Prohibits building public/redistributable databases or commercial applications without an enterprise AWS Data Exchange license. Zero IMDb dataset ingestion.
  - **AniList / MyAnimeList / AniDB / BTVA:** Non-commercial GraphQL thresholds, anti-scraper rules (`Jikan` is unofficial), and proprietary restrictions prohibit ingestion into an open-source commercializable DB.
  - **Instagram / Meta platforms:** Automated scraping bots are forbidden by platform ToS. Official studio announcements must be read manually and transcribed strictly as verifiable factual credits with post URL/date.
  - **IGDB (Twitch/Amazon):** Allows free commercial website usage with attribution (video games only), but forbids open-source redistribution; therefore never persist IGDB payloads inside Core DB or open dumps.
  - **Wikimedia APIs / WDQS:** Must send a descriptive HTTP `User-Agent` with contact URL/email, obey rate limits (`Retry-After`, backoff), and persist raw staging payloads idempotently.

## 5. Privacy, GDPR, NDA, and Restricted Materials

- **Public figures minimization (GDPR):** Store only public professional biographical facts from open/verified sources. Never store private emails, phone numbers, home addresses, national IDs, or non-public relatives (enforced in IDDb via `public.speakers` FK constraints on `speaker_relationships`).
- **Pre-release / NDA ban:** Never ingest leaked scripts, confidential studio call sheets, or credits covered by active release embargoes/NDAs.
- **Copyrighted media ban:** Never store or redistribute copyrighted film frames, promotional stills, posters, or studio Instagram promotional graphics.

## 6. Monetization & Fiscal Informational Flag

- Any new website feature enabling recurring direct revenue (AdSense/display ads, paid memberships/subscriptions, commercial sponsorships, Patreon/Ko-fi tiers with rewards) must trigger an informational reminder in the review report noting that continuous commercial operation activates Italian tax/VAT/business-registration obligations (`Partita IVA`, ATECO, commercialista consultation).
