# Positive News Crawler — functional & technical specification

## Purpose

Collect multilingual articles from public mainstream and niche news sites into a local database. A separate asynchronous selector evaluates whether a logical news item is positive. Its append-only feedback drives source discovery and automatically pauses consistently low-yield sources. A single operator manages the system through a small authenticated website.

## Naming contract

- Product name: Positive News Crawler. It lives in the `crawler/` directory of the `posinus` repository.
- Django project package, deployment directory, and operating-system service account: `posinus`.
- Runtime environment variable prefix: `POSINUS_`.
- Runtime database, log, backup, service, and scheduled-task names use `posinus` or Positive News Crawler exclusively.

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
- ✅ On Ubuntu, store production SQLite state in `/var/lib/posinus`, shared by the local `posinus` group through a setgid directory, default ACLs, an explicit `0660` database mode, and `umask 0007`; every database client must run on the same host and belong to that group.
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
- ✅ Selection thresholds live here, in `exchange_selection_profile` (one profile is active) and `exchange_selection_bound` (`gate_min` / `gate_max` / `highlight_min`, values 0–10). Clients read them through the `exchange_active_selection_profile` view — the fifth readable object of the contract — and record the profile name and revision in `selector_version`. The rule is one for both readers: the pipeline decides by it, the operator UI explains decisions by it.

### Source policy

- ✅ Discover candidate domains only from external links of positively reviewed items, excluding a blocklist of social networks, messengers, video platforms, app stores, and link shorteners.
- ✅ Automatically accepted sources enter probation, limited to 20 saved articles.
- ✅ Promote after at least ten final reviews, at least 80% extraction success, and positive yield of at least 2%.
- ✅ Pause an active source below 2% yield after at least 50 final reviews in a rolling 30-day window.
- ✅ Ignore skipped/missing reviews and allow the operator to restart probation.

### Operator and operation

- ✅ Single local operator account; authenticated dashboard, source editor, news/duplicate view, crawl runs, events, source statistics, backup status.
- ✅ News list sorting by date or by source name (both directions) and filtering by source, review decision, and evaluation scores: all 20 characteristics are shown at once as dual-threshold 0–10 range sliders; every active range must match the latest evaluation of the news item (via `exchange_latest_evaluation_scores`), so any tightened range excludes news without scores.
- ✅ Both filters read the latest verdict only. The decision filter matches `exchange_latest_reviews`, never the raw event table, because a corrected decision is a new event and the old one must not keep matching. Score ranges match the evaluation of the configured evaluator (`POSINUS_MANUAL_SCORE_SELECTOR`) only, so the operator's snapshot copy of those scores is not counted as a second evaluation. Each news item appears at most once regardless of how many events or selectors match.
- ✅ The news list is paginated in pages of 50 and reports the real number of matching news items, not the size of the page. Paging links carry the active filters and sort order.
- ✅ News detail renders the latest evaluation of each selector as a heat scale: one cell per characteristic on an 11-step single-hue ramp (monotone lightness, per-step text contrast ≥ 4.5:1), grouped by category, value digits always visible, axis anchors in tooltips, plus a 0–10 legend; news without scores show a placeholder.
- ✅ News detail can request and persist a Russian title/body translation plus a short Russian summary through the local `model-router-mcp` `chat` tool. Router URL, token, provider, model, tier, and generation limits come from `POSINUS_*` settings; the default configured model hint is DeepSeek Chat and can be changed without editing code.
- ✅ An operator can send a news item to publication («Отправить в публикацию»). The append-only manual review event is idempotent per operator/news pair and snapshots the configured evaluator's latest characteristic scores in the same transaction; the event's news relation retains access to every occurrence URL for later weight fitting. The button states its real consequence: the pipeline picks the item up and posts it to all three platforms within about two hours, and there is no undo.
- ✅ The dashboard carries the stop cock: «Остановить публикации» for an hour, until the end of the day, or until lifted, with a reason. It writes the `pause` file into the shared mailbox (`POSINUS_PIPELINE_REQUESTS_DIR`), which the pipeline's publisher reads at the start of every run; the queue keeps growing and nothing is lost. Pausing and resuming are logged as operator events. The web never writes the pipeline's database. When the mailbox is missing — as on a development machine — the page says so and keeps working.
- ✅ News detail explains the verdict: one Russian sentence («Не прошла: позитивность 6 из 10, нужно от 8, и ни одна сильная сторона не дотянула…») followed by the table of conditions with the score, the bound, the outcome, and which strong side came closest. Computed from the thresholds in force, so it can never disagree with the pipeline's decision.
- ✅ A selection screen: the thresholds in force, an editable draft, and what both do to every scored news item — how many pass, which condition stops the most items, which news the draft adds or removes, and which missed by a single point. «Применить черновик» raises the profile revision and logs an operator event; «Пересчитать уже оценённые» drops a `run-evaluator-backfill` request into the shared mailbox. Axes that are scored but used by no condition are named on the screen.
- ✅ The dashboard shows «Машина»: the last run of every pipeline service with its counters, read from the pipeline's own database. The connection is read-only twice over (`mode=ro` plus `PRAGMA query_only`) with a two-second busy timeout, because a web page must never block the write that records a post that already went out. A run left unfinished for longer than twice its timer interval reads as «прервался», not as «идёт». A missing or unreadable database shows one line of explanation and the rest of the page still works — on a development machine that file does not exist at all.
- ✅ The daily backup copies the pipeline's database into the same rotation of seven. It holds the retellings, the captions and the links to published posts, and losing it would mean publishing everything again. A failure there is logged as an operator event and never fails the crawler's own backup.
- ✅ CLI commands for operator creation, worker, and maintenance.
- ✅ Windows/Ubuntu install and service files, structured rotating logs, CI matrix.
- ✅ Reproducible Ubuntu production layout, shared local SQLite group access, and guarded fast-forward update with backup/rollback.
- ✅ Production Nginx/HTTPS reverse proxy for `newscrawler.wildcar.org`, with Waitress restricted to loopback and Django honoring the proxy TLS scheme.
- ⏳ Real-source smoke validation is environment-specific follow-up work.

## Project structure

```text
collector/       Django domain, migrations, services, commands, views
posinus/     Django project configuration
templates/       Russian operator interface
tests/           parser, policy, database contract, UI and worker tests
docs/            selector contract and ADRs
examples/        direct-SQLite selector example
scripts/         Windows/Ubuntu installation and Windows task setup
deploy/systemd/  Ubuntu service units
```

## Deployment

- Store production configuration in `/etc/posinus/crawler.env`, application code in `/opt/posinus/crawler`, mutable database/backups/browser state in `/var/lib/posinus`, and logs in `/var/log/posinus`.
- Run services as the non-login `posinus` system user and group; grant other local database clients group membership rather than ownership of the application tree.
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
