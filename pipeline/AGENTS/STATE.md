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
  `crawler/`, and prod is migrated: the `posinus-{evaluator,preparer,publisher}` timers run
  the scripts in place from `/opt/posinus/pipeline`, config in `/etc/posinus/pipeline.env`,
  state in `/var/lib/posinus/pipeline`, user `posinus-pipeline`. A crawler update ships
  pipeline code too, so `install.sh` is only needed for unit or config changes.
- The model is `deepseek-v4-pro`: DeepSeek retired `deepseek-chat`, which had every run
  failing with HTTP 400 until 2026-07-25. It also needed a bigger token budget — v4-pro
  spends ~950 completion tokens on one evaluation, and against the old 1000 cap the provider
  returned empty content. `EVALUATOR_MAX_TOKENS` and `PREPARER_MAX_TOKENS` default to 4000.
- End-to-end verified on prod after the switch: evaluator 6 evaluated / 0 failed, preparer
  5 prepared / 0 failed, publisher 5 published with telegram, site and vk all `ok`. VK now
  lands on `vk.ru/wall-233237778_*`, so the api.vk.ru switch works.
- `evaluator.py` scores news on 20 axes via model-router-mcp and, with the `default`
  profile, writes `positive`/`not_positive` plus scores in one transaction. LIVE on prod
  (every 10 min); backfill done. ~120 positive of ~6200.
- `preparer.py` turns selected news into a **markdown** retelling (H1 title, paragraphs,
  `Источник: [имя](url)`) plus downloaded illustrations, in the pipeline-owned SQLite +
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
  wall-233237778_2 landed through the pipeline. `vk_call` targets `api.vk.ru`, live on prod since 2026-07-25.

- Since 2026-07-25 the pipeline-owned DB opens in WAL with a 30s busy timeout, and
  `record_publication` retries a locked write, leaving an `unrecorded-*` marker and failing
  the run when it cannot record a post that already went out. Found while reviewing the
  operator UI concept: the DB was in rollback-journal mode, so the first reader could have
  blocked that write and caused duplicate posts on the next run. 80 tests pass. Not yet on
  prod: it ships with the next crawler update, no `install.sh` needed.

## Next

1. Prompt calibration and soft profiles («Россия» / «Международное»). The `default` profile
   selected 0 of 6 in the first clean v4-pro batch, so calibration should account for how the
   new model scores, not only for the thresholds.
2. Register `deepseek-v4-pro` properly in model-router-mcp: its `bootstrap.py` still seeds
   only the retired `deepseek-chat` and `deepseek-reasoner`, so the live entry is a manual
   registry row whose prices were copied from deepseek-chat. Costs in the logs are estimates
   until the real v4-pro pricing lands in `/registry`.

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
