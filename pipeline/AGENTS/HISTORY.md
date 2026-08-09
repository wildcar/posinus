# History

Newest first. Each entry ≤5 lines using the format defined in `AGENTS.md`.

---

## 2026-08-09 · Raster-only illustrations: an SVG logo jammed three platforms at once
- What: downloads keep only `CONTENT_TYPE_EXT` raster types (SVG and other `image/*` exotica skipped, no more `.img` files); a generated picture with unknown magic bytes is dropped. Three moya-planeta.ru SVG logos blacklisted. 281 tests pass. LIVE at `1e53d03`.
- Why: news 8690/8710 carried nothing but SVG logos — telegram, Эгея and VK all rejected the photo upload while wildcar_org (a file copy) published the logos; the vision check skips extensionless `.img`, so the exact junk it exists to drop slipped past unseen.
- Files: pipeline/preparer.py, pipeline/tests/test_preparer.py, pipeline/AGENTS/SPEC.md
- Next: — (both items repaired and out on all four platforms; wildcar.org pages rebuilt with the generated JPG)

## 2026-08-08 · «Добрые новости» уходит: the notes carry exactly the item tags
- What: `PublisherConfig.site_tags` defaults to empty — the news note's tag field is now exactly the item tags (model + constant); `EGEYA_TAGS` stays as an optional base, daypic still overrides with «картина дня». 279 tests pass.
- Why: the owner never asked for «добрые новости» — it was the hermes-era code default, invisible while the flat `tags` field was ignored and surfacing the moment tags[] started to save. Notes 8623 and 8515 carry it (posted before the change); deleting that tag in Эгея's admin strips it from both, owner's call.
- Files: pipeline/publisher.py, pipeline/tests/test_publisher.py, pipeline/{AGENTS/SPEC.md,docs/services.md,deploy/pipeline.env.example}
- Next: the queue's 2 items still carry NULL model tags (old preparer) — constants only until they drain; the first item prepared after the deploy shows model tags on both platforms.

