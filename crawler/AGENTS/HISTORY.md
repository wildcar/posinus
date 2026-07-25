# History

Newest first. Each entry is at most five lines using the format defined in `AGENTS.md`.

## 2026-07-25 · «Эфир» and a dashboard that answers questions
- What: New «Эфир» screen (`broadcast.py` + three tabs): the queue in the order the publisher will really take it, with the expected time of exit derived from the interval and window the publisher recorded on its last run, the age of each item and its «сила» from the scores; the published feed with a live link per platform; a card per platform with successes, failures and the last error. The dashboard gained «Сейчас» (today's numbers, each a link, each with «обычно» from the last two weeks), «Требует внимания» (broken platform, looping preparation, paused source — «Всё в порядке» when empty) and «Состав ленты за 30 дней» by source. 11 tests added, 97 pass.
- Why: step 4 of `../docs/ui-concept.md`. «Что вышло вчера» took three clicks and no answer, and the 124-item queue was invisible from the UI entirely. Reading the publisher's own recorded config instead of keeping a second copy of the interval means the times on screen cannot drift from the ones it uses.
- Files: crawler/collector/services/{broadcast,console}.py, crawler/collector/{views,urls}.py, crawler/templates/collector/{broadcast,dashboard,base}.html, crawler/tests/test_broadcast.py, crawler/AGENTS/SPEC.md
- Next: queue ordering by «сила» with operator overrides (a contract extension), then Telegram notifications and the minute refresh.

## 2026-07-25 · The web can read the pipeline database, safely
- What: `pipeline_db.py` opens the pipeline's SQLite read-only twice over (`mode=ro` + `PRAGMA query_only`) with a 2s busy timeout and turns every failure into `PipelineUnavailable`; `pipeline_status.py` turns the new `service_run` rows into the dashboard's «Машина» block, calling a run left open past twice its timer interval «прервался». The daily backup now copies that database into the same rotation of seven. `install.sh` grants group `posinus` read on the DB, its sidecars and media, with setgid and default ACLs. 10 tests added, 86 pass. Pipeline side in the same commit.
- Why: step 3 of `../docs/ui-concept.md`. Half of the machine lives in a database this process does not own, and the dangerous part is not reading it — it is holding a lock while the publisher records a post that already went out, which is how you get a duplicate in the channel.
- Files: crawler/collector/services/{pipeline_db,pipeline_status,maintenance}.py, crawler/collector/views.py, crawler/templates/collector/dashboard.html, crawler/posinus_crawler/settings.py, crawler/deploy/crawler.env.example, crawler/tests/test_pipeline_db.py, crawler/AGENTS/SPEC.md, ../docs/deployment.md
- Next: prod deploy, then step 4 — the pulse screen and the queue («Эфир»).

## 2026-07-25 · Selection thresholds move into the database; a screen to calibrate them
- What: New `exchange_selection_profile` / `exchange_selection_bound` tables plus the `exchange_active_selection_profile` view (migration `0008_selection_profile`, which also seeds the owner's rule as `default` r1). `collector/services/selection.py` reads the same rows and explains a verdict; `calibration.py` replays a profile over every scored item. News detail now says why an item passed or failed; the new «Отбор» screen compares a draft with the rule in force, names what cuts the most, lists what a draft adds, removes or nearly passed, and has «Применить черновик» (revision +1, operator event) and «Пересчитать уже оценённые». 14 tests added, 76 pass. Pipeline side in the same commit.
- Why: step 2 of `../docs/ui-concept.md`. The rule lived in `pipeline/evaluator.py`, so the UI would have had to explain decisions from a second copy — and two copies of one rule drift apart within a month. Calibration was also the number one open task in `pipeline/AGENTS/STATE.md`.
- Files: crawler/collector/{models,views,urls}.py, crawler/collector/migrations/0008_selection_profile.py, crawler/collector/services/{selection,calibration,pipeline_mailbox}.py, crawler/templates/collector/{selection,news_detail,base}.html, crawler/tests/test_selection.py, crawler/AGENTS/SPEC.md, ../docs/contracts/database-contract.md
- Next: prod deploy (`update-ubuntu.sh` for the migration, `install.sh` for the backfill unit); then step 3, the pipeline DB read-only connection.

## 2026-07-25 · The stop cock reaches the operator: a button on the dashboard
- What: New `collector/services/pipeline_mailbox.py` writes the shared request files (`pause`, `run-<service>`) atomically into `POSINUS_PIPELINE_REQUESTS_DIR` and reads the current pause; the dashboard shows the state and carries «Остановить публикации» with a duration (hour / end of day / until lifted) and a reason, plus «Возобновить». Both are logged as operator events. A missing or unwritable mailbox is reported in words instead of a 500. 8 tests added, 62 pass. Pipeline side in the same commit.
- Why: step 1 of `../docs/ui-concept.md` — the operator needs one button that holds every publication when the world outside the news changes, and it has to work from a phone. A file is the whole protocol: the web must never write the pipeline's database.
- Files: crawler/collector/{services/pipeline_mailbox.py,views.py,urls.py}, crawler/templates/collector/dashboard.html, crawler/posinus_crawler/settings.py, crawler/deploy/crawler.env.example, crawler/tests/test_publication_pause.py, crawler/AGENTS/SPEC.md
- Next: owner runs `pipeline/deploy/install.sh` to create the mailbox on prod; until then the dashboard shows «нет связи с конвейером».

## 2026-07-25 · Step 0 of the UI concept finished: pages of 50, honest count, honest button
- What: `news_list` pages through a `Paginator` (50 per page) instead of slicing at 200; the page shows the real total, the range on screen and links that carry the filters. Nginx `proxy_read_timeout`/`proxy_send_timeout` go to 120s. The «Отобрано» button is now «Отправить в публикацию» with a line saying what it does: Telegram, wildcar.ru and VK in about two hours, no undo. Three tests added (54 pass).
- Why: the old slice hid both the rest of the corpus and its size; 60s cut off the synchronous translation call with a 504 while the model kept working; and the button's label said nothing about the post it sets in motion.
- Files: crawler/collector/views.py, crawler/templates/collector/{news_list,news_detail}.html, crawler/deploy/nginx/posinus.conf, crawler/tests/{test_news_filters,test_news_actions}.py, crawler/AGENTS/SPEC.md
- Next: step 2 of `docs/ui-concept.md` — selection thresholds into the crawler DB (two tables plus a view for the pipeline), then the selection screen.

## 2026-07-25 · News list filters read the latest verdict, not every event
- What: The decision filter now matches the `exchange_latest_reviews` view through a new unmanaged `LatestReview` model (state-only migration `0007_latestreview`) via `Exists`, and score ranges filter on `POSINUS_MANUAL_SCORE_SELECTOR`. Three tests added; the `make_review` fixture defaults to `news-evaluator`, which two tests asserted on. 52 pass.
- Why: The contract corrects a verdict by appending an event, so filtering the raw table matched superseded decisions (a news item with `skipped` then `not_positive` showed up under both) and the join duplicated rows. The manual review snapshots the evaluator's scores under `operator:*`, so axis filters were matching one evaluation twice. Found while reviewing the operator UI concept.
- Files: crawler/collector/{models,views}.py, crawler/collector/migrations/0007_latestreview.py, crawler/tests/{conftest,test_news_filters,test_news_actions,test_news_detail}.py, crawler/AGENTS/SPEC.md
- Next: remaining step 0 of docs/ui-concept.md - Paginator with an honest count, `proxy_read_timeout 120s`, renaming the «Отобрано» button.

## 2026-07-25 · Prod migrated to posinus; Nginx static path fixed
- What: Ran the posinus migration on the host: one checkout at `/opt/posinus`, `/etc/posinus/crawler.env`, `/var/lib/posinus/posinus.sqlite3`, `POSINUS_*`, `posinus-web`/`posinus-worker` under user `posinus`. Found and fixed a gap the script had missed — Nginx still aliased `/static/` to `/opt/newscrawler/staticfiles/`, so pages returned 200 while every CSS and JS file 404'd; the script now repoints and reloads Nginx itself. Removed the `.pre-posinus` rollback trees and both old local clones.
- Why: The split repositories and the newscrawler naming were retired; a half-renamed host is worse than either end state.
- Files: crawler/deploy/migrate-to-posinus.sh, crawler/AGENTS/STATE.md
- Next: Deploy the rejected-news retention.

## 2026-07-25 · Merged into the posinus monorepo; prod rename prepared
- What: This repository is now `posinus`: the crawler moved to `crawler/`, positive-news-evaluator came in as `pipeline/` with its history (git subtree), shared docs moved to the root (`AGENTS.md`, `AGENTS/{MEMORY,ENV}.md`, `docs/contracts/`, `docs/deployment.md`), and paths/units/accounts/env prefix were renamed to the posinus scheme (`posinus_crawler` package, `POSINUS_*`, `/opt/posinus/crawler`, `posinus-web`/`posinus-worker`, user `posinus`). `update-ubuntu.sh` now separates REPO_DIR from APP_DIR and `deploy/migrate-to-posinus.sh` migrates the host.
- Why: The exchange contract is owned by crawler migrations and consumed by the pipeline, so axis changes needed two commits across two repos with no atomicity; an agent working in one repo also had no memory of the other.
- Files: whole tree; crawler/scripts/update-ubuntu.sh, crawler/deploy/migrate-to-posinus.sh, ../AGENTS.md, ../docs/
- Next: Owner runs `migrate-to-posinus.sh --apply` on prod; nothing else can deploy until then.

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
