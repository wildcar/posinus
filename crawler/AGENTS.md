# Crawler — Agent Instructions

Local instructions for `crawler/`. Read the repository root [`AGENTS.md`](../AGENTS.md) first:
it holds the service boundaries, the shared rules, the memory store and the environment. This
file covers only what is specific to this service.

## What it is

Positive News Crawler — a multilingual public-news collector with a SQLite feedback loop and an
operator UI. It owns the database and the `exchange_*` contract that `pipeline/` consumes.

## Documents

| File | Role |
|------|------|
| `AGENTS/SPEC.md` | Functional and technical source of truth for the crawler. |
| `AGENTS/STATE.md` | Current snapshot: goal, now, next, open questions, deferred. |
| `AGENTS/HISTORY.md` | Append-only iteration log, newest first. |
| `README.md` | User-facing installation, operation and selector contract (Russian). |
| `docs/adr/` | Architecture Decision Records for the crawler. |

Shared, at the root: `../AGENTS/MEMORY.md`, `../AGENTS/ENV.md`,
`../docs/contracts/database-contract.md`, `../docs/deployment.md`.

## Stack & Commands

Python 3.12/3.13/3.14, Django 5.2 LTS, SQLite WAL, Trafilatura, Feedparser, Playwright
Chromium, Waitress, Pytest. Run everything from this directory.

```bash
# install (Ubuntu / Windows)
sh scripts/install.sh
./scripts/install.ps1
# migrate / operator
python manage.py migrate
python manage.py createoperator operator
# web / worker
python -m waitress --listen=127.0.0.1:8000 posinus_crawler.wsgi:application
python manage.py runworker
# verify
python manage.py check
python manage.py makemigrations --check --dry-run
python -m pytest
```

`tests/test_ui.py` reads `deploy/systemd/posinus-web.service` by relative path, so pytest must
run from this directory.

## Architecture

```text
Operator browser -> Django/Waitress UI ------------+
                                                    +-> local SQLite (WAL)
Single worker -> feeds/sitemaps/HTML/Playwright ---+
pipeline/ -> exchange_* views and tables ----------+

collector/models.py                 persistent domain model
collector/services/fetch.py         safe public-web acquisition and extraction
collector/services/ingest.py        normalization and duplicate grouping
collector/services/crawler.py       leases, schedules, crawl runs
collector/services/maintenance.py   source scoring, discovery, retention, backup
collector/services/translation.py   model-backed Russian translation via model-router-mcp
collector/management/commands/      worker/operator/maintenance entrypoints
templates/collector/                operator UI
posinus_crawler/                    Django project: settings, urls, wsgi
tests/                              unit and SQLite integration tests
deploy/ and scripts/                Ubuntu and Windows operation
```

The Django project package is `posinus_crawler`. The app label is `collector` and stays that
way: it is recorded in `django_migrations` and in content types.

## Service-specific rules

- Exactly one worker per database; the worker lock is a hard invariant.
- Crawl public HTTP(S) only, obey `robots.txt`, and never bypass login, paywall, CAPTCHA or
  private/reserved network boundaries.
- Schema, views, indexes, constraints and triggers change through Django migrations only.
- Retention deletes stored translations when it purges the original full text.
- Environment variables use the `POSINUS_` prefix. `settings.py` falls back to
  `data/posinus.sqlite3` when `POSINUS_DB_PATH` is unset, which is right for dev and a trap in
  prod, so `scripts/update-ubuntu.sh` asserts the production values before touching anything.

## Code Style

- Python 3.12+ idioms, PEP 8, type hints on public service boundaries, snake_case identifiers.
- Keep network, persistence and policy logic in `collector/services`; views and management
  commands stay thin.
- Timezone-aware UTC datetimes; short SQLite transactions with retry on lock contention.
- Deterministic fixture tests for parser behavior; real-site smoke tests stay optional.