## 2026-08-08 · News get tags, and wildcar.ru gets every picture
- What: the retelling call also returns 3–6 Russian content tags (lenient, `prepared_item.tags`, comma-separated, NULL for older items); the publisher appends the constant «позитивная», «новость», «позитивная новость» at send time and delivers the merged list to Эгея's tags field (after the `EGEYA_TAGS` base) and the wildcar.org page front matter (Material `tags` plugin, enabled in wildcar-site `87ff101`); the daypic page carries «картина дня» the same way. Эгея notes now mirror the wildcar.org page: ALL pictures uploaded, lead → text → the rest, captions on the next Neasden line (caption-inside-picture, per the Neasden source). 278 tests pass.
- Why: owner's request — tags on the platforms that have a field for them, and wildcar.ru should carry the full picture set like wildcar.org does.
- Files: pipeline/{preparer,publisher,daypic}.py, pipeline/tests/{test_preparer,test_publisher,test_daypic}.py, pipeline/{AGENTS/SPEC.md,docs/services.md,README.md,deploy/pipeline.env.example}; wildcar-site: mkdocs.yml (+ recorded awesome-nav/requirements)
- Prod: LIVE at `32ce328` (2026-08-08, `update-ubuntu.sh` twice — the second run ships the tags[] discovery: Эгея's tag control is a multi-select `tags[]`, and the flat `tags=строка` sent since day one was silently ignored, so no auto-posted note ever had tags). Verified live: preparer dry run returned 6 model tags; the 17:00 MSK slot rendered tag chips on wildcar.org/news/8593; a throwaway draft on the live engine proved tags[] saves and creates tags (deleted, no public trace).
- Next: ~~deploy~~; ~~check the 19:00 MSK slot~~ — done: news 8623 went out with all four tags as links and all three pictures on the Эгея note, chips on wildcar.org. Watch the first item prepared after the deploy for model tags on both platforms; the «позитивная» slug is `pozitivnaya-2` (a pre-doseq probe tag squats `pozitivnaya` inside the engine, publicly invisible — owner's call whether to clean it in Эгея's admin).

## 2026-08-03 · Issues go out on a slot grid: 09:00–23:00 Moscow, every two hours
- What: `PUB_SLOTS` (default `09:00,…,23:00` in `PUB_WINDOW_TZ`) replaces the interval and the window for NEW items — one fresh item per slot, a slot counts as served by the last successful post, a run that missed the exact minute still posts inside its slot; empty slots fall back to the old interval+window pacing (window defaults moved to 09:00–23:00). The timer now fires exactly on the slots plus :15/:45 for retries; the run config records `slots` and the crawler's «Эфир» forecasts on the grid (its own HISTORY entry). Also defused a test time bomb: hardcoded `prepared_at='2026-07-23'` crossed `PUB_EXPIRE_AFTER_DAYS` and broke 8 publisher tests on main — dates are relative now. 264 pipeline + 140 crawler tests pass.
- Why: owner's call — releases must sit on exact times, first at 09:00, last at 23:00 MSK; the drifting 30-min timer with a 60-min interval scattered posts over random minutes (05:22, 06:55, 18:15…).
- Files: pipeline/publisher.py, pipeline/deploy/posinus-publisher.timer, pipeline/deploy/pipeline.env.example, pipeline/tests/test_publisher.py, pipeline/{AGENTS/SPEC.md,docs/services.md}, /etc/posinus/pipeline.env
- Next: ~~the owner runs `install.sh` for the retimed timer~~ — done the same day at 19:27 UTC on the owner's go-ahead; the next slot fire is 20:00 UTC (23:00 MSK) on the minute. Watch that issue land at 23:00 sharp.

## 2026-07-30 · The «4 MB cap» was a trailing slash: router URL is /mcp/ now
- What: `router_url` defaults to `http://127.0.0.1:8088/mcp/` (evaluator, crawler settings, both env examples, `/etc/posinus/pipeline.env`); the vision-check retry is reverted and the size gate relaxed from 2.5 MB to a sanity 8 MB. Live-verified: the 3.7 MB GIF (5 MB as base64) that used to reset passed 4 of 4 through `/mcp/`. 254 pipeline + 138 crawler tests pass.
- Why: the owner root-caused the sweep's flaky ECONNRESETs — FastMCP answers `/mcp` with a 307 to `/mcp/` without reading the request body first, so any megabyte-scale POST dies on the redirect; small bodies fit the socket buffer, which is exactly the observed size-correlated flakiness. There never was a 4 MB message cap.
- Files: pipeline/{evaluator,preparer}.py, pipeline/tests/test_preparer.py, crawler/posinus_crawler/settings.py, crawler/{.env.example,deploy/crawler.env.example}, pipeline/deploy/pipeline.env.example, pipeline/{AGENTS/SPEC.md,docs/services.md}, ../AGENTS/ENV.md
- Next: model-router-mcp side (rewrite `scope["path"]` in the ASGI middleware; same fix for the 401 branch answering without draining the body) — the owner's court. ~~Re-check the 7 pictures the sweep kept on failed calls~~ — done on prod right after the deploy: all 7 checked with zero resets, all judged real photographs and kept.

## 2026-07-30 · Junk pictures are now caught by looking: a vision check in the preparer
- What: every downloaded picture goes to the router's `chat` tool as an image (`IMAGE_CHECK_PROVIDER`, default `codex-oauth`); an explicit `drop` verdict deletes it, everything else — router failure, unusable reply, unknown MIME, >2.5 MB — keeps it. A transport quirk found and worked around: megabyte-scale `images_b64` bodies occasionally get an instant ECONNRESET before the router sees the request (root cause open, `../AGENTS/ENV.md`), so the call retries once; the hard 4 MB MCP message cap is why >2.5 MB files are skipped. `--review-images` swept the pre-check queue on prod: 197 pictures of 74 items, 51 dropped — including the UPI header and the «Add as a preferred source on Google» badge from the owner's report, GNN logos, watermarked and off-topic photos; items left pictureless get one generated from the stored retelling. 256 tests pass.
- Why: the owner's report on news 7464 — the URL blacklist catches a logo only the second time, after it has already been published; filters cannot see what a picture is, a vision model can. Live-verified on 7464's own four pictures: drop/drop/(over-cap GIF kept)/keep, every verdict correct.
- Files: pipeline/preparer.py, pipeline/tests/test_preparer.py, pipeline/{AGENTS/SPEC.md,docs/services.md,README.md,deploy/pipeline.env.example}, ../AGENTS/ENV.md
- Next: the 6 items the sweep emptied get generated pictures via the one-off run; watch the next preparer runs for check verdicts in the logs.

## 2026-07-30 · Telegram's 10 MB refusal root-caused: news 3143, prepared before the shrink
- What: the operator's complaint («попыток до 8 … file of size 10668803 bytes is too big for a photo») is news 3143 — two ~10 MB PNGs stored as-is before `shrink_image` existed; telegram burned all 8 attempts, the other three platforms are ok, and https://wildcar.org/news/3143/ weighs ~20 MB. `shrink_image` (this session's change; the concurrent daypic session's `git add -A` swept it into `a8f295a`, so the entry below calls it the owner's) is the cure going forward, verified on those exact files: 10.7 MB → 314 KB, 10 MB → 146 KB, quality checked by eye. 3 new tests; 244 pass. A sweep of the media tree found nothing else over Telegram's cap.
- Why: the pipeline published every picture byte-for-byte as downloaded, and readers paid for it — Telegram with a refusal, slow lines with 5–10 s page loads.
- Files: docs only here (pipeline/docs/services.md, pipeline/AGENTS/STATE.md, ../AGENTS/ENV.md); code already in `a8f295a`.
- Next: ~~the owner runs the handed-over repair block for 3143~~ — owner's call 2026-07-30: no repair, 3143 stays unpublished on telegram (and 6775 on VK, a stale token-era error); both are finalized best-effort, out of the queue since 07-29/07-24, burning nothing. The give-up mechanism did its job.

