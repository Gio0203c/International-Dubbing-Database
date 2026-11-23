# International Dubbing Database

International Dubbing Database (IDDB) is an open relational database for dubbing metadata: voice actors, works, dubbing editions, casts, and bridge identifiers to external databases.

This repository contains the database engine only: SQL migrations, schema documentation, SPARQL queries, Python ETL scripts, and unit tests. Live data lives in PostgreSQL and is never committed to Git.

The schema is designed to be country- and language-agnostic, using ISO standards for languages and countries. Data ingestion relies only on open or permitted APIs, starting with the Wikidata Query Service (CC0). HTML scraping of protected databases and redistributing restricted dumps (such as IMDb `.tsv.gz` files) are not allowed.

## Prerequisites

- PostgreSQL 17+
- Python 3.11+
- pytest

## Setup

Clone the repository and create a local `.env` file from the template:

```bash
git clone https://github.com/Gio0203c/International-Dubbing-Database.git
cd International-Dubbing-Database
cp .env.example .env
```

Edit `.env` with your local PostgreSQL connection settings and a valid `WIKIDATA_USER_AGENT` string. Never commit `.env`.

Database migrations and ETL scripts are currently under initial development.

## License

International Dubbing Database is released under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
