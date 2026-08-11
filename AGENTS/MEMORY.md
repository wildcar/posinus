# Memory

Durable agent memory for this repository: working agreements and facts that are NOT derivable
from the code, git history, or the per-service SPEC/STATE/HISTORY documents.

This is the only agent memory store in the project. Do not use external or per-tool memory
stores — memory must travel with the repository when cloned. Read it at the start of every
session; when you learn something durable, append a short bullet and commit it with the
related change.

Recording rules: one bullet, one fact. Working agreements get a brief **why** so the rule does
not look arbitrary. Convert relative dates to absolute. Never record what is already in the
code, git history, or SPEC/STATE/HISTORY. Never put a secret value here. Consolidate from time
to time: merge duplicates, drop stale entries.

## Working agreements

- Agents must not create system principals (users, groups): `useradd` was denied by permission
  policy on 2026-07-14 and again on 2026-07-15, even after the owner approved the permanent
  deploy in chat. Why: granting access to prod data must be executed by the owner personally.
  Prepare an installer and hand it over instead.
- Commit and push to `main` without asking, immediately after a verified change (owner,
  2026-07-24; restated 2026-07-25). Why: the owner runs a fast solo loop here and treats an
  unpushed change as unfinished; branch and PR ceremony is unwanted on this repository. This
  covers the whole tail of a change, not only code: record the verified state in
  `<service>/AGENTS/STATE.md`, log it in `HISTORY.md`, commit and push, all without a
  confirmation round. Do not ask «записать состояние?» - just write it.
- Deploying to prod is part of finishing a change too (owner, 2026-07-25): run
  `sudo /opt/posinus/crawler/scripts/update-ubuntu.sh`, verify, and record the resulting
  commit in `crawler/AGENTS/STATE.md`. Prod is the same machine as dev, so nothing is remote
  about it. The one thing that stays the owner's is creating system principals and running
  `pipeline/deploy/install.sh`.
- Give the owner a complete runnable command, never prose like «перезапустите posinus-web»
  (owner, 2026-07-25). Why: a half-instruction makes them reconstruct the exact invocation.
  Better still, run it: the only host actions reserved for the owner are creating system
  principals and granting them access to prod data (`useradd`, `usermod -aG posinus`).
- User-facing conversation and the operator UI are Russian; code and new technical
  documentation are English, to keep maintenance consistent.
- Prefer simple single-host operation over speculative scaling. Additional infrastructure
  needs an observed requirement, not a predicted one.

## Project facts — shared

- Both services live in one repository since 2026-07-25 (`crawler/`, `pipeline/`). Why: the
  `exchange_*` contract is owned by the crawler's migrations and consumed by the pipeline, so
  a change to the axis set needs both sides in one commit. The old split repositories
  (`positive-news-crawler`, `positive-news-evaluator`) forced two commits with no atomicity,
  and an agent working in one of them had no memory of the other.
- `selector_name` stays the string `news-evaluator` even though everything else was renamed to
  the posinus scheme (2026-07-25). Why: it identifies existing rows in
  `exchange_review_events`, and the queue query excludes news already reviewed under that
  name — renaming it would re-queue the whole corpus for re-evaluation.
- The Django app label `collector` also stays: it is recorded in `django_migrations` and in
  content types, so renaming it would need data migrations for no benefit.
- Prod runs from a single checkout at `/opt/posinus`, and the pipeline scripts execute in
  place rather than being copied (2026-07-25). Why: with both services in one repository the
  old copy-to-`/opt` step became self-referential, and running in place means one `git pull`
  ships both services.

## Project facts — crawler

- Initial sources are added by an operator; automatic discovery follows external links only
  from positively reviewed news.
- The source list is feedback-driven: missing or skipped selector feedback must never
  penalize a source.
- Deleting rejected news is the crawler's job, in its maintenance pass. The pipeline only
  supplies the `not_positive` verdict — the exchange contract forbids it from writing
  anything but the two exchange tables.

## Project facts — pipeline

- The `default` selection rule (owner's spec, 2026-07-23): positivity≥8 AND heroism≤4 AND
  clickbait≤4 AND promo≤4 AND at least one of pride_humanity / pride_russia / inspiration /
  beauty / interestingness / surprise / uniqueness ≥9. Note that heroism is used as an UPPER
  gate here even though its reference `threshold_direction` is `lower_bound`.
- The v0 test selector wrote `decision='skipped'` on purpose: scores without a verdict until
  the threshold model landed (see the pipeline SPEC, «Сервис v0»).
- Post-selection artifacts (the markdown retelling, downloaded illustrations) and the
  «Подготовлено» / «Опубликовано» labels live in the pipeline-owned SQLite, not the crawler's.
- Publication targets (owner, 2026-07-23): Telegram channel @posinus (numeric chat id
  `-1003795927410`, bot `buyvbot`); the wildcar.ru site — an Эгея blog («Позитивные новости»)
  on a SEPARATE host `95.165.109.250`, login `wildcar`; the VK community wall @positivenus
  (`VK_GROUP_ID=233237778`). MAX was dropped: the owner cannot create a MAX bot, which needs a
  verified org or self-employed profile, and chose VK instead.
- The publish mechanisms were ported from `~/repo/hermes` (`send_tg.py`,
  `wildcar_publish_*.py`), which had posted to these platforms manually for months. Those
  secrets live in `~/.hermes/.env` (Telegram) and `hermes/egeya.txt` (Эгея password; the login
  is in line 1 only when the file has ≥2 lines, else it defaults to `wildcar`). The service
  user cannot read keeper's home, so the owner copies them into `/etc/posinus/pipeline.env`.
- VK wall posting needs a classic `vk1.` USER token of a group admin (scope
  photos,wall,groups). A community token fails (`wall.post` 214,
  `photos.getWallUploadServer` 27); a VK ID `vk2.a.` token fails with 1051 — auth only, no API
  methods. VK ID will not mint API tokens for new apps and the old endpoint rejects id.vk.ru
  apps, so the working token comes from the grandfathered Kate Mobile app_id (`2685278`) via
  the legacy implicit flow (non-expiring). Post with `owner_id=-<id>` plus `from_group=1`.
  Full recipe: `pipeline/docs/services.md`, «VK: the token type matters».
- Taking a published news page off wildcar.org (first done 2026-08-11, news 8949): flip its
  `publication` row to `status='removed'` (never delete rows), remove
  `/var/lib/posinus/wildcar-org/news/<id>/`, regenerate `index.md` and `rss.xml` with the
  publisher's own `_wildcar_published_entries`/`build_wildcar_index`/`build_wildcar_feed`
  (they take only `status='ok'` rows), touch the rebuild marker, verify the 404. Why: index
  and feed are static files rewritten only on publish — deleting the page alone leaves them
  linking to a 404 until the next post goes out.
- «Картина дня» reaches the owner's telegram bot `@wildaiapi_bot` (`~/repo/ort_bot`,
  11 subscribers, no channel) by PULL, not push: the bot reads the pickup manifest in
  `DAYPIC_DIR` at 08:00 and broadcasts the vertical picture itself (owner, 2026-08-09,
  who chose this over the pipeline sending through a second bot token). Why: the bot
  already owns its subscriber list and its own broadcast loop, so pushing would have
  meant teaching the pipeline to read another project's user database across a
  permission boundary. The bot's own picture-of-day generation is retired in favour of
  this one — the owner handles that side in its repository.
