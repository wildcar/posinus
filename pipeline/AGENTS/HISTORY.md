# History

Newest first. Each entry ≤5 lines using the format defined in `AGENTS.md`.

---

## 2026-07-28 · The source's logo is not an illustration
- What: an image blacklist in the pipeline DB — `ignored_image`, keyed by URL without query. A listed candidate is dropped in `extract_illustrations` before download: no slot in the four-image limit, no caption in the translation call. `preparer.py --ignore-image URL [--note "…"]` adds an entry and purges the picture (row + file) from prepared-but-unpublished items; published and operator-edited ones stay untouched. 6 tests added, 197 pass.
- Why: owner's call on wildcar.org/news/7113 — The Optimist Daily's logo went out as the lead picture, captioned «Optimist daily». Site logos land as illustrations en masse: four queued items carried this same logo at position 1, and GNN, ScienceDaily, nsknews, sunnyskyz show the same pattern.
- Files: pipeline/preparer.py, pipeline/tests/test_preparer.py, pipeline/{AGENTS/SPEC.md,docs/services.md,README.md}
- Prod: pending — deploy, then run `--ignore-image` for the Optimist Daily logo and verify the queue lost its copies.
- Next: the queue still holds other sources' logos (goodnewsnetwork ×2, sciencedaily, nsknews, sunnyskyz, reasonstobecheerful); one `--ignore-image` per URL once the owner confirms the list.

## 2026-07-27 · Telegram bows to the Дзен mirror; captions arrive in Russian, once
- What: Telegram goes back to sendPhoto + caption, capped by `TG_TEXT_LIMIT` (1500 visible chars; Telegram's own 1024 for photo captions on top) with a «Полный текст на wildcar.org» link when paragraphs are dropped. The preparer folds duplicate illustration candidates (URL-without-query; a duplicate donates its caption) and byte-identical downloads, and sends the original captions along in the retelling call, taking them back translated in a lenient `captions` array. 11 tests added, 191 pass.
- Why: owner's calls, all three. dzen.ru mirrors the channel through its телеграм autopublisher, which drops posts over ~1500 chars — the site RSS route opens only past 10 subscribers, so the full-text telegram lasted a day. og:image repeating the lead figure gave posts two identical photographs, and English figcaptions under a Russian retelling looked wrong; translating them rides the same paid call.
- Files: pipeline/{publisher,preparer}.py, pipeline/tests/{test_publisher,test_preparer}.py, pipeline/deploy/pipeline.env.example, pipeline/{AGENTS/SPEC.md,docs/services.md,README.md}
- Prod: LIVE at `73a2db8`, shipped with `git -C /opt/posinus pull` while the services were inactive — `update-ubuntu.sh` fails and rolls back since deb.nodesource.com started answering 403 (see AGENTS/ENV.md). Verified by a dry run of news 5616: telegram is a photo upload again, 817 chars against limit 1024.
- Next: once the Дзен channel passes 10 subscribers and the RSS transmission is on, reconsider the телеграм mirror and the 1500 cap. Check the next prepared item's `illustration.caption` is Russian. ~~Owner: fix or drop the dead nodesource apt repo~~ — done 2026-07-27: the repo was dead weight (apt nodejs purged back in April, node lives in keeper's nvm), removed, `update-ubuntu.sh` verified end-to-end; see AGENTS/ENV.md.

## 2026-07-27 · The whole retelling in telegram, and two new places to read it
- What: New `wildcar_org` platform: news pages with ALL pictures on the static wildcar.org site (content dir → rebuild marker → `posinus-wildcar-org-build` units run MkDocs as keeper → the publisher waits for the page URL), plus a regenerated section index and a Дзен-compliant RSS feed — dzen.ru switches from the телеграм autopublisher to polling `news/rss.xml`. Telegram now sends the full text (sendMessage, ≤4096 visible chars) with the picture as a link preview from wildcar.org. 16 tests added, 180 pass.
- Why: the owner's report «новость в телеграм публикуется не целиком» — a 1024-char photo caption cannot hold a 4-paragraph retelling, and counting the raw HTML against the limit was also throwing away a paragraph that actually fit (news 7169: 2 of 4 paragraphs fit, 1 was posted).
- Files: pipeline/publisher.py, pipeline/tests/test_publisher.py, pipeline/deploy/{install.sh,pipeline.env.example,wildcar-org-build.sh,posinus-wildcar-org-build.service,posinus-wildcar-org-build.path}, pipeline/{AGENTS/SPEC.md,docs/services.md,README.md}, docs/deployment.md, AGENTS/ENV.md; wildcar-site: .gitignore, README.md
- Prod: code deployed at `c23faf8` (12:22 UTC, `update-ubuntu.sh`); a dry run of news 5745 rendered all four platforms, telegram with the full 1416-char text and the wildcar.org preview URL. The platform stays off until the owner's install.sh + env key.
- Next: owner runs install.sh, sets WILDCAR_ORG_BASE_URL in the env, connects the RSS in the Дзен studio and turns its телеграм autoposting off.

