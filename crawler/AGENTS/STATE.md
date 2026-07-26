# State

## Goal

Operate a single-host multilingual news crawler whose source list improves from asynchronous positive-news feedback.

## Now

- MVP code and repository harness are present in the development checkout and deployed to the Ubuntu production host.
- The crawler now lives in `crawler/` inside the merged `posinus` repository, next to `pipeline/`; the GitHub remote is `https://github.com/wildcar/posinus`.
- The posinus rename is LIVE on prod since 2026-07-25: one checkout at `/opt/posinus`, `/etc/posinus/crawler.env`, `/var/lib/posinus/posinus.sqlite3`, `POSINUS_*` variables, `posinus-web`/`posinus-worker` under user `posinus`. Verified after the migration: web and worker active, `/login/` 200, static 200, SQLite integrity ok. Pre-migration backup kept at `/var/lib/posinus/backups/pre-posinus-20260725T075954Z.sqlite3`.
- The migration missed one thing, since fixed both on the host and in the script: Nginx kept serving static from the old `/opt/newscrawler/staticfiles/`, so every CSS and JS request 404'd while pages returned 200.
- SQLite migrations, WAL pragmas, exchange views/triggers, daily backup, retention, source policy, UI, deployment files, and tests are implemented.
- `https://newscrawler.wildcar.org` is live behind Nginx with a Let's Encrypt certificate; Waitress remains restricted to `127.0.0.1:8000`.
- Production holds the initial RU/EN source set described in history; the crawler saves only articles published on the current UTC date.
- The exchange contract carries the News Evaluator axis set v1: 20 integer scores from 0 to 10, append-only review events and scores, and latest-score views.
- The news list sorts and filters by source, decision, and all evaluation axes. News detail shows the latest scores per selector as a heat scale.
- News detail now has a model-backed Russian translation action. It saves the translated title, full text, short summary, actual model identifier, and generation time. Router address, token, provider, model, tier, temperature, token limit, and timeout are environment settings; the default model matches the pipeline's `deepseek-v4-pro`.
- News detail now has an idempotent operator «Отправить в публикацию» action. It creates an append-only positive review and snapshots the configured evaluator's latest scores; occurrences retain source URLs for future weight fitting.
- Retention deletes stored translations when it purges the original full text after 90 days.
- Production runs commit `c83cc8a` since 2026-07-25 21:06 UTC (updated with `update-ubuntu.sh`);
  migration `0008_selection_profile` applied, web and worker active, HTTPS `/login/` 200.
  Pre-update backup: `/var/lib/posinus/backups/pre-update-20260725T210644Z.sqlite3`.
- Earlier: production ran commit `b885377` from 2026-07-25 20:07 UTC (updated with `update-ubuntu.sh`); migrations through `0007_latestreview` are applied, web/worker/model-router services are active, HTTPS `/login/` returns 200, SQLite integrity is `ok`. Pre-update backup: `/var/lib/posinus/backups/pre-update-20260725T195627Z.sqlite3`.
- The production crawler environment carries the router token as `POSINUS_ROUTER_AUTH_TOKEN`, plus `POSINUS_TRANSLATION_PROVIDER`/`POSINUS_TRANSLATION_MODEL` which the file never had before 2026-07-25; web was restarted and its loaded environment was verified.
- Production translation smoke test passed for news 5364, back when the model was `deepseek-chat`; the Russian body and summary were persisted. Not re-run since the switch to `deepseek-v4-pro`.
- A production failure on news 760 exposed invalid JSON from unescaped quotes in model prose. Marker-delimited translation sections with one correction retry are deployed; news 760 translated and persisted successfully after the update.
- Verified on Ubuntu/Python 3.12: Django checks clean, migrations match models, and all 47 tests pass.
- Agent-authored Russian text follows `.claude/skills/humanizer-ru/SKILL.md`; collected article content stays verbatim.
- Retention: `purge_rejected_content(days=3)` tombstones news with a `not_positive` verdict and no `positive` one (skipped/undecided/never-reviewed/selected are kept), wired into `maintenance`. Committed, not yet deployed. The external evaluator's backfill already ran on prod (latest reviews: 120 positive, 6108 not_positive), so the first prod run will tombstone ~4230 rejected items older than 3 days.

- The translation is a background job since 2026-07-26 (`TranslationJob` + a worker thread in the
  web process, on only with `POSINUS_JOB_WORKER=1`). No HTTP request waits for the model any more.
  LIVE on prod at commit `471574a`, migration `0010` applied, «Translation worker started» in the
  log, and verified end to end on news 7108: queued → running → done in 45 seconds with the
  Russian title saved. That is the request that used to die on the proxy timeout.
- The card can fix a retelling and pick the pictures since 2026-07-25 (requests through the
  mailbox, never a write to the pipeline DB), LIVE at commit `ad2eaac`. Pictures are served by
  Nginx now: `www-data` is in group `posinus`, `POSINUS_MEDIA_ACCEL_PREFIX=/_pipeline_media` is in
  the env, and a direct request to that location returns 404 because it is internal.
- The news card shows the pipeline half (retelling, gallery, per-platform posts) since
  2026-07-25, LIVE at commit `34e149c`. Pictures are streamed by Django for now: the internal
  `location /_pipeline_media/` is in the live Nginx config (backup `newscrawler.bak-media`), but
  `POSINUS_MEDIA_ACCEL_PREFIX` stays unset until the owner runs `usermod -aG posinus www-data`,
  because granting a system account access to prod data is the owner's to do.