## 2026-07-30 · The vertical picture was never vertical
- What: the orientation now travels in the PROMPT as well as in `params.size` (`ORIENTATIONS`, one sentence prepended per rendition), and the saved PNG is measured — a mismatch logs «asked for a vertical frame, got 1536x1024» and still publishes. 2 tests added, 246 pass.
- Why: the owner's report — today's telegram post carries a horizontal picture and no vertical file exists. Both renditions came back 1536x1024: the router asked codex-oauth for `1024x1536` (request_logs 8618) and the provider ignored it, as it did on every earlier portrait request. A direct test with the frame stated in words returned a real 1024x1536, so the words are the working channel.
- Files: pipeline/daypic.py, pipeline/tests/test_daypic.py, pipeline/{AGENTS/SPEC.md,docs/services.md}, AGENTS/ENV.md
- Next: today's issue (2026-07-30) stays as published — regenerating it would repost to all four platforms; the owner decides whether to redo telegram by hand. Fixing `size` inside model-router-mcp is still open.

## 2026-07-29 · The first real issue is out (and pictures learned to lose weight)
- What: the first «Картина дня» went out end-to-end on prod at 22:23–22:28 UTC through the run-now request: JSON prompt+description, both renditions via gpt-image-2, wildcar.org https://wildcar.org/kartina/2026-07-30/, telegram https://t.me/posinus/525, Эгея https://wildcar.ru/all/kartina-dnya-30-iyulya-2026/ — VK's upload server answered with an empty `photo` (transient; retried by the timer up to PUB_MAX_ATTEMPTS). The owner's parallel change rode into commit `a8f295a` via `git add -A`: `preparer.shrink_image` (ffmpeg → JPEG ≤1600px when heavier than 400 KB, lenient on failure), wired into the preparer's downloads and daypic's generations; it re-encoded both 3 MB PNGs to ~0.5 MB JPGs on the first issue.
- Why: the manual run was the owner's real button press at 01:23 MSK; the shrink is the owner's own work, recorded here so `a8f295a`'s message is not the whole story.
- Files: (already in `a8f295a`) pipeline/{preparer,daypic}.py, pipeline/tests/test_preparer.py
- Next: confirm VK lands on a retry; watch the 08:00 timer skip the already-published day.

