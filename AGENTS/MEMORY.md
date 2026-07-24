# Memory

Durable agent memory for this repository: working agreements and facts that are NOT
derivable from the code, git history, or SPEC/STATE/HISTORY.

This is the ONLY agent memory store in the project. Do not use external or per-tool memory
stores — memory must travel with the repo (see AGENTS.md -> Memory). Read at the start of
every session; when you learn something durable, append a short bullet here and commit it
together with the related change.

MEMORY.md = durable facts/agreements; current state -> STATE.md; iteration log -> HISTORY.md.

## Working agreements (feedback)

- Agents must not create system principals (users/groups): `useradd newsevaluator` was
  denied by permission policy on 2026-07-14 and again on 2026-07-15 even after the
  owner approved the permanent deploy in chat. Why: granting access to prod data must
  be executed by the owner personally — prepare an installer and hand it over instead.
- Commit and push to `main` without asking, immediately after a verified change (owner,
  2026-07-24). Why: the owner runs a fast solo loop here and treats an unpushed change as
  unfinished; branch/PR ceremony is unwanted on this repo.

## Project facts

- The v0 test selector writes `decision='skipped'` on purpose: scores without a verdict
  until the threshold model lands (see SPEC «Сервис v0»).
- The `default` selection rule (owner's spec, 2026-07-23): positivity≥8 AND heroism≤4
  AND clickbait≤4 AND promo≤4 AND at least one of pride_humanity/pride_russia/inspiration/
  beauty/interestingness/surprise/uniqueness ≥9. Note heroism is used as an UPPER gate
  here even though its reference `threshold_direction` is `lower_bound`.
- Post-selection artifacts (prepared HTML, downloaded illustrations, retelling) and the
  «Подготовлено»/«Опубликовано» labels live in an evaluator-owned DB, NOT the crawler DB:
  the exchange contract forbids clients from writing any table but the two exchange ones.
- Deleting rejected news is the crawler's job (its maintenance), not the evaluator's —
  same contract limit. The evaluator only supplies the `not_positive` verdict.
- Publication targets (owner, 2026-07-23): Telegram channel @posinus (numeric chat id
  `-1003795927410`, bot `buyvbot`); site wildcar.ru — an Эгея («Позитивные новости») blog
  on a SEPARATE host `95.165.109.250`, login `wildcar`; VK community wall @positivenus
  (`VK_GROUP_ID=233237778`). MAX was dropped —
  owner cannot create a MAX bot (needs a verified org/self-employed profile), chose VK.
- The publisher's publish mechanisms were ported from `~/repo/hermes` (`send_tg.py`,
  `wildcar_publish_*.py`), which have posted to these platforms manually for months. Those
  secrets live in `~/.hermes/.env` (Telegram) and `hermes/egeya.txt` (Эгея password, login
  in line 1 only if the file has ≥2 lines else default `wildcar`) — but `newsevaluator`
  can't read keeper's home, so the owner must copy them into the evaluator env file.
- VK: wall posting needs a classic `vk1.` USER token of a group admin (scope
  photos,wall,groups). A community token fails (`wall.post` 214, `photos.getWallUploadServer`
  27); a VK ID `vk2.a.` token (id.vk.ru PKCE / "Log in with VK") fails with 1051 — auth-only,
  no API methods. VK ID won't mint API tokens for new apps and the old endpoint rejects
  id.vk.ru apps, so the working token comes from the grandfathered Kate Mobile app_id
  (`2685278`) via the legacy implicit flow (non-expiring). Post with `owner_id=-<id>` +
  `from_group=1`. Full recipe: `docs/services.md` «VK: the token type matters».