- Prod runs commit `454a811` as of 2026-07-25 22:32 UTC; migrations `0008` and `0009` applied,
  all pipeline timers and the four mailbox `.path` units active.
- The dashboard's live half refreshes itself once a minute from `fragment/dashboard/` (401 on an
  expired session, 204 when nothing changed, no polling in a hidden tab).
- The publication queue goes out by «сила», not by preparation time, since 2026-07-25
  (`exchange_publication_plan` + the `exchange_publication_order` view, migration `0009`), with
  выше / ниже / отложить / снять on every row of «Эфир». LIVE on prod (commit `db2c1a9`): the
  view returns 6489 rows with strength from 0.0 to 9.3, and a publisher run through the mailbox
  read the plan without a single fallback warning.
- «Эфир» exists since 2026-07-25: queue with expected exit times, published feed with links,
  platform cards; and the dashboard shows «Сейчас», «Требует внимания» and the 30-day source mix.
  All of it reads the pipeline DB and degrades to one line when that DB is unavailable. The queue
  is still shown in the publisher's real order (preparation time) with «сила» beside it — the
  reordering itself needs the contract extension that is next.
- The web reads the pipeline database since 2026-07-25, read-only and non-blocking
  (`mode=ro` + `query_only`, 2s busy timeout), through `collector/services/pipeline_db.py`.
  The dashboard's «Машина» block shows the last run of every pipeline service from the new
  `service_run` table, and the daily backup now copies that database too (rotation of seven,
  `pipeline-*.sqlite3`). Everything degrades to one honest line when the file is missing, as it
  is on a development machine. LIVE on prod since 2026-07-25 21:44 UTC (commit `6835aae`):
  the web user reads `service_run` and a write attempt is refused by `query_only`. One thing had
  to be corrected there: `mode=ro` cannot open a WAL database whose `-shm` is absent, and it is
  absent between runs because the pipeline services are oneshots — the first prod query failed
  with «attempt to write a readonly database». The connection is `mode=rw` now, and the group
  has `0660` on the DB; the pragma is the guarantee, not the file mode.
- Selection thresholds live in the crawler DB since 2026-07-25 (migration `0008_selection_profile`):
  `exchange_selection_profile` + `exchange_selection_bound`, read by the pipeline through the
  `exchange_active_selection_profile` view. The owner's rule is seeded as `default` r1. The news
  card explains each verdict from those rows and the «Отбор» screen replays a draft over the whole
  scored corpus (counts, biggest blocker, added / removed / near-miss lists), applies it with a
  revision bump, and can request a full recompute. 76 tests pass. LIVE on prod since
  2026-07-25 21:06 UTC (commit `c83cc8a`, migration `0008` applied, `install.sh` rerun):
  the view returns `default|1|11 rows`, and a dry-run rescore over all 6469 scored items
  reported 0 corrections — the seeded thresholds reproduce every existing verdict exactly.
  The screen's own pivot query costs 0.2s on those 129380 score rows.
- The stop cock is in the UI since 2026-07-25: the dashboard writes a `pause` file into
  `POSINUS_PIPELINE_REQUESTS_DIR` (hour / end of day / until lifted, with a reason) and the
  publisher honours it. LIVE on prod since 2026-07-25 20:44 UTC (commit `2978f31`): the mailbox
  exists, the web user can write into it, and a test pause was honoured and then expired on its
  own. The web writes files only — never the pipeline database.
- Step 0 of `../docs/ui-concept.md` is done as of 2026-07-25: the news list pages by 50 with the
  real total and filter-preserving links (no more `[:200]` slice), Nginx read/send timeouts are
  120s so the synchronous translation call is not cut off at 60s, and the operator button reads
  «Отправить в публикацию» with a line about what it triggers. 54 tests pass. LIVE on prod
  (commit `5cc1966`, 2026-07-25 20:29 UTC); the live Nginx site `/etc/nginx/sites-available/newscrawler`
  was edited to 120s and reloaded, backup `newscrawler.bak-20260725`. Verified: web and worker
  active, HTTPS `/login/` 200, no migrations pending.
- Two news-list filter defects fixed on 2026-07-25, both found while reviewing the operator UI
  concept. The decision filter matched the raw event table, so a news item whose verdict was
  corrected (the pipeline's backfill did exactly that) matched both its old and its new
  decision; it now goes through the `exchange_latest_reviews` view, mapped by the new unmanaged
  `LatestReview` model, with a state-only migration `0007_latestreview`. Score ranges now
  filter on `POSINUS_MANUAL_SCORE_SELECTOR` so the operator snapshot is not counted as a second
  evaluation. 52 tests pass; the `make_review` fixture now defaults to `news-evaluator`.

## Next

1. Step 3 of `../docs/ui-concept.md`: the read-only connection to the pipeline DB (ACLs through `install.sh`, a helper that survives a missing file, a backup of that DB, a run table in all three scripts).
2. Deploy the rejected-news retention; expect the first maintenance run to tombstone ~4230 items.
3. Watch live translation errors; malformed model formatting now gets one automatic correction attempt.
4. Register every local SQLite client service in `/etc/posinus/update-services` and create the UI operator if still pending.
5. Watch crawl runs and positive-yield statistics; tune per-site rules where extraction fails.

## Open questions

- None.

## Deferred

- Remote/multi-host operation, multiple workers, server database, paid/search APIs, email/webhook notifications.
- Moving the positivity classifier into this repository is no longer deferred: it lives in `pipeline/` since the 2026-07-25 merge. It stays a separate service behind the exchange contract, not crawler code.
