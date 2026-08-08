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
- Since 2026-08-08 the retelling call also returns content tags (3–6 short Russian
  tags, lenient parse, `prepared_item.tags` comma-separated; NULL for items prepared
  before). The publisher appends the constant news tags «позитивная», «новость»,
  «позитивная новость» at send time and puts the merged list into the tag-capable
  platforms: Эгея's tags field (after the `EGEYA_TAGS` base «добрые новости») and the
  wildcar.org page front matter (rendered by the Material `tags` plugin, enabled in
  wildcar-site commit `87ff101`). The daypic wildcar.org page carries its Эгея tags
  (`DAYPIC_SITE_TAGS`, «картина дня») the same way. Эгея notes now mirror the
  wildcar.org page: ALL pictures are uploaded — lead first, then text, then the rest,
  each with its caption on the next Neasden line (that renders as a caption inside
  the picture block — verified against the Neasden source, extension `picture.php`).
  LIVE on prod at `32ce328` (2026-08-08, two `update-ubuntu.sh` runs, own-DB `tags`
  column migrated on first open). Found on the way: Эгея's tag control is a
  multi-select `tags[]` (option text = tag name), and the flat `tags=строка` field the
  publisher had sent from day one was silently ignored — no automatically posted note
  ever carried tags, and «добрые новости» never existed as a tag on the site. Fixed with
  doseq form encoding + repeated `tags[]`; verified against the live engine with a
  throwaway draft (tags saved, the unknown tag was created, draft deleted, no public
  trace). The 17:00 MSK slot (news 8593) went out during the work with the old wire
  format: its wildcar.org page renders the tag chips, its Эгея note has no tags
  (published — not repaired). END-TO-END VERIFIED on the 19:00 MSK slot (news 8623):
  the Эгея note carries all four tags as links AND all three pictures, wildcar.org
  renders the chips. Cosmetic leftover: probe #1 (pre-doseq) created a garbage tag
  whose slug squats `pozitivnaya` inside the engine (publicly 404, zero notes), so
  the real «позитивная» lives at `/tags/pozitivnaya-2/` — fixable only from Эгея's
  admin on its host, if the owner cares. Model tags in `prepared_item.tags` start
  filling with the next prepared item (nothing new was selected since the deploy);
  the retelling dry run through the live router already returned six.
- Same day, owner's follow-up: no «добрые новости» on the notes — that was the old
  `EGEYA_TAGS` code default inherited from hermes, surfacing the moment tags started
  to actually save. `site_tags` now defaults to empty (the env knob stays; daypic
  keeps overriding it with «картина дня»), so a news note carries exactly the item
  tags. The 19:00 and 21:00 MSK notes (8623, 8515) went out with the base tag before
  the change; deleting the «добрые новости» tag in Эгея's admin would strip it from
  both at once, owner's call.
- Since 2026-07-26 the router sees two callers: scoring sends `external_user_id` =
  `SELECTOR_NAME` (overridable with `ROUTER_USER_ID`), retelling sends `news-preparer`
  (`PREPARER_ROUTER_USER_ID`). Before that both were `news-evaluator`, so the router billed
  the retellings to the evaluator. LIVE on prod since 2026-07-26 19:22 UTC (commit `0fe0f42`,
  `update-ubuntu.sh`, no migrations, backup `pre-update-20260726T192225Z.sqlite3`) and verified
  there: `preparer.py --dry-run --news-id 3683` retold through the router as `news-preparer`
  and wrote nothing. `/etc/posinus/pipeline.env` has neither key, so both run on the code
  defaults - the file only needs an edit to override them.
- Partially resolved 2026-07-27: the wildcar.org pages and the Дзен feed now carry ALL
  downloaded illustrations with captions (`illustration_files`). Telegram, VK and Эгея
  still publish only the lead picture — that part stays open.
- `publisher.py` posts prepared news, fully automatically by timer, to Telegram @posinus,
  wildcar.ru (Эгея) and the VK community wall @positivenus; each platform enables only when
  its secret is set. Renders each format from the stored markdown; idempotent per
  `(news_id, platform)`. Since 2026-08-03 NEW posts go out on the `PUB_SLOTS` grid —
  09:00–23:00 Moscow every 2 h, one fresh item per slot, a late run still posts inside its
  slot; an emptied `PUB_SLOTS` falls back to `PUB_MIN_INTERVAL_MINUTES` inside the window.
  A failing platform is retried up to `PUB_MAX_ATTEMPTS` (8), then given up
  on so it can't block the queue. LIVE on prod: Telegram + wildcar.ru + VK all working.
  The grid is LIVE at `84c08c4` (2026-08-03 19:22 UTC, `update-ubuntu.sh`) and verified by
  a dry run through the real config: «slot 21:00 served, next 23:00». The retimed timer
  (fires exactly on the slots + :15/:45 for retries) is INSTALLED too — the owner had
  `install.sh` run at 19:27 UTC the same day; `list-timers` shows the next fires at
  :45 (retry run) and 20:00 UTC, which is the 23:00 MSK slot on the minute. All seven
  pipeline timers survived the reinstall, web and worker active.
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
- Operator edits land through the mailbox since 2026-07-25 (`apply_edits.py` + its path unit),
  LIVE on prod at commit `ad2eaac` and verified there: a request file for news 111 was applied in
  under five seconds, `edited_at`/`edited_by` were set and the request consumed. The test title
  and those two columns were then restored on news 111, so nothing of the check is left in the
  data.
