# History

Newest first. Each entry is at most five lines using the format defined in `AGENTS.md`.

## 2026-07-23 · Discovery blocklist for social/stores
- What: Added `BLOCKED_DISCOVERY_DOMAINS` + `is_blocked_discovery_domain`; `process_positive_discovery` now skips social networks, messengers, video, app stores, and link shorteners. +2 tests (49 total).
- Why: Auto-discovery kept creating dead probation sources (vk, ok, t.me, apps.apple.com, rustore, …) from share/app links in positive articles.
- Files: collector/services/maintenance.py, tests/test_maintenance.py, AGENTS/SPEC.md
- Next: Deploy; prod cleanup of existing blocked probation sources done separately.

## 2026-07-23 · Two constructive-journalism sources added
- What: Added Fix the News (`fixthenews.com/feed/`) and YES! Magazine (`yesmagazine.org/feed`) as active RSS sources on prod (verified 200/RSS/fresh/robots/same-domain links).
- Why: Operator request to add reputable constructive/solutions-journalism outlets.
- Files: production DB only (no code change).
- Next: Skipped BBC (no positive-only feed), CBS Uplift (video pages, thin text), Happy Broadcast (no RSS), SJN/VK/Reddit (not crawlable article feeds).

## 2026-07-23 · Retention for rejected news
- What: `purge_rejected_content(days=3)` tombstones news with a `not_positive` verdict and no `positive` one after 3 days (content blanked, row and append-only events kept), wired into the `maintenance` command; +1 test.
- Why: Evaluator now assigns verdicts; drop rejected news so only selected/undecided items linger (owner request).
- Files: collector/services/maintenance.py, collector/management/commands/maintenance.py, tests/test_maintenance.py, AGENTS/SPEC.md
- Next: Deploy; first prod run will tombstone ~4230 already-rejected items (backfill already ran, 120 positive / 6108 not_positive).

## 2026-07-21 · Robust translation deployed
- What: Deployed `e471193`, verified all services/HTTPS/SQLite, and successfully translated the previously failing news 760 with marked response sections.
- Why: Confirm the invalid-JSON failure is fixed against the same production article.
- Files: production host; backup `pre-update-20260721T150005Z.sqlite3`
- Next: Watch for provider failures; format errors now receive one automatic correction attempt.

## 2026-07-21 · Robust translation response format
- What: Replaced long free-text JSON replies with marked title/summary/body sections and one format-correction retry; added regression coverage for quotes and malformed first replies.
- Why: DeepSeek twice returned invalid JSON for news 760 because a quote inside translated prose was not escaped.
- Files: `collector/services/translation.py`, `tests/test_news_actions.py`, `AGENTS/*`
- Next: Deploy and retry production news 760.

## 2026-07-21 · Production translation smoke test
- What: Diagnosed HTTP 401: the protected env file had the correct router token, but the running web process predated the edit; restarted web, confirmed process tokens match, and translated news 5364 with `deepseek-chat`.
- Why: The live translation button failed until Waitress reloaded its environment.
- Files: production configuration and database only; no secret values recorded
- Next: Use the operator UI normally and watch model errors in the web journal.

## 2026-07-21 · Translation feature deployed
- What: Updated production to `a354bc9`, applied migration 0006, restarted web/worker, and verified HTTPS, SQLite integrity, translation table, and all three relevant services.
- Why: Make news translation and manual score-labelled selection available in the live operator UI.
- Files: production host; backup `pre-update-20260721T144230Z.sqlite3`
- Next: Add `NEWSCRAWLER_ROUTER_AUTH_TOKEN` to the protected production environment and smoke-test one translation.

## 2026-07-21 · Translation and manual selection on news detail
- What: Added persisted Russian translation and summary through configurable model-router-mcp, plus idempotent operator selection that snapshots the latest evaluator scores and retains links through the news relation; retention removes translated full text with the source article.
- Why: Operators need to read foreign news in Russian and collect labelled score vectors for later selection-weight fitting.
- Files: `collector/{models,views,services,migrations}`, `templates/collector/news_detail.html`, settings/env/deployment docs, tests, `AGENTS/*`
- Next: Deploy migration 0006 and set the router token/model in `/etc/newscrawler/newscrawler.env`.

