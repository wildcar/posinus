# History

Newest first. Each entry ≤5 lines using the format defined in `AGENTS.md`.

---

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