- `notify.py` since 2026-07-25: hourly alarms (broken platform, a silent day inside an open
  window, an empty queue) and a daily digest at 09:00 Moscow, both through the existing bot.
  Silent until the owner puts their own chat id in `NOTIFY_CHAT_ID` — never the public channel.
  LIVE on prod (commit `454a811`, both timers enabled, hourly check runs and logs that no chat id
  is set). A dry run against prod data immediately earned its keep: the first version called VK
  broken from the dead 24-attempt row left over from the old token, so alarms now count only
  failures from the last 24 hours — a false alarm on day one is how a notification channel dies.
  Owner action left: put a chat id in `/etc/posinus/pipeline.env`.
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
  «paused» when unreadable; the crawler UI writes it. The publication window (since
  2026-08-03 the fallback for an emptied `PUB_SLOTS`, defaults 09:00–23:00): a new item only
  appears between `PUB_WINDOW_START` and `PUB_WINDOW_END` in `PUB_WINDOW_TZ`
  (Europe/Moscow), while retries of an already-public item ignore it. 92 tests pass. LIVE on prod
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

- The evaluator asks the model for a rubric and writes it to `exchange_news_topic` with the verdict
  (2026-07-26). The list is closed and lives in the crawler DB; an unusable answer lands on the
  placeholder and shows in the run counter «без темы» rather than costing a second paid answer.
- `retention.py` (daily, 03:30 UTC) is the only thing that deletes pipeline files: pictures of an
  unpublished candidate after 10 days, of a published item after 30, plus media directories no row
  knows about. Rows are never deleted. Before it the media directory grew ~40 MB a day: 113 MB in
  three days, 129 directories, 370 files. NOT YET DEPLOYED as of this writing.
- A prepared item that waited `PUB_EXPIRE_AFTER_DAYS` (10) is taken off the queue by the publisher
  (`status = 'expired'`), unless it is already public somewhere or held by the operator. Retention
  deletes files only for rows already out of the queue, so «картинок нет, а новость ещё выйдет»
  cannot happen. Visible on «Эфир» as «Снятые с очереди» and in the flow as «снята с очереди».
- The preparer was resurrecting published items: only `status = 'prepared'` counted as done, so a
  published row came back into the queue and was retold — 215 preparations over 129 news items in
  the 24 hours before the fix, and ten posts with their publication date rewritten to today. Fixed
  on 2026-07-26 together with `mark_published`, which now keeps the first date. Nothing was posted
  twice: every `publication` row already said `ok`.

- Since 2026-07-27 the publisher has a fourth platform, `wildcar_org`, and telegram posts
  the WHOLE retelling. wildcar.org (static MkDocs site on this host) gets a news section:
  the publisher writes `news/<id>/index.md` + all pictures into
  `/var/lib/posinus/wildcar-org`, regenerates the section index and the Дзен RSS feed
  (`news/rss.xml`, marked up per dzen.ru/help requirements), touches the
  `rebuild-wildcar-org` marker for the new `posinus-wildcar-org-build` units (user keeper +
  group posinus, rsync into `~/repo/wildcar-site/docs/news` + `mkdocs build`) and waits for
  the page URL to go live. Telegram switched from sendPhoto (1024-char caption, which cut
  most retellings to one paragraph) to sendMessage with the full text and the picture as a
  link preview from wildcar.org; the length limit is now counted on the visible text — the
  old raw-HTML count alone was costing a paragraph (news 7169 fit 2 of 4, posted 1).
  Дзен needs no adapter: it polls the RSS itself once the owner connects the feed.
  16 tests added, 180 pass. Code is on prod (commit `c23faf8`, `update-ubuntu.sh`,
  2026-07-27 12:22 UTC, backup `pre-update-20260727T122224Z.sqlite3`) and verified by a
  dry run of news 5745 through all four platforms: wildcar_org built the page with 4
  images, telegram rendered 1416 chars of full text with the preview
  `https://wildcar.org/news/5745/1.png`. NOT YET LIVE — the platform is off until the
  owner runs `install.sh` (new build unit + content dir), sets WILDCAR_ORG_BASE_URL in
  `/etc/posinus/pipeline.env`, connects the feed in the Дзен studio and turns the телеграм
  autopublisher off there.