## 2026-07-26 · The preparer stops spending the evaluator's name at the router
- What: `evaluator.Config` gained `router_user` (env `ROUTER_USER_ID`, empty falls back to `selector_name`), `build_chat_arguments` sends it as `external_user_id`, and `preparer.main` sets it to `news-preparer` (env `PREPARER_ROUTER_USER_ID`). 5 tests added, 164 pass.
- Why: both scoring and retelling arrived at model-router-mcp as `news-evaluator`, so the router's per-user usage could not tell them apart and every retelling token was billed to the evaluator. `selector_name` itself cannot change - it is the frozen contract string on ~6200 review events - so the router identity had to become its own field.
- Files: pipeline/{evaluator,preparer}.py, pipeline/tests/{test_evaluator,test_preparer}.py, pipeline/deploy/pipeline.env.example, pipeline/AGENTS/SPEC.md, pipeline/docs/services.md
- Prod: LIVE at 19:22 UTC via `update-ubuntu.sh` (no migrations), verified by a preparer dry run on news 3683 that retold through the router as `news-preparer`.
- Next: the publisher posts only the first illustration (`ORDER BY position LIMIT 1`) while the preparer downloads up to four — 94 of 128 prepared items hold 2–4 pictures that never go out. Decide per platform (Telegram media group, VK multi-attachment, site body images) or drop the extra downloads.

## 2026-07-26 · Ten days in the queue, and a preparer that would not let go
- What: The publisher takes a prepared item off the queue after `PUB_EXPIRE_AFTER_DAYS` (10), except one already public somewhere or held by the operator; retention then deletes its pictures, and only ever touches rows that are already out of the queue. Separately: `prepared_ids` now excludes every finished status, and `mark_published` keeps the first publication date. 12 tests added, 159 pass.
- Why: checking the boundary the owner asked about («у картинок срок выйдет, а новость ещё выйдет») turned up a live bug — only `status = 'prepared'` counted as done, so published items came back into the queue and were retold at the price of a model call: 215 preparations over 129 news items in a day, and ten published posts with their date rewritten to today. The platforms were saved only by the `publication` rows already saying `ok`.
- Files: pipeline/{publisher,preparer,retention}.py, pipeline/deploy/pipeline.env.example, pipeline/tests/{test_publisher,test_preparer,test_retention}.py, pipeline/AGENTS/SPEC.md, docs/deployment.md
- Next: watch the first expiries around 2026-08-02 — the whole prepared backlog was made on 23–24 July, so a large batch reaches ten days at once.

## 2026-07-26 · The rubric, and the first thing that deletes files
- What: The evaluator reads the closed rubric list from `exchange_topic`, asks the model for one, and writes it to `exchange_news_topic` in the same transaction as the verdict; an unusable answer lands on the placeholder and shows up in the run counter «без темы» rather than throwing a paid evaluation away. New `retention.py` with a daily timer deletes pictures of unpublished candidates after 10 days and of published items after 30, keeping every row. 10 tests added, 147 pass.
- Why: nothing had ever deleted a pipeline file — the media directory grew about 40 MB a day with no end. Rows are a different matter: a thousand news items cost a quarter of a megabyte and are the whole history «Состав ленты» is built on, so retention never touches them.
- Files: pipeline/{evaluator,preparer,retention}.py, pipeline/deploy/{install.sh,pipeline.env.example,posinus-retention.service,posinus-retention.timer}, pipeline/tests/{test_evaluator,test_retention}.py, pipeline/AGENTS/SPEC.md, docs/contracts/database-contract.md
- Next: the publication queue is longer than ten days, so an item that waits its turn past the tenth day will go out without a picture — either a staleness threshold in the queue or a longer period.

