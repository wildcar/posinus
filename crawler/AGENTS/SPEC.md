# Positive News Crawler — functional & technical specification

## Purpose

Collect multilingual articles from public mainstream and niche news sites into a local database. A separate asynchronous selector evaluates whether a logical news item is positive. Its append-only feedback drives source discovery and automatically pauses consistently low-yield sources. A single operator manages the system through a small authenticated website.

## Naming contract

- Product and repository name: `positive-news-crawler` / Positive News Crawler.
- Django project package, deployment directory, and operating-system service account: `newscrawler`.
- Runtime environment variable prefix: `NEWSCRAWLER_`.
- Runtime database, log, backup, service, and scheduled-task names use `newscrawler` or Positive News Crawler exclusively.

## Stack

- Python 3.12, 3.13, or 3.14; Django 5.2 LTS and server-rendered templates.
- SQLite in WAL mode on a local disk; one web process, one crawler worker, and same-host client processes using the exchange contract.
- Feedparser for RSS/Atom, XML sitemap parsing, Trafilatura for main-text extraction, Playwright Chromium only for configured JS sites.
- Waitress for the web process; systemd on Ubuntu and Task Scheduler on Windows.

## Architecture

```text
Public sites -> crawler worker -> normalization/deduplication -> SQLite WAL
                                          |                      ^
Operator -> authenticated Django UI ------+                      |
External selector <- exchange views -> append-only review events-+
```

## Functional requirements

### Collection

- ✅ Poll due sources every minute; default source interval is 60 minutes.
- ✅ Source acquisition cascade: RSS/Atom, sitemap (including gzip/index), HTML listing, opt-in Playwright.
- ✅ Preserve feed ETag and Last-Modified values.
- ✅ Obey robots, identify the user agent, apply delays/timeouts/backoff, reject private/reserved addresses and protected paths.
- ✅ Extract title, text, author, date, language, canonical URL, metadata, and outbound links.
- ✅ Save only articles published on the current date in the active time zone (`TIME_ZONE`, UTC in production); reject undated articles and skip stale feed entries before download.
- ✅ Allow per-source URL regexes, CSS selectors, delay, interval, and Playwright setting.

### Storage and duplicate handling

- ✅ Configure WAL, foreign keys, 30-second busy timeout, and normal synchronization on Django connections.
- ✅ Allow one worker via OS file lock; lease due sources and recover expired leases.
- ✅ On Ubuntu, store production SQLite state in `/var/lib/newscrawler`, shared by the local `newscrawler` group through a setgid directory, default ACLs, an explicit `0660` database mode, and `umask 0007`; every database client must run on the same host and belong to that group.
- ✅ Group exact normalized-body SHA-256 duplicates.
- ✅ Group near duplicates of the same language within 48 hours using SimHash and title similarity; translations remain separate.
- ✅ Retain every occurrence/source URL while exposing one logical item to the selector.
- ✅ Purge full content and detailed metadata after 90 days while retaining the technical tombstone.
- ✅ Purge content of rejected news (has a `not_positive` verdict and no `positive` one from any selector) after 3 days, keeping the tombstone and the append-only review events; `skipped`, never-reviewed, and selected news are kept.
- ✅ Create integrity-checked SQLite backups and retain seven files.

### Feedback contract

- ✅ `exchange_news_for_selection` exposes active logical news and all occurrences as JSON.
- ✅ `exchange_review_events` accepts positive/not_positive/skipped events with selector/version/idempotency metadata.
- ✅ `exchange_latest_reviews` returns the latest event for a news/selector pair.
- ✅ Unique idempotency constraint and triggers enforce append-only events.
- ✅ `exchange_evaluation_characteristics` stores the fixed evaluation-axis set (v1: 20 rows seeded by migration, mirroring the News Evaluator spec) with key, Russian title, category, description, 0/10 scale anchors, threshold direction, and position.
- ✅ `exchange_evaluation_scores` accepts one integer 0–10 score per review event and characteristic key; a foreign key validates keys, uniqueness covers (event, characteristic), and triggers enforce append-only rows — corrections are a new event with a full score set.
- ✅ `exchange_latest_evaluation_scores` returns the scores attached to the latest review event for a news/selector pair.

### Source policy