- Since 2026-07-27 (second pass, owner's feedback) telegram is back to sendPhoto with a
  caption: the Дзен телеграм autopublisher — the way dzen.ru mirrors the channel until it
  reaches 10 subscribers and can take the site RSS — drops posts longer than ~1500 visible
  chars, so full retellings had to go. The cap is `TG_TEXT_LIMIT` (1500; Telegram's own
  1024 for photo captions applies on top), counted on visible text, and a truncated
  caption links «Полный текст на wildcar.org» to the news page. The preparer got two
  fixes in the same pass: illustration candidates are de-duplicated by URL-without-query
  (og:image almost always repeats the lead figure with other sizing params; the duplicate
  donates its caption) plus byte-identical downloads are dropped, and the original
  English captions ride in the retelling call as a «Подписи к иллюстрациям» block,
  coming back translated in `captions` — lenient like the rubric, an unusable array
  keeps the originals at no extra model call. 11 tests added, 191 pass. LIVE on prod at
  `73a2db8` (2026-07-27 22:51 UTC, via `git pull` in /opt/posinus: `update-ubuntu.sh` is
  blocked by the dead nodesource apt repo, see AGENTS/ENV.md), verified by a dry run of
  news 5616 — telegram is a photo upload again, 817 chars against limit 1024. To check
  after the next preparer run: `illustration.caption` comes out Russian.
- Since 2026-07-29 a news item that ends up with zero pictures gets one generated from
  the retelling: router `generate_image`, provider `IMAGE_PROVIDER` (default
  `codex-oauth` → `gpt-image-2`), stored as a normal illustration with
  `source_url = generated://<model_id>`. Failure is non-fatal; dry runs never spend an
  image call. 8 tests added, 205 pass. LIVE at `64d5e7e` and verified end-to-end
  2026-07-29 ~15:00 UTC after the owner refreshed the router's codex auth.json: a
  sandbox `generate_illustration` call returned a 2.9 MB photorealistic PNG from
  `gpt-image-2`, no text or logos on it, saved as `1.png` with
  `source_url = generated://gpt-image-2`. The router's codex credential is its own login
  session (owner, 2026-07-29) — no rotation seesaw with other holders; if it expires,
  the symptom is a 401 in the logs and pictureless posts until the owner re-logs it in
  (`../AGENTS/ENV.md`).
- Since 2026-07-28 retellings keep long dashes: the owner lifted the humanizer-ru dash
  ban («длинные тире — прекрасны»), so the prompt line and `normalize_ru` are gone.
  Already-prepared items keep their hyphens; nothing is rewritten retroactively.
- Since 2026-07-28 the preparer keeps an image blacklist: `ignored_image` in its own DB,
  matched by URL without query. A listed candidate is dropped before download, takes no
  slot in the four-image limit and its caption skips the translation call.
  `preparer.py --ignore-image URL [--note "…"]` adds an entry and purges the picture from
  prepared-but-unpublished items (published and operator-edited ones stay). First entry:
  the Optimist Daily logo, which published as the lead picture of news 7113. 6 tests
  added, 197 pass. LIVE at `42e0383` (2026-07-28 08:01 UTC, `update-ubuntu.sh`): the
  logo is blacklisted and its 8 queued copies are purged — four of them were lead
  pictures. With the owner's go-ahead the other sources' logos followed the same day:
  GNN ×2, ScienceDaily, nsknews, sunnyskyz, RTBC — then on 2026-07-29 AP's boilerplate
  (share-image placeholder, promo banner, Google Play badge; wildcar.org/news/1624).
  10 blacklist entries, 29 queued copies purged in total, none left on a prepared item.
  Caveat: dims.apnews.com keys embed the crop signature, so a different crop of the same
  AP placeholder would slip through — matching the `?url=` asset is the fix if it does.
- Since 2026-07-30 downloaded pictures are also judged by LOOKING: each goes to the
  router's `chat` tool as an image (`IMAGE_CHECK_PROVIDER`, default `codex-oauth`), and an
  explicit `drop` verdict deletes it — logos, banners, badges, text cards, watermarked and
  off-topic photos. Lenient: any failure keeps the picture; >8 MB and unknown-MIME files
  are skipped. `--review-images` swept the pre-check queue on prod (2026-07-30 11:24 UTC):
  197 pictures of 74 items, 51 dropped, incl. the UPI header + Google badge from the
  owner's news-7464 report; the 6 items it emptied got pictures generated from their
  stored retellings (2026-07-30 ~11:50 UTC), and the sweep now ends by doing that itself.
  LIVE at `9e052ae`+`e38cfcd`+the slash commit; 254 tests pass.
- The sweep's flaky ECONNRESETs root-caused by the owner (2026-07-30): FastMCP answers
  `/mcp` with a 307 to `/mcp/` WITHOUT reading the request body, so megabyte POSTs died
  on the redirect — there is no 4 MB message cap. `router_url` now defaults to `/mcp/`
  everywhere (evaluator, crawler settings, env examples, `/etc/posinus/pipeline.env`),
  the retry and the 2.5 MB gate are reverted (the vision-check cap is 8 MB, a sanity
  bound); the 3.7 MB GIF that used to reset passed 4 of 4 through `/mcp/` live. Killing
  the redirect inside model-router-mcp (and its 401-without-draining twin) stays open.

- Since 2026-07-29 the pipeline has a fourth deliverable: «Картина дня» (`daypic.py`,
  ported from ~/repo/ort_bot). Slots (prompt pair, style list, caption, local time,
  model hints, the two sizes) live in the crawler DB (`exchange_daypic_slot`,
  migrations `0012`+`0013`, seeded `day` slot switched OFF) and are edited on the
  crawler's «Картина дня» page. Per issue the chat model returns JSON {prompt,
  description} built from the date and a RANDOM style that never repeats within the
  slot's month (no web search at the router — the calendar comes from the model's
  knowledge); the picture is drawn TWICE from that prompt (vertical 1024x1536 for
  telegram and the pickup file, horizontal 1536x1024 for the sites and VK; horizontal
  failure falls back to vertical) and posted with «<title> · <дата>» + description to
  wildcar.org (own `kartina` section, synced by the build script), telegram, Эгея and
  VK. Files: `/var/lib/posinus/pipeline/daypic/<date>-<slot>[-wide].<ext>` — stable
  names for the owner's bot. Own tables `daypic_item`/`daypic_publication`; retention
  deletes both files after `DAYPIC_KEEP_DAYS` (90), rows stay. 34 daypic tests, 239 pass
  total; crawler side 138 pass. LIVE AND ARMED at `46be072` (2026-07-29 22:09 UTC,
  `update-ubuntu.sh`, backup `pre-update-20260729T220929Z.sqlite3`): migrations
  `0012`+`0013` applied (the unedited seed texts were refreshed by 0013), the owner ran
  install.sh and ENABLED the `day` slot, the timer fires every 15 min, and a triggered
  run on prod logged «1 slot(s) enabled, platforms [wildcar_org, telegram, site, vk]» —
  correctly not due before 08:00 MSK. `/etc/posinus/pipeline.env` has no DAYPIC_* keys,
  so everything runs on code defaults. The FIRST REAL ISSUE is OUT
  (2026-07-29 22:28 UTC, via the run-now request): wildcar.org/kartina/2026-07-30/,
  t.me/posinus/525 and wildcar.ru all ok; VK's upload server returned an empty `photo`
  once (transient — the timer retries up to PUB_MAX_ATTEMPTS, then best-effort). Both
  renditions were re-encoded by the owner's `preparer.shrink_image` (ffmpeg, rode into
  `a8f295a`) from ~3 MB PNG to ~0.5 MB JPG — and both came out 1536x1024, so telegram
  got a horizontal picture: codex-oauth ignores `params.size`. Since 2026-07-30 the
  orientation is stated in the prompt as well (`ORIENTATIONS`) and the result is
  measured; the 2026-07-30 issue itself was left published as it went out. A dev
  dry run through the live router already returned a valid JSON pair (prompt +
  description, random style, real July-30 holidays).

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

- News 3143 stays unpublished on telegram, and 6775 on VK — the owner's call
  (2026-07-30): both are already finalized best-effort and out of the queue, no
  attempts are being burned, and newer news reach VK fine. The best-effort give-up
  worked exactly as designed; no repair, the `error` rows in `publication` are just
  history. New items are shrunk at preparation time (`shrink_image`), and nothing
  else in the media tree exceeds Telegram's cap. The ~20 MB wildcar.org page was
  DELETED instead (owner, 2026-07-30): the `(3143, wildcar_org)` publication row
  removed so the next publish cannot resurrect it, the page dir gone, index + RSS
  regenerated via the publisher's own builders, rebuild verified live (404, no
  references). wildcar.ru and VK copies stay; the two 10 MB PNGs in
  `media/3143/` are left for retention to purge on schedule.

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