## 2026-07-25 · Apply the operator's corrections to a prepared retelling
- What: New stdlib-only `apply_edits.py` reads `edit-<news_id>.json` from the mailbox and applies the title, body, lead picture and dropped pictures, then removes the request; a `.path` unit runs it within a second, with no timer and no `ExecStartPre` (deleting requests before reading them would lose the edit). `prepared_item` gained `edited_at`/`edited_by`, and `prepared_ids` now excludes edited items so the preparer can never regenerate over a human fix. Edits to a published item are refused. 7 tests added, 126 pass. Crawler side (form, picture actions) in the same commit.
- Why: step 5 of `../docs/ui-concept.md`. The correction has to reach this database somehow, and the one thing it must not be is a second writer on the file.
- Files: pipeline/apply_edits.py, pipeline/preparer.py, pipeline/deploy/{install.sh,posinus-apply-edits.service,posinus-apply-edits-run.path}, pipeline/tests/{test_apply_edits,test_stdlib_only}.py, pipeline/AGENTS/SPEC.md, pipeline/docs/services.md
- Next: owner reruns install.sh for the new unit.

## 2026-07-25 · Telegram tells the operator when something is broken
- What: New stdlib-only `notify.py` with two timers: an hourly alarm check (platform failed ≥3 times, a full day silent inside an open window, an empty queue for 3 days) and a daily one-line digest at 09:00 Moscow. The same alarm repeats at most every 12 hours (`notification` table). Destination is `NOTIFY_CHAT_ID` — the owner's own chat; empty means nothing is sent, which is the default. 9 tests added, 118 pass.
- Why: step 4 / 8.7 of `../docs/ui-concept.md` and the loudest complaint about the system — you learned about a broken platform only by opening the site. The high bar is deliberate: noise makes the channel worthless in a week, and then the message that matters is ignored with the rest.
- Files: pipeline/notify.py, pipeline/deploy/{install.sh,posinus-notify*.service,posinus-notify*.timer,pipeline.env.example}, pipeline/runlog.py, pipeline/tests/{test_notify,test_runlog,test_stdlib_only}.py, pipeline/AGENTS/SPEC.md, pipeline/docs/services.md
- Next: owner sets NOTIFY_CHAT_ID and runs install.sh; then step 5, the flow and the news card.

## 2026-07-25 · Publish the strongest item next, not the one prepared first
- What: `load_plan` reads `exchange_publication_order` from the crawler DB (read-only, 5s timeout) and `order_queue` sorts prepared items by the operator's rank, then strength, then preparation time as a stable tie-break, skipping held and dropped ones. An unreachable crawler DB or a missing view means an empty plan and the previous behaviour, which is the whole point of the fallback. 5 tests added, 109 pass. Crawler side (table, view, screen actions) in the same commit.
- Why: 124 prepared items were going out in preparation order — the worst available signal, since it records when the machine got round to an item rather than how good it is.
- Files: pipeline/publisher.py, pipeline/tests/test_publisher.py, pipeline/AGENTS/SPEC.md, ../docs/contracts/database-contract.md
- Next: Telegram notifications (8.7).

## 2026-07-25 · One row per run: what the machine did, in the database
- What: New stdlib-only `runlog.py` — a `service_run` row opened when a run starts and closed when it ends, with counters and the effective configuration (never a secret). All three scripts record through it, including the backfill service; a dry run leaves no row. Recording never breaks a run: every failure there is swallowed and logged, including an unopenable database. `install.sh` also hands group `posinus` read access to the DB, its `-wal`/`-shm` sidecars and the media tree, with setgid and default ACLs so files created later stay readable. 5 tests added, 104 pass. Crawler side (read-only connection, «Машина» block, backup) in the same commit.
- Why: step 3 of `../docs/ui-concept.md`. «Что делала машина» was answerable only through `journalctl`, which the operator UI cannot read; and the files that matter for access are the ones SQLite creates next week, not the ones present at install time.
- Files: pipeline/runlog.py, pipeline/{evaluator,preparer,publisher}.py, pipeline/deploy/install.sh, pipeline/tests/{test_runlog,test_stdlib_only}.py, pipeline/AGENTS/SPEC.md, ../docs/deployment.md
- Next: owner-side `install.sh` for the new ACLs, then the queue screen (3.7).