## 2026-07-29 · «Прогнать сейчас» значит сейчас
- What: the `run-daypic` mailbox request lifts the generate_at gate for that pass — the SCRIPT consumes the file (the unit's ExecStartPre rm is gone) and treats it as the operator's «сейчас»; the daily idempotency still holds, and a pause consumes the file too so the .path unit cannot loop. The page says so, and the slot form is pre-filled: migration `0014` writes the real default sizes into empty fields, the model-hint fields carry placeholders naming the pipeline defaults. 244 pipeline + 138 crawler tests pass.
- Why: the owner pressed the button at 01:18 MSK and nothing happened — the gate held until 08:00, which is right for the timer and wrong for a human's explicit click; and blank fields hid what an empty value actually does.
- Files: pipeline/daypic.py, pipeline/tests/test_daypic.py, pipeline/deploy/posinus-daypic.service, pipeline/{AGENTS/SPEC.md,docs/services.md}, crawler/collector/{models,forms,views}.py, crawler/collector/migrations/0014_alter_daypicslot_image_size_and_more.py, crawler/templates/collector/daypic.html, crawler/tests/test_daypic_page.py, crawler/AGENTS/SPEC.md
- Next: deploy needs the updated unit file on the host as well (one install + daemon-reload), then the button makes the first real issue.

## 2026-07-29 · Картина дня, второй заход: две ориентации, подпись и wildcar.org
- What: every issue is now drawn twice from one prompt (vertical for telegram, horizontal `1536x1024` for the sites and VK, falling back to vertical); the chat reply is JSON {prompt, description} and every post carries «<title> · <дата по-русски>» plus the day's description; wildcar.org joins the platforms with its own `kartina` section (page + index + nav, synced by the build script alongside news); the style is random, never repeating within the slot's month. Own-DB columns `file_path_wide`/`caption` arrive by in-place migrate; crawler migration `0013` adds `image_size_wide` and refreshes the unedited 0012 seed prompts. 239 pipeline + 138 crawler tests pass.
- Why: owner's calls, all four — telegram wants vertical while the sites want horizontal, a bare picture without «какие сегодня праздники» under it says nothing, wildcar.org was left out only pending this decision, and the day-of-month style made the month predictable.
- Files: pipeline/{daypic,retention}.py, pipeline/tests/test_daypic.py, pipeline/deploy/{pipeline.env.example,wildcar-org-build.sh}, pipeline/{AGENTS/SPEC.md,docs/services.md,README.md}, crawler/collector/{models,forms}.py, crawler/collector/migrations/0013_daypicslot_image_size_wide.py, crawler/collector/services/daypic.py, crawler/templates/collector/daypic.html, crawler/tests/test_daypic_page.py, docs/contracts/database-contract.md; wildcar-site: .gitignore (docs/kartina)
- Prod: LIVE AND ARMED at `46be072` (2026-07-29 22:09 UTC, `update-ubuntu.sh`, backup `pre-update-20260729T220929Z.sqlite3`). The owner had already run install.sh and enabled the slot; a triggered systemd run logged all four platforms and «not due before 08:00». Seed texts refreshed by 0013 on prod.
- Next: watch the first real issue after 08:00 MSK 2026-07-30 end-to-end on all four platforms (журнал: `journalctl -u posinus-daypic.service`).

## 2026-07-29 · Картина дня moves in from ort_bot
- What: `daypic.py` — a daily generated picture per operator-configured slot: chat model builds the prompt from the date and a day-of-month style, `generate_image` draws it (codex-oauth, vertical 1024x1536, one safe-suffix retry), the publisher's adapters post it to telegram/site/vk with its own `daypic_publication` idempotency; file lands as `daypic/<date>-<slot>.<ext>` for the owner's bot. Slots live in the crawler DB (`exchange_daypic_slot`), retention purges files after `DAYPIC_KEEP_DAYS` (90). New units posinus-daypic.{service,timer} + run.path; 26 tests, 231 pass.
- Why: owner's call — extract the ort_bot «картинка дня» into the pipeline, where the platforms, the router and the timers already live, and make prompt/model/styles editable on the crawler site.
- Files: pipeline/daypic.py, pipeline/retention.py, pipeline/runlog.py, pipeline/tests/{test_daypic,test_runlog,test_stdlib_only}.py, pipeline/deploy/{install.sh,pipeline.env.example,posinus-daypic.*}, pipeline/{AGENTS/SPEC.md,docs/services.md,README.md}, docs/contracts/database-contract.md, docs/deployment.md
- Prod: code LIVE at `a7352b5` (2026-07-29 21:42 UTC, `update-ubuntu.sh`, backup `pre-update-20260729T214158Z.sqlite3`); migration `0012` applied, slot seeded off, in-place dry run as `posinus-pipeline` clean. Dev dry run through the live router: correct next-day date, style 30/31, real holidays.
- Next: owner runs `sudo bash /opt/posinus/pipeline/deploy/install.sh` (daypic units + dir); operator enables the `day` slot; wildcar_org section for pictures stays an open owner's call; the evening slot is one row on the page when wanted.

## 2026-07-29 · A news with no picture draws one
- What: a news item that still has zero pictures after download gets one generated from the retelling: the router's `generate_image` tool, provider `IMAGE_PROVIDER` (default `codex-oauth`, `gpt-image-2` on prod; empty switches the feature off, `IMAGE_MODEL` pins a model), same `news-preparer` identity. The file lands as a normal illustration with `source_url = generated://<model_id>`; the prompt asks for a photorealistic horizontal picture with no text or logos, built from the retold title + lead (capped at 600 chars). Lenient like the caption translation: any failure logs and the item publishes pictureless as before. A dry run only logs that it would generate. `images_generated` lands in the run counters. 8 tests added, 205 pass.
- Why: owner's call. The image blacklist made pictureless items the norm for AP news — the placeholder/promo/badge junk is ignored now, and AP's paywalled real photos never made it through anyway.
- Files: pipeline/preparer.py, pipeline/tests/test_preparer.py, pipeline/deploy/pipeline.env.example, pipeline/{AGENTS/SPEC.md,docs/services.md}
- Prod: LIVE at `64d5e7e` (2026-07-29 14:33 UTC, `update-ubuntu.sh`). Verified to the router boundary: re-preparing news 1702 found 4 real pictures this time (no generation needed — and the retold title carries a live long dash), and a direct `generate_illustration` call failed soft exactly as designed, because the router's codex-oauth credential is dead: `Codex OAuth token refresh failed: HTTP 401 access_denied`, its auth file last refreshed 2026-07-14 while newer copies (keeper's codex CLI, hermes) rotated the single-use refresh token on 2026-07-21. openai-auth-sync, built for exactly this, has user units down since 2026-06-16 and does not watch the router's real path (`/var/lib/model-router/home/.codex/auth.json`).
- Next: ~~owner revives the router's codex login~~ — done 2026-07-29 ~15:00 UTC: the owner refreshed the router's auth.json, and a sandbox `generate_illustration` returned a 2.9 MB photorealistic PNG from `gpt-image-2` (no text/logos, correct provenance entry). The feature is armed; the first pictureless item in the queue will draw its own. Still open: whether generated pictures should carry a visible «изображение сгенерировано» label on the platforms; and openai-auth-sync stays down, so the rotation seesaw can kill the credential again (see ../AGENTS/ENV.md).

