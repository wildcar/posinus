# State

## Goal

Operate a single-host multilingual news crawler whose source list improves from asynchronous positive-news feedback.

## Now

- MVP code and repository harness are present in the development checkout and deployed to the Ubuntu production host.
- The crawler now lives in `crawler/` inside the merged `posinus` repository, next to `pipeline/`; the GitHub remote is `https://github.com/wildcar/posinus`.
- **The posinus rename is in the repository but NOT yet on prod.** The live host still runs `/opt/newscrawler`, `/etc/newscrawler/newscrawler.env`, `/var/lib/newscrawler/newscrawler.sqlite3`, `NEWSCRAWLER_*` variables and the `newscrawler-web`/`newscrawler-worker` units. `deploy/migrate-to-posinus.sh` performs the switch and the owner must run it; until then, do not run `scripts/update-ubuntu.sh` on prod — its path assertions will fail by design.
- SQLite migrations, WAL pragmas, exchange views/triggers, daily backup, retention, source policy, UI, deployment files, and tests are implemented.
- `https://newscrawler.wildcar.org` is live behind Nginx with a Let's Encrypt certificate; Waitress remains restricted to `127.0.0.1:8000`.
- Production holds the initial RU/EN source set described in history; the crawler saves only articles published on the current UTC date.
- The exchange contract carries the News Evaluator axis set v1: 20 integer scores from 0 to 10, append-only review events and scores, and latest-score views.
- The news list sorts and filters by source, decision, and all evaluation axes. News detail shows the latest scores per selector as a heat scale.
- News detail now has a model-backed Russian translation action. It saves the translated title, full text, short summary, actual model identifier, and generation time. Router address, token, provider, model, tier, temperature, token limit, and timeout are environment settings; the default model hint matches the evaluator's `deepseek-chat`.
- News detail now has an idempotent operator «Отобрано» action. It creates an append-only positive review and snapshots the configured evaluator's latest scores; occurrences retain source URLs for future weight fitting.
- Retention deletes stored translations when it purges the original full text after 90 days.
- Production runs commit `e471193`; migration `0006_newstranslation` is applied, `news_translations` exists, web/worker/model-router services are active, HTTPS returns 200, and SQLite integrity is `ok`.
- The production crawler environment contains the router token (still under its old `NEWSCRAWLER_ROUTER_AUTH_TOKEN` name until the prod rename runs); web was restarted and its loaded token matches the router process without exposing either value.
- Production translation smoke test passed for news 5364 with `deepseek-chat`; the Russian body and summary were persisted.
- A production failure on news 760 exposed invalid JSON from unescaped quotes in model prose. Marker-delimited translation sections with one correction retry are deployed; news 760 translated and persisted successfully after the update.
- Verified on Ubuntu/Python 3.12: Django checks clean, migrations match models, and all 47 tests pass.
- Agent-authored Russian text follows `.claude/skills/humanizer-ru/SKILL.md`; collected article content stays verbatim.
- Retention: `purge_rejected_content(days=3)` tombstones news with a `not_positive` verdict and no `positive` one (skipped/undecided/never-reviewed/selected are kept), wired into `maintenance`. Committed, not yet deployed. The external evaluator's backfill already ran on prod (latest reviews: 120 positive, 6108 not_positive), so the first prod run will tombstone ~4230 rejected items older than 3 days.

## Next

1. Owner: run `sudo bash /opt/posinus/crawler/deploy/migrate-to-posinus.sh` (after a `--dry-run`) to move prod onto the posinus names. It stops the services, renames the group and users, moves `/opt`, `/etc`, `/var/lib`, `/var/log`, rewrites the env files and installs the new units. Everything below waits on it, because the update script now asserts posinus paths.
2. Deploy the rejected-news retention; expect the first maintenance run to tombstone ~4230 items.
3. Watch live translation errors; malformed model formatting now gets one automatic correction attempt.
4. Register every local SQLite client service in `/etc/posinus/update-services` and create the UI operator if still pending.
5. Watch crawl runs and positive-yield statistics; tune per-site rules where extraction fails.

## Open questions

- None.

## Deferred

- Remote/multi-host operation, multiple workers, server database, paid/search APIs, email/webhook notifications.
- Moving the positivity classifier into this repository is no longer deferred: it lives in `pipeline/` since the 2026-07-25 merge. It stays a separate service behind the exchange contract, not crawler code.
