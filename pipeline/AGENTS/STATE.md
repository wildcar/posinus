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
- LIVE on prod since 2026-07-25 19:57 UTC (commit `3f50691`): the pipeline DB reports
  `journal_mode = wal`. The mode flipped on the first open after the update, verified with a
  `publisher.py --dry-run` that sent nothing.
- `notify.py` since 2026-07-25: hourly alarms (broken platform, a silent day inside an open
  window, an empty queue) and a daily digest at 09:00 Moscow, both through the existing bot.
  Silent until the owner puts their own chat id in `NOTIFY_CHAT_ID` — never the public channel.
  Needs `install.sh` for the two new timers.
- Since 2026-07-25 the publisher takes the queue in the order the crawler computes
  (`exchange_publication_order`: operator rank, then strength, then preparation time), skipping
  held and dropped items. LIVE on prod at commit `db2c1a9`, verified by a run that read the plan
  with no fallback warning; 114 prepared items are now ordered by strength.
- Since 2026-07-25 every run leaves a `service_run` row in the pipeline DB (`runlog.py`):
  service, status, start and end, counters, effective config without secrets. The crawler UI
  reads those rows for its «Машина» block. LIVE on prod since 2026-07-25 21:44 UTC (commit
  `6835aae`): a publisher run recorded `{"published": 11, "queue": 124, "window_open": false}`
  with its config. The staleness rule proved itself immediately — `update-ubuntu.sh` SIGTERMed a
  running preparer, so its row stayed on 'running' with no way to close it, which is exactly what
  the reader reports as «прервался» past twice the timer interval.
- Since 2026-07-25 the selection rule is not in the code: `load_profile` reads
  `exchange_active_selection_profile` from the crawler DB, `selector_version` carries
  `default.r1`, and `--builtin-profile` forces the hard-coded fallback (which also kicks in when
  the view is missing or empty). `--backfill --rescore-all` re-verdicts the whole scored corpus
  from stored scores, writing only changes; it runs from `posinus-evaluator-backfill.service`,
  which has no timer and only fires on a `run-evaluator-backfill` request file. 99 tests pass.
  LIVE on prod since 2026-07-25 21:06 UTC (commit `c83cc8a`): a run triggered through the
  mailbox logs «selection profile default.r1», and `--backfill --rescore-all --dry-run` over the
  whole corpus reports 6469 news, 125 selected, 0 corrections — the thresholds in the database
  reproduce every verdict the hard-coded rule had produced. That pass takes ~18s, almost all of
  it in the join of `exchange_latest_evaluation_scores` with `exchange_latest_reviews` (two
  window-function views materialized); fine for a background oneshot, and the UI's own pivot
  reads the score view alone in 0.2s.
- Since 2026-07-25 the publisher has two safety catches, both from step 1 of
  `../docs/ui-concept.md`. The stop cock: a `pause` file in `REQUESTS_DIR`
  (`/var/lib/posinus/pipeline/requests`) holds every send, expires by itself and fails towards
  «paused» when unreadable; the crawler UI writes it. The publication window: a new item only
  appears between `PUB_WINDOW_START` and `PUB_WINDOW_END` in `PUB_WINDOW_TZ` (08:00–22:00
  Europe/Moscow), while retries of an already-public item ignore it. 92 tests pass. LIVE on prod
  since 2026-07-25 20:44 UTC (commit `2978f31`, `install.sh` run by the owner) and verified there
  end to end: the mailbox is `drwxrws--- posinus-pipeline:posinus`, the web user `posinus` can
  write into it and the pipeline user reads and removes; a `run-publisher` file started the
  service through the `.path` unit and was consumed; a pause with a deadline held two runs
  («nothing sent, the queue keeps growing») and the run after the deadline logged «pause expired»
  and deleted the file. The window is visible in every run log: «window closed, opens
  2026-07-26T05:00:00+00:00» — that is 08:00 Moscow.
- Prod backlog on 2026-07-25: 118 items `prepared` against 11 `published`. Order of exit is
  preparation time, so a strong item waits behind average ones and anything older than a couple
  of days goes out stale. This is the queue screen argued for in `../docs/ui-concept.md` (3.7),
  and it is now a live problem, not a hypothetical one. `PUB_MIN_INTERVAL_MINUTES=60` was added
  to `/etc/posinus/pipeline.env` on 2026-07-25 (owner's call) to drain it twice as fast; the
  file had no `PUB_*` key at all before, so everything else still runs on code defaults.
- Posts went out without pictures because `illustration.file_path` is absolute and the posinus
  rename left 336 rows (116 news items, 112 of them still queued) pointing at
  `/var/lib/news-evaluator/media/...`. The files themselves were moved and all 336 are present
  under `/var/lib/posinus/pipeline/media/`, so nothing was lost. Fixed in code, not in data:
  the publisher falls back to `MEDIA_DIR/<news_id>/<filename>`. The stale rows can be rewritten
  LIVE on prod (commit `b885377`, 2026-07-25 20:07 UTC). The 336 stale rows were then rewritten
  to the new prefix on 2026-07-25 20:15 UTC (owner authorized the direct UPDATE); all 370
  `illustration` rows now point at an existing file, and news 111 renders `image=True` without
  the fallback. Backup taken before the rewrite:
  `/var/lib/posinus/pipeline/pre-mediapath-20260725.sqlite3`.
- One VK row is a dead tail, not a live failure: news 6775, 24 attempts, error 27 (group auth),
  accumulated before the classic user token was installed. 15 VK posts are `ok`, so the token
  works; the item is past `PUB_MAX_ATTEMPTS` and finalizes best-effort on the next real run.

## Next

1. Prompt calibration and soft profiles («Россия» / «Международное»). The `default` profile
   selected 0 of 6 in the first clean v4-pro batch, so calibration should account for how the
   new model scores, not only for the thresholds. The tooling for this now exists: the «Отбор»
   screen shows what a draft would pass on the current corpus, so calibration is an operator
   session rather than a code change.
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