## 2026-07-29 · AP's boilerplate joins the image blacklist
- What: three more `--ignore-image` entries on prod, no code change: the AP share-image placeholder («Associated Press / apnews.com»), the AP promo banner (`promo-2x.png` behind its stable `dims4/…/94c503b` resizer URL, seen verbatim on 5 items) and the Google Play badge from AP's footer. 10 blacklist entries total; 1 queued copy purged (news 3567), no AP boilerplate left on any prepared item.
- Why: owner's call on wildcar.org/news/1624 — the published page carries all three instead of a real photo. AP articles without a photo ship the placeholder as og:image, and every AP page appends the app promo and store badges.
- Files: none (data only, `ignored_image` on prod).
- Next: dims.apnews.com keys carry the crop signature in the path, so a DIFFERENT crop of the same placeholder would slip through; if one does, consider matching the `?url=` asset for dims URLs.

## 2026-07-28 · «Длинные тире — прекрасны»: the dash ban is lifted
- What: retellings keep long dashes. The «не используй длинное тире» line is out of the prompt, `normalize_ru` is deleted, and titles, paragraphs and captions go out with «—» exactly as the model writes it (whitespace is still collapsed). Two tests flipped to assert the opposite, 197 pass.
- Why: owner's call, reversing their own humanizer-ru hard rule; the rule left `.claude/skills/humanizer-ru/SKILL.md` the same morning (that edit rode into `f2ed0d6`).
- Files: pipeline/preparer.py, pipeline/tests/test_preparer.py, pipeline/AGENTS/SPEC.md
- Prod: LIVE at `78483f3` (2026-07-28 08:19 UTC, `update-ubuntu.sh`, backup `pre-update-20260728T081935Z.sqlite3`), verified in place: the deployed module has no `normalize_ru` and the prompt no dash line. Already-prepared retellings keep their hyphens, nothing is rewritten retroactively.
- Next: —