## 2026-07-25 · The selection rule comes from the database, with its revision in the event
- What: `load_profile` reads `exchange_active_selection_profile` — the fifth readable object of the contract — and falls back to the built-in `DEFAULT_PROFILE` when the view is missing or empty, so a rollback past the crawler migration still evaluates. The profile name and revision travel in `selector_version` (`0.2.0+deepseek-v4-pro+default.r1`). `--profile` is replaced by `--builtin-profile`; `--backfill --rescore-all` re-applies the thresholds to everything already scored and writes only where the verdict changed, behind a new timer-less `posinus-evaluator-backfill.service` and its request file. 7 tests added, 99 pass. Crawler side (tables, view, migration, screens) in the same commit.
- Why: step 2 of `../docs/ui-concept.md`. One rule for two readers: the evaluator decides by it and the operator screen explains and calibrates by it. A hard-coded copy would have started lying the first time the operator moved a threshold.
- Files: pipeline/evaluator.py, pipeline/deploy/{install.sh,posinus-evaluator-backfill.service,posinus-evaluator-backfill-run.path}, pipeline/tests/test_evaluator.py, pipeline/AGENTS/SPEC.md, pipeline/docs/services.md, ../docs/contracts/database-contract.md
- Next: owner-side deploy, then prompt calibration on the new screen.

## 2026-07-25 · Stop cock, publication window and the request mailbox
- What: The publisher reads a `pause` file in `REQUESTS_DIR` at the start of a run and sends nothing while it is there (an unreadable or malformed file counts as paused; an expired one is deleted and publication resumes). A new item also waits for the publication window, `PUB_WINDOW_START`–`PUB_WINDOW_END` in `PUB_WINDOW_TZ`, default 08:00–22:00 Europe/Moscow; retries of an already-public item ignore the window. `install.sh` creates the mailbox (group `posinus`, setgid, 2770) and installs three `.path` units that start a service when the web drops `run-<name>`; each service removes the request in `ExecStartPre`. 12 tests added, 92 pass.
- Why: step 1 of `../docs/ui-concept.md`. The publisher knew one rule, 120 minutes since the last post, and would happily post a kitten at 03:40 or during a day of mourning. The mailbox is also the fundament for running a service from the web without sudo and without a second job queue.
- Files: pipeline/publisher.py, pipeline/deploy/{install.sh,posinus-*-run.path,posinus-*.service,pipeline.env.example}, pipeline/tests/test_publisher.py, pipeline/AGENTS/SPEC.md, pipeline/docs/services.md
- Next: owner runs `pipeline/deploy/install.sh` so the mailbox and the path units exist on prod; then the queue screen (3.7).

## 2026-07-25 · Publish with the picture again after the media directory moved
- What: `lead_image_path` falls back to `MEDIA_DIR/<news_id>/<filename>` when the stored absolute path is gone, logs the substitution, and warns when the file is missing everywhere; `PublisherConfig` gained `media_dir` (`MEDIA_DIR`). Two tests (82 total). Prod also got `PUB_MIN_INTERVAL_MINUTES=60` in the env file, owner's call against the 118-item backlog.
- Why: the posinus rename moved the media tree but left 336 `illustration.file_path` rows on `/var/lib/news-evaluator/media/...`, so 116 news items (112 still queued) published as text with no picture. All 336 files exist at the new path, so this is a stale string, not lost data - and fixing it in code makes the next move heal itself.
- Files: pipeline/publisher.py, pipeline/tests/test_publisher.py, pipeline/AGENTS/SPEC.md, pipeline/docs/services.md, pipeline/deploy/pipeline.env.example
- Next: rewrite the 336 stale rows for tidiness; queue ordering (docs/ui-concept.md 3.7).

## 2026-07-25 · WAL and a retried write, so a reader cannot cause duplicate posts
- What: `open_own_db` (preparer, publisher) sets `journal_mode = WAL` and `busy_timeout = 30000`; `record_publication` retries a locked write with the same backoff as `evaluator.write_review` and, on final failure of an `ok` send, writes an `unrecorded-<news_id>-<platform>-<ts>.txt` marker and lets the run fail. Three tests added (80 total).
- Why: the DB was in rollback-journal mode, where an open reader blocks a commit. The publisher sends the post BEFORE recording it, so a lost write means the next run posts again - a duplicate in Telegram, VK and on the site. Dormant today, live the moment the planned operator UI reads this DB.
- Files: pipeline/{preparer,publisher}.py, pipeline/tests/test_publisher.py, pipeline/AGENTS/SPEC.md, pipeline/docs/services.md
- Next: crawler-side filter fixes (latest-review decision filter, selector_name on axis filters).

