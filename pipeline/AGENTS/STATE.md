# State

Current snapshot. Overwrite this file each iteration. Aim for ≤50 lines: keep pointers and
the live picture here; push detail into `AGENTS/SPEC.md` (the contract) and `AGENTS/HISTORY.md`
(the log).

## Goal

A service that scores every news item collected by the crawler (`../crawler`) on the fixed
v1 characteristic set (20 axes, integer 0–10), selects the strong ones, prepares them into
a publish-ready retelling, and posts them to the platforms.

## Now

- Since 2026-07-25 this is the `pipeline/` directory of the `posinus` repository, next to
  `crawler/`. Prod names in the repo are `posinus-{evaluator,preparer,publisher}` under
  `/opt/posinus/pipeline`, but **prod itself has not been migrated yet**: the live host still
  runs `news-{evaluator,preparer,publisher}` from `/opt/news-evaluator`. See Next.

- `evaluator.py` scores news on 20 axes via model-router-mcp and, with the `default`
  profile, writes `positive`/`not_positive` plus scores in one transaction. LIVE on prod
  (every 10 min); backfill done. ~120 positive of ~6200.
- `preparer.py` turns selected news into a **markdown** retelling (H1 title, paragraphs,
  `Источник: [имя](url)`) plus downloaded illustrations, in the evaluator-owned SQLite +
  media dir. Canonical form is `prepared_item.retold_body_md`; no HTML page anymore. LIVE
  on prod (every 15 min).
- `publisher.py` posts prepared news, fully automatically by timer, to Telegram @posinus,
  wildcar.ru (Эгея) and the VK community wall @positivenus; each platform enables only when
  its secret is set. Renders each format from the stored markdown; idempotent per
  `(news_id, platform)`. Paces NEW posts to at most one per `PUB_MIN_INTERVAL_MINUTES`
  (default 120); a failing platform is retried up to `PUB_MAX_ATTEMPTS` (8), then given up
  on so it can't block the queue. LIVE on prod: Telegram + wildcar.ru + VK all working.
- VK works end-to-end now: a classic `vk1.` USER token of a group admin (obtained via the
  grandfathered Kate Mobile app_id) sits in the env with `VK_GROUP_ID=233237778`; post
  wall-233237778_2 landed through the pipeline. `vk_call` targets `api.vk.ru` in the repo.

## Next

1. Owner: run the prod migration, in this order. It replaces the old redeploy step.
   `sudo bash /opt/posinus/crawler/deploy/migrate-to-posinus.sh` (dry run first, then
   `--apply`), then `sudo bash /opt/posinus/pipeline/deploy/install.sh` to recreate the three
   timers under their new names. The migration also lands the code prod is still missing:
   `api.vk.com` → `api.vk.ru`, the 2h pacing, the give-up rule, the markdown retelling.
2. After the migration, post one item with `--news-id` to confirm the VK photo upload path
   works against api.vk.ru; easy to revert to api.vk.com if it does not.
3. Prompt calibration and soft profiles («Россия» / «Международное»).

## Open questions

- Long-term model choice; deepseek-chat is only the test model (swap via env file).

## Resolved

- VK live: publisher posts to @positivenus with a classic user admin token; the community
  (214/27) and VK ID `vk2.a`/1051 dead ends are documented in `docs/services.md`
  (owner, 2026-07-24).
- Publish at most one NEW news per 2h (`PUB_MIN_INTERVAL_MINUTES=120`); a failing platform
  is given up after `PUB_MAX_ATTEMPTS` so it can't block the queue (owner, 2026-07-23).
- Prepared retelling is stored as markdown, not HTML: no platform consumes HTML, and it
  kills the HTML→paragraph round-trip in the publisher (owner, 2026-07-23).
- Publishing is full-auto (no approval gate); platforms Telegram + wildcar.ru + VK; MAX
  dropped (owner cannot create a MAX bot) (owner, 2026-07-23).
- Strict `default` rule is intended; retelling generated fresh (owner, 2026-07-23).

## Deferred

- —