## 2026-07-28 · The source's logo is not an illustration
- What: an image blacklist in the pipeline DB — `ignored_image`, keyed by URL without query. A listed candidate is dropped in `extract_illustrations` before download: no slot in the four-image limit, no caption in the translation call. `preparer.py --ignore-image URL [--note "…"]` adds an entry and purges the picture (row + file) from prepared-but-unpublished items; published and operator-edited ones stay untouched. 6 tests added, 197 pass.
- Why: owner's call on wildcar.org/news/7113 — The Optimist Daily's logo went out as the lead picture, captioned «Optimist daily». Site logos land as illustrations en masse: four queued items carried this same logo at position 1, and GNN, ScienceDaily, nsknews, sunnyskyz show the same pattern.
- Files: pipeline/preparer.py, pipeline/tests/test_preparer.py, pipeline/{AGENTS/SPEC.md,docs/services.md,README.md}
- Prod: LIVE at `42e0383` (2026-07-28 08:01 UTC, `update-ubuntu.sh`, backup `pre-update-20260728T080112Z.sqlite3`). `--ignore-image` run for the Optimist Daily logo: 8 queued copies purged (651, 2510, 3059, 4424, 7118, 7120, 7123, 7124 — the last four had it as the LEAD picture), published 7113/7126/6223 untouched, run recorded as `ignore-image` in `service_run`.
- Next: ~~the queue still holds other sources' logos~~ — done 2026-07-28 08:04 UTC, owner confirmed: GNN ×2, ScienceDaily, nsknews, sunnyskyz, RTBC blacklisted too (7 entries total, 20 more queued copies purged); no `%logo%` source_url is left on a prepared item, and no item lost its last picture.

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