- ✅ Discover candidate domains only from external links of positively reviewed items, excluding a blocklist of social networks, messengers, video platforms, app stores, and link shorteners.
- ✅ Automatically accepted sources enter probation, limited to 20 saved articles.
- ✅ Promote after at least ten final reviews, at least 80% extraction success, and positive yield of at least 2%.
- ✅ Pause an active source below 2% yield after at least 50 final reviews in a rolling 30-day window.
- ✅ Ignore skipped/missing reviews and allow the operator to restart probation.

### Operator and operation

- ✅ Single local operator account; authenticated dashboard, source editor, news/duplicate view, crawl runs, events, source statistics, backup status.
- ✅ News list sorting by date or by source name (both directions) and filtering by source, review decision, and evaluation scores: all 20 characteristics are shown at once as dual-threshold 0–10 range sliders; every active range must match the latest evaluation of the news item (via `exchange_latest_evaluation_scores`), so any tightened range excludes news without scores.
- ✅ News detail renders the latest evaluation of each selector as a heat scale: one cell per characteristic on an 11-step single-hue ramp (monotone lightness, per-step text contrast ≥ 4.5:1), grouped by category, value digits always visible, axis anchors in tooltips, plus a 0–10 legend; news without scores show a placeholder.
- ✅ News detail can request and persist a Russian title/body translation plus a short Russian summary through the local `model-router-mcp` `chat` tool. Router URL, token, provider, model, tier, and generation limits come from `NEWSCRAWLER_*` settings; the default configured model hint is DeepSeek Chat and can be changed without editing code.
- ✅ An operator can mark a news item as selected. The append-only manual review event is idempotent per operator/news pair and snapshots the configured evaluator's latest characteristic scores in the same transaction; the event's news relation retains access to every occurrence URL for later weight fitting.
- ✅ CLI commands for operator creation, worker, and maintenance.
- ✅ Windows/Ubuntu install and service files, structured rotating logs, CI matrix.
- ✅ Reproducible Ubuntu production layout, shared local SQLite group access, and guarded fast-forward update with backup/rollback.
- ✅ Production Nginx/HTTPS reverse proxy for `newscrawler.wildcar.org`, with Waitress restricted to loopback and Django honoring the proxy TLS scheme.
- ⏳ Real-source smoke validation is environment-specific follow-up work.

## Project structure

```text
collector/       Django domain, migrations, services, commands, views
newscrawler/     Django project configuration
templates/       Russian operator interface
tests/           parser, policy, database contract, UI and worker tests
docs/            selector contract and ADRs
examples/        direct-SQLite selector example
scripts/         Windows/Ubuntu installation and Windows task setup
deploy/systemd/  Ubuntu service units
```

## Deployment

- Store production configuration in `/etc/newscrawler/newscrawler.env`, application code in `/opt/newscrawler`, mutable database/backups/browser state in `/var/lib/newscrawler`, and logs in `/var/log/newscrawler`.
- Run services as the non-login `newscrawler` system user and group; grant other local database clients group membership rather than ownership of the application tree.
- Run migrations, create the operator, install Chromium, then start Waitress and exactly one worker.
- Publish the operator UI only through the HTTPS reverse proxy; keep Waitress on `127.0.0.1:8000` and serve collected static files directly from Nginx.
- Keep the database, backup directory, logs, worker, UI, and every direct SQLite client on the same local filesystem and machine.
- Stop all registered database clients before updates; take and verify a SQLite backup before migrations.
- Detailed procedures are in `docs/ubuntu-deployment.md`, `README.md`, and `AGENTS/ENV.md`.

## Current state

- ✅ MVP implemented, migrated, and verified on Windows/Python 3.14.5.
- ✅ Twenty-three deterministic tests pass; SQLite integrity and exchange objects are verified; CI covers Python 3.12, 3.13, and 3.14 on Ubuntu and Windows.
- ✅ Initial production source list (20 sources: RIA good-news section plus 19 verified RU/EN sources) loaded on the destination host on 2026-07-13.
- ⏳ Configure real operator credentials on the destination host.

See `AGENTS/STATE.md` for the live snapshot.

## Data sources & dependencies

- Public HTTP(S) RSS/Atom feeds, sitemaps, and article/listing HTML.
- No search API, paid publisher account, CAPTCHA bypass, remote DB, Redis, or Celery.
- External selector is a separate process that shares the same local SQLite file.
