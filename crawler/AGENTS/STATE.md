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
- News detail now has an idempotent operator «Отобрано» action. It creates an append-only positive review and snapshots the configured evaluator's latest scores; occurrences retain source URLs for future weight fitting.
- Retention deletes stored translations when it purges the original full text after 90 days.
- Production runs commit `3f50691` since 2026-07-25 19:56 UTC (updated with `update-ubuntu.sh`); migrations through `0007_latestreview` are applied, web/worker/model-router services are active, HTTPS `/login/` returns 200, SQLite integrity is `ok`. Pre-update backup: `/var/lib/posinus/backups/pre-update-20260725T195627Z.sqlite3`.
- The production crawler environment carries the router token as `POSINUS_ROUTER_AUTH_TOKEN`, plus `POSINUS_TRANSLATION_PROVIDER`/`POSINUS_TRANSLATION_MODEL` which the file never had before 2026-07-25; web was restarted and its loaded environment was verified.
- Production translation smoke test passed for news 5364, back when the model was `deepseek-chat`; the Russian body and summary were persisted. Not re-run since the switch to `deepseek-v4-pro`.
- A production failure on news 760 exposed invalid JSON from unescaped quotes in model prose. Marker-delimited translation sections with one correction retry are deployed; news 760 translated and persisted successfully after the update.
- Verified on Ubuntu/Python 3.12: Django checks clean, migrations match models, and all 47 tests pass.
- Agent-authored Russian text follows `.claude/skills/humanizer-ru/SKILL.md`; collected article content stays verbatim.
- Retention: `purge_rejected_content(days=3)` tombstones news with a `not_positive` verdict and no `positive` one (skipped/undecided/never-reviewed/selected are kept), wired into `maintenance`. Committed, not yet deployed. The external evaluator's backfill already ran on prod (latest reviews: 120 positive, 6108 not_positive), so the first prod run will tombstone ~4230 rejected items older than 3 days.

- Two news-list filter defects fixed on 2026-07-25, both found while reviewing the operator UI
  concept. The decision filter matched the raw event table, so a news item whose verdict was
  corrected (the pipeline's backfill did exactly that) matched both its old and its new
  decision; it now goes through the `exchange_latest_reviews` view, mapped by the new unmanaged
  `LatestReview` model, with a state-only migration `0007_latestreview`. Score ranges now
  filter on `POSINUS_MANUAL_SCORE_SELECTOR` so the operator snapshot is not counted as a second
  evaluation. 52 tests pass; the `make_review` fixture now defaults to `news-evaluator`.

## Next

1. Deploy the rejected-news retention; expect the first maintenance run to tombstone ~4230 items.
2. Watch live translation errors; malformed model formatting now gets one automatic correction attempt.
3. Register every local SQLite client service in `/etc/posinus/update-services` and create the UI operator if still pending.
4. Watch crawl runs and positive-yield statistics; tune per-site rules where extraction fails.

## Open questions

- None.

## Deferred

- Remote/multi-host operation, multiple workers, server database, paid/search APIs, email/webhook notifications.
- Moving the positivity classifier into this repository is no longer deferred: it lives in `pipeline/` since the 2026-07-25 merge. It stays a separate service behind the exchange contract, not crawler code.