## 2026-07-25 · deepseek-v4-pro plus the token budget it needs; prod verified
- What: DeepSeek retired `deepseek-chat`, so every evaluator and preparer run had been failing with HTTP 400. Switched to `deepseek-v4-pro` (owner's choice) and registered it manually in model-router-mcp, whose `bootstrap.py` still seeds only the retired names. The rename alone was not enough: v4-pro spends ~950 completion tokens on one 20-axis evaluation, so against the old 1000 cap the provider returned empty content, logged as "DeepSeek returned an empty response". Reproduced it against the router (fails at 1000, succeeds at 2000 using 963), then made the budget configurable with a 4000 default for both scripts.
- Why: A model-name swap that leaves 6 of 11 news failing is not a fix, and the failure mode reads as a prompt problem while actually being a token cap.
- Files: pipeline/evaluator.py, pipeline/preparer.py, pipeline/deploy/pipeline.env.example, pipeline/AGENTS/STATE.md, ../AGENTS/ENV.md
- Next: Prompt calibration; put real v4-pro pricing in the router registry.

## 2026-07-25 · Merged into the posinus monorepo as pipeline/
- What: positive-news-evaluator became the `pipeline/` directory of the `posinus` repository (history preserved via git subtree), next to `crawler/`. Units, paths and accounts renamed: `posinus-{evaluator,preparer,publisher}`, `/opt/posinus/pipeline`, `/etc/posinus/pipeline.env`, `/var/lib/posinus/pipeline`, user `posinus-pipeline`. The scripts now run in place from the checkout instead of being copied, so a crawler update ships new pipeline code. Added `tests/test_stdlib_only.py` (77 tests total) to guard the stdlib-only and no-crawler-imports invariants.
- Why: One machine, one contract, one owner — and the crawler's `docs/database-contract.md` was already a cross-repo file dependency. Sharing a repository with Django also made an accidental third-party import easy, hence the guard test.
- Files: whole tree; pipeline/deploy/install.sh, pipeline/tests/test_stdlib_only.py, pipeline/AGENTS.md, ../AGENTS.md
- Next: Owner runs `crawler/deploy/migrate-to-posinus.sh --apply`, then `pipeline/deploy/install.sh` to recreate the timers under the new names.

## 2026-07-24 · Publisher on api.vk.ru; document the VK token maze
- What: `vk_call` and the returned wall URL now use `api.vk.ru` (not `api.vk.com`), ahead of VK's endpoint migration. `docs/services.md` «VK: the token type matters» now covers all three failure modes (community → 214/27; VK ID `vk2.a` → 1051, auth-only) and the working recipe: a classic `vk1.` user token via the grandfathered Kate Mobile app_id (`2685278`), non-expiring.
- Why: VK went live on prod with a real user admin token (post wall-233237778_2); the `vk2.a`/1051 trap was undocumented and cost hours, and api.vk.com is being phased out.
- Files: publisher.py, docs/services.md, AGENTS/{STATE,MEMORY}.md
- Next: owner redeploys (`install.sh`) so prod calls api.vk.ru.

## 2026-07-24 · Document the preparer & publisher services
- What: New `docs/services.md` — operational reference for both services (systemd units + timers, env config incl. PREP_BATCH / PUB_BATCH / PUB_MIN_INTERVAL_MINUTES / PUB_MAX_ATTEMPTS, own-DB tables, media layout, ops commands, the VK error-27 gotcha, throttle + best-effort give-up notes). Registered in the AGENTS.md document map; `AGENTS/ENV.md` points to it.
- Why: Owner asked to save service info in the repo docs.
- Files: docs/services.md, AGENTS.md, AGENTS/ENV.md

## 2026-07-23 · Publisher: rate limit + give up on a failing platform
- What: New items publish at most once per `PUB_MIN_INTERVAL_MINUTES` (default 120), measured from the last successful post; already-public items still finish cross-posting without the limit. A platform that keeps failing is retried up to `PUB_MAX_ATTEMPTS` (8) then given up on, and the item is finalized «Опубликовано» best-effort — fixes head-of-line blocking where one failing platform (a bad VK token, error 27) stalled the whole queue. Publisher now returns 0 on recorded platform failures (no more noisy systemd 'failed'). +5 tests (75 total).
- Why: Owner asked for ≤1 news per 2h; prod showed 20 prepared but only 1 posted because VK failed and blocked the queue.
- Files: publisher.py, tests/test_publisher.py, deploy/news-evaluator.env.example, AGENTS/{SPEC,STATE}.md
- Next: Owner redeploys; VK needs a USER token (group admin), current one is a community token.

## 2026-07-23 · Store the retelling as markdown, not HTML
- What: The preparer now stores the retelling as a canonical markdown document (`retold_body_md`: H1 title, paragraphs, `Источник: [имя](url)`) instead of building an HTML page; the publisher parses that markdown instead of regex-ing paragraphs back out of HTML. Dropped the HTML page (`build_page`, `page_path`, `PAGES_DIR`, the pages dir). Older own DBs auto-migrate: `migrate_own_db` adds `retold_body_md` and backfills it from the old HTML on open, no model calls. Publisher no longer opens the crawler DB (source comes from the markdown). 70 tests; round-trip verified.
- Why: No platform consumes HTML (Эгея wants Neasden, TG its own subset, VK plain text); HTML was a vestigial artifact and the HTML→paragraph round-trip was a smell. Markdown is the model's structure, matches the hermes `.md` convention, and is hand-editable. Owner's call.
- Files: preparer.py, publisher.py, tests/{test_preparer,test_publisher}.py, deploy/{install.sh,news-evaluator.env.example}, AGENTS/{SPEC,STATE}.md, README.md
- Next: Owner redeploys (`install.sh`); the own DB migrates itself on first open.

## 2026-07-23 · Publisher: prepared news to the platforms
- What: New `publisher.py` (stdlib, ported from the proven hermes flows) posts prepared news to Telegram (@posinus, sendPhoto + HTML caption), wildcar.ru (Эгея: login, upload, note-process, note-publish, verify), and a VK community wall (photo upload + wall.post; needs a user token). Full-auto by timer, small batch; each platform enables only when its secret is set. Idempotent per `(news_id, platform)`; marks «Опубликовано» when all enabled platforms succeed. +19 tests (67 total); Telegram getMe and Эгея login/CSRF verified live without posting.
- Why: Publication stage of the pipeline. Owner chose full-auto and VK instead of MAX (MAX bot creation blocked — needs a verified org profile).
- Files: publisher.py, tests/test_publisher.py, deploy/{install.sh,news-publisher.service,news-publisher.timer,news-evaluator.env.example}, AGENTS/{SPEC,STATE,MEMORY}.md, README.md
- Next: Owner deploys (`install.sh`) and fills platform secrets in the env file; get a VK user token + group id for the community.

## 2026-07-23 · Preparer: selected news to HTML pages
- What: New `preparer.py` (stdlib, reuses evaluator's MCP client) prepares selected news: article re-fetch + illustration/caption extraction (og:image, figure/figcaption, lazy img, robots-aware), fresh Russian retelling (JSON title/body, humanizer-ru + deterministic long-dash→hyphen), self-contained HTML page, evaluator-owned SQLite + media/pages dirs, «Подготовлено» label. Deploy: install.sh + news-preparer.timer (15 min). +12 tests (48 total).
- Why: Step 4 of the pipeline — turn «Отобрано» news into publish-ready pages.
- Files: preparer.py, tests/test_preparer.py, deploy/{install.sh,news-preparer.service,news-preparer.timer,news-evaluator.env.example}, AGENTS/{SPEC,STATE,AGENTS}.md, README.md
- Next: Owner deploys; then publication stage («Опубликовано»).

## 2026-07-23 · Default selection profile implemented
- What: `SelectionProfile` + `DEFAULT_PROFILE` in `evaluator.py`; scoring now writes positive/not_positive; new `--backfill` re-verdicts old `skipped` news from stored scores (no model calls). `write_review` takes a `decision`. +9 unit tests (36 total).
- Why: Owner confirmed the strict rule and said proceed; turns the always-`skipped` v0 into real selection.
- Files: evaluator.py, tests/test_evaluator.py, AGENTS/SPEC.md, AGENTS/STATE.md
- Next: Owner deploys and runs `--backfill` once (dry-run on prod: 6228 processed, 120 selected).

## 2026-07-23 · Selection rule and post-selection pipeline specced
- What: Fixed the `default` selection profile (positivity≥8, heroism/clickbait/promo≤4, one bright axis ≥9 → «Отобрано»), the label lifecycle, the «Подготовлено» preparation stage (illustrations+captions, RU retelling, HTML) in an evaluator-owned DB, and the publication placeholder.
- Why: User request to define selection thresholds and the downstream prepare/publish flow.
- Files: AGENTS/SPEC.md, AGENTS/STATE.md
- Next: Implement the profile in code plus a backfill pass over `skipped` events.
- Fixes-on-the-fly: removed a stray duplicate `news-evaluator` repo I had created before finding this one.

## 2026-07-15 · Permanent mode live
- What: Owner ran `deploy/install.sh`: `newsevaluator` user created, timer active (25 news / 10 min), first batch 25/25 with 0 failures, events recorded as `0.2.0+deepseek-chat`.
- Why: Ships the deferred deploy step; the evaluator now runs unattended.
- Files: AGENTS/STATE.md (snapshot refresh only)
- Next: Threshold model; prompt calibration.

## 2026-07-15 · Permanent deploy prepared, model un-hardcoded (v0.2.0)
- What: `selector_version` now records the model that actually answered; empty `EVALUATOR_MODEL` delegates choice to the router (provider/tier hints); added `deploy/` — oneshot service + 10-min timer + env template + idempotent `install.sh` (creates the dedicated user, auto-fills the router token, registers in update-services).
- Why: Owner asked to make the service permanent with the model swappable without code edits.
- Files: evaluator.py, tests/test_evaluator.py, deploy/*, AGENTS/SPEC.md, AGENTS.md, AGENTS/{ENV,STATE,MEMORY}.md, README.md
- Next: Owner runs `sudo bash deploy/install.sh` (permission policy: agents must not create system users); verify first timer runs.

## 2026-07-14 · Evaluator service v0, first live scores
- What: Built stdlib-only `evaluator.py` (MCP chat via model-router-mcp, tolerant JSON validation with up to 3 attempts, transactional event+scores write) plus 27 unit tests; scored news 10–12 into the prod crawler DB with deepseek-chat.
- Why: First working version of the evaluator; validation guards against models that ignore strict JSON rules.
- Files: evaluator.py, tests/test_evaluator.py, AGENTS/SPEC.md, AGENTS.md, AGENTS/{ENV,STATE,MEMORY}.md, README.md
- Next: Threshold model; prompt calibration; dedicated user + systemd timer for deploy.

## 2026-07-14 · Storage contract landed in the crawler
- What: SPEC/STATE now point to the real storage — axis set in `exchange_evaluation_characteristics`, per-axis 0–10 scores in append-only `exchange_evaluation_scores` tied to review events, latest via `exchange_latest_evaluation_scores` — replacing the draft "scores in event metadata" plan.
- Why: The crawler implemented the evaluation side of the exchange contract (crawler commit 9697c9e); specs must match it.
- Files: AGENTS/SPEC.md, AGENTS/STATE.md
- Next: Threshold model, evaluator prompt, then the service skeleton against the real contract.

## 2026-07-14 · Mandatory humanizer-ru skill
- What: Vendored smixs/humanizer-ru v1.2.0 into `.claude/skills/` (un-ignored in .gitignore) and made it mandatory for all Russian prose; set up `origin` and pushed to github.com/wildcar/positive-news-evaluator.
- Why: User requirement — Russian text produced in this repo must not read as AI-generated.
- Files: .claude/skills/humanizer-ru/{SKILL.md,LICENSE}, AGENTS.md, .gitignore, AGENTS/STATE.md
- Next: Threshold model design (see AGENTS/STATE.md → Next).

## 2026-07-14 · Adopt agent-template harness
- What: Migrated repo to the wildcar/agent-template layout; moved SPEC.md → AGENTS/SPEC.md (content unchanged).
- Why: Standardize the agent workflow across repos, matching positive-news-crawler.
- Files: AGENTS.md, CLAUDE.md, README.md, AGENTS/{SPEC,STATE,HISTORY,MEMORY,ENV}.md, docs/adr/TEMPLATE.md, .gitignore, .gitattributes
- Next: Threshold model design (see AGENTS/STATE.md → Next).

## 2026-07-14 · Characteristic set v1
- What: Fixed the v1 characteristic set — 20 independent axes scored 0–10 — plus scale rules and a draft model response format.
- Why: The axes are the foundation for thresholds, the evaluator prompt, and the service.
- Files: SPEC.md (now AGENTS/SPEC.md)
- Next: Threshold model, evaluator prompt, service skeleton.