## 2026-07-16 · Evaluation heat scale on the news detail page
- What: News detail now shows «Баллы по характеристикам» — the latest evaluation per selector as a category-grouped heat scale (11-step single-hue ramp with computed monotone lightness and ≥ 4.5:1 per-cell text contrast, 0-10 legend, anchor tooltips); shared test fixtures extracted to `tests/conftest.py`, 5 new tests, headless-Chromium render check.
- Why: Operators need to read the evaluator's detailed axis scores at a glance without querying SQLite.
- Files: `collector/views.py`, `templates/collector/news_detail.html`, `tests/conftest.py`, `tests/test_news_detail.py`, `tests/test_news_filters.py`, `README.md`, `AGENTS/*`
- Next: Deploy via `update-ubuntu.sh`; register SQLite client services and create the UI operator on production.

## 2026-07-15 · News list sorting and evaluation-score filters
- What: News list now sorts by date or source name and filters by source, selector decision, and all 20 evaluation axes shown at once as dual-threshold 0-10 sliders; added a read-only unmanaged model over `exchange_latest_evaluation_scores` (state-only migration 0005) and 13 tests; verified end-to-end in headless Chromium.
- Why: Operators need to slice collected news by the evaluator's scores, source, and date.
- Files: `collector/views.py`, `collector/models.py`, `collector/migrations/0005_latestevaluationscore.py`, `templates/collector/news_list.html`, `templates/base.html`, `tests/test_news_filters.py`, `README.md`, `AGENTS/*`
- Next: Deploy via `update-ubuntu.sh`; register SQLite client services and create the UI operator on production.

## 2026-07-14 · Evaluation contract applied on production
- What: Ran `update-ubuntu.sh` (f8739e7 → c67532c): migrations 0003–0004 applied, 20 characteristics seeded, scores table with triggers and the `exchange_latest_evaluation_scores` view live; services healthy, integrity `ok`.
- Why: The evaluator service needs the axis set and score storage in the production database, not only in the codebase.
- Files: production host only; pre-update backup `pre-update-20260714T221728Z.sqlite3`.
- Next: Evaluator side — thresholds, prompt, service skeleton; register the future evaluator unit in `/etc/newscrawler/update-services`.

## 2026-07-14 · Evaluation-score exchange contract
- What: Added the News Evaluator axis set v1 to the database — `exchange_evaluation_characteristics` reference table (20 rows seeded in migration 0004), append-only `exchange_evaluation_scores` (integer 0–10 per review event and axis, FK-validated keys, triggers), `exchange_latest_evaluation_scores` view — plus contract docs, README, and three tests.
- Why: The separate evaluator service (`~/repo/positive-news-evaluator`) needs the fixed characteristic set in the shared SQLite and a place to store per-news scores.
- Files: `collector/models.py`, `collector/migrations/0003–0004`, `docs/database-contract.md`, `README.md`, `tests/test_exchange.py`, `AGENTS/*`
- Next: Apply migrations on production via `update-ubuntu.sh`; evaluator side — thresholds, prompt, service skeleton.

## 2026-07-14 · Mandatory humanizer-ru skill
- What: Vendored the humanizer-ru editing skill v1.2.0 (upstream commit a8b6a4b, MIT) into `.claude/skills/humanizer-ru/` and made it mandatory in `AGENTS.md` for agent-authored Russian text; crawled content stays verbatim.
- Why: Operator requires all Russian prose deliverables cleaned of AI-generation markers and clerical style.
- Files: `.claude/skills/humanizer-ru/`, `AGENTS.md`, `AGENTS/STATE.md`
- Next: Apply the skill to every Russian deliverable in future iterations.

## 2026-07-13 · Current-date-only collection
- What: `crawl_source` saves only articles published on the current date (`published_today` gate: stale feed entries skipped before download, undated or older articles rejected at ingest); stale pre-existing rows purged from the production database.
- Why: Operator decision — the pipeline should hold only same-day news; 619 of 969 initially collected articles were older backfill.
- Files: `collector/services/crawler.py`, `tests/test_worker.py`, `AGENTS/SPEC.md`, `AGENTS/*`
- Next: Verify the next crawl cycle saves only current-date articles and watch per-source yields under the date filter.

## 2026-07-13 · Case-insensitive gzip response handling
- What: `fetch_url` now detects gzip bodies by magic bytes (`decompress_gzip_body`) instead of a case-sensitive `Content-Encoding` dict lookup; added a unit test.
- Why: ria.ru sends lowercase `content-encoding: gzip`, so bodies stayed compressed and HTML listings silently produced zero candidate links (run "success" with 0 articles).
- Files: `collector/services/fetch.py`, `tests/test_fetch.py`, `AGENTS/*`
- Next: Deploy to `/opt/newscrawler`, re-run the RIA source, and confirm articles are saved.

## 2026-07-13 · Initial production source list
- What: Added 20 verified sources (8 RU + 12 EN positive-news sites: 19 RSS feeds plus the AP Oddities HTML listing, each checked for HTTP 200, feed validity, freshness, robots.txt) to the production database and repaired the RIA source (removed 404 sitemap endpoint, added `ria\.ru/\d{8}/` include pattern).
- Why: The deployed crawler had an empty working source list; candidates came from `~/repo/hermes/positive-news/registry.md` usage counts and a web search for dedicated positive-news outlets.
- Files: production SQLite only (no code changes); rejected candidates recorded in `AGENTS/STATE.md`.
- Next: Watch first crawl runs and positive-yield statistics; tune per-site rules and probation/pauses as feedback arrives.

## 2026-07-13 · Nginx HTTPS reverse-proxy support
- What: Added the Nginx site, loopback-only forwarded-scheme trust in Waitress/Django, deployment procedure, and regression assertions for `newscrawler.wildcar.org`.
- Why: Publish the operator UI through HTTPS while keeping Waitress bound only to loopback.
- Files: `deploy/nginx/`, `newscrawler/settings.py`, `docs/ubuntu-deployment.md`, `tests/test_ui.py`, `AGENTS/*`
- Next: Create the UI operator, add initial sources, and run selected live-source smoke tests.

## 2026-07-13 · Shared SQLite mode normalization
- What: Added explicit `0660` initialization and systemd pre-start normalization for the production SQLite database.
- Why: SQLite creates a new database with default `0644` permissions, which became `0640` and blocked other group members from writing.
- Files: `deploy/systemd/`, `docs/ubuntu-deployment.md`, `docs/database-contract.md`, `AGENTS/*`
- Next: Apply `chmod 0660` on the target database and continue deployment verification.

## 2026-07-13 · Ubuntu 24.04 and Python 3.12 support
- What: Extended the supported Python range to 3.12, added it to the CI matrix, and retargeted the Ubuntu guide to the stock 24.04 runtime.
- Why: Deploy cleanly on the destination Ubuntu 24.04 LTS host without third-party Python packages.
- Files: `pyproject.toml`, `.github/workflows/ci.yml`, `docs/ubuntu-deployment.md`, `README.md`, `AGENTS/*`
- Next: Run the documented deployment on the destination host and verify the live systemd services.

## 2026-07-13 · Ubuntu production deployment and updater
- What: Added the production filesystem/user model, shared SQLite group permissions, hardened systemd units, full deployment guide, and guarded update script with backup/rollback.
- Why: Support sudo-driven installation and safe same-host database access by multiple local service accounts.
- Files: `docs/ubuntu-deployment.md`, `scripts/update-ubuntu.sh`, `deploy/`, `README.md`, `AGENTS/*`
- Next: Deploy on the target host, register selector units, configure HTTPS, and run live source smoke tests.

## 2026-07-13 · Unified crawler naming
- What: Renamed the Django package and every runtime, deployment, database, log, environment, UI, and package identifier to the crawler naming contract.
- Why: Remove conflicting product terminology and standardize the internal folder and service account as `newscrawler`.
- Files: `newscrawler/`, `deploy/systemd/`, `collector/services/`, `.env.example`, `README.md`, `AGENTS/*`
- Next: Configure the runtime environment and seed initial production sources.

## 2026-07-13 · GitHub repository initialized
- What: Initialized Git on `main`, configured the GitHub remote, and published the project repository.
- Why: Establish version control and the shared upstream repository.
- Files: repository metadata, `AGENTS/STATE.md`, `AGENTS/HISTORY.md`
- Next: Configure the runtime environment and seed initial production sources.

## 2026-07-13 · SQLite crawler MVP and repository relocation
- What: Implemented the crawler, UI, SQLite exchange contract, policy automation, deployment assets, tests, and populated the repository harness.
- Why: Deliver the approved single-host positive-news collection plan in its permanent repository location.
- Files: `collector/`, Django project package, `tests/`, `README.md`, `AGENTS/*`, `docs/`
- Next: Configure environment/operator, seed real sources, and run live smoke tests.

---
