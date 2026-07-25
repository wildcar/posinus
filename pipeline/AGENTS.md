# Agent Instructions

Primary entrypoint for any agent (Claude, Codex, DeepSeek, etc.) working in this repository.

## Project

positive-posinus-evaluator — AI agent that scores news collected by Positive News Crawler on a fixed set of characteristics; per-axis thresholds decide which news pass on.

## Environment

- OS / shell: see `AGENTS/ENV.md`
- Commit identity: `wildcar <wildcar@mail.ru>`
- Details, credentials, command cheat-sheet: `AGENTS/ENV.md`

## Document Map

| File | Role |
|------|------|
| `AGENTS.md` | This entrypoint. Workflow, rules, map. |
| `CLAUDE.md` | Compatibility pointer to `AGENTS.md`. |
| `AGENTS/SPEC.md` | Functional specification — source of truth for product behavior. |
| `AGENTS/STATE.md` | Current snapshot: goal, now, next, open questions, deferred. Overwritten each iteration. |
| `AGENTS/HISTORY.md` | Append-only iteration log, newest first. Read only the top few entries. |
| `AGENTS/MEMORY.md` | Durable cross-session memory: working agreements + project facts. The ONLY agent memory store — see Memory. |
| `AGENTS/ENV.md` | Host, tools, credentials, command cheat-sheet. Read on demand. |
| `README.md` | Public-facing readme: what the project is, how to run it locally. |
| `docs/` | Domain / reference docs. Read on demand when a task touches that area. |
| `docs/services.md` | Operational reference for the preparer & publisher services: systemd units/timers, env config, own-DB tables, ops commands, gotchas. |
| `docs/adr/` | Architecture Decision Records — one file per significant decision (see `docs/adr/TEMPLATE.md`). |
| `.claude/skills/humanizer-ru/SKILL.md` | Vendored humanizer-ru skill — mandatory for all Russian prose (see Language Rules). |

## Startup Checklist

1. Read `AGENTS.md` (this file).
2. Read `AGENTS/SPEC.md`.
3. Read `AGENTS/STATE.md`.
4. Read top 3–5 entries in `AGENTS/HISTORY.md`.
5. Read `AGENTS/MEMORY.md` (working agreements + durable facts).
6. Check `git status --short` before editing; do not overwrite unrelated user changes.

Open `AGENTS/ENV.md` only when you need environment details. Open the relevant file under `docs/` when the task touches that domain.

## Change Workflow

For every iteration that changes code or behavior:

1. If the functional contract changes — update `AGENTS/SPEC.md` first.
2. Make the changes.
3. Overwrite `AGENTS/STATE.md` to reflect the new current state.
4. Prepend a new entry to `AGENTS/HISTORY.md` using the format below.
5. Commit and push after successful verification.

### `AGENTS/HISTORY.md` entry format (≤5 lines, newest first)

```
## YYYY-MM-DD · <short iteration title>
- What: <one line — what changed>
- Why: <one line — reason / task>
- Files: <key paths, comma-separated>
- Next: <one line — what was planned right after>
```

Keep each entry tight. Long explanations belong in commit messages or `SPEC.md`. An optional `Fixes-on-the-fly:` line is fine when an iteration also corrected small things discovered mid-way.

When you ship a deferred item from `STATE.md`, write a normal HISTORY entry and remove the item from `STATE.md`.

## Memory

`AGENTS/MEMORY.md` is the **single** store of durable agent memory in this project.
Do not use external or per-tool memory stores (memory directories outside the repo, a
tool's built-in memory, etc.): memory must travel with the repository when cloned.

- Read `AGENTS/MEMORY.md` at the start of every session (see Startup Checklist).
- When you learn a durable fact or a working agreement, append a short bullet there and
  commit it together with the related change.
- Split of concerns: durable facts/agreements -> `MEMORY.md`; current snapshot ->
  `STATE.md`; iteration log -> `HISTORY.md`.

Recording rules — keep these a habit:

- One bullet = one fact; keep it short. Long explanations belong in commit messages or `SPEC.md`.
- For working agreements, add a brief **why** so the rule doesn't look arbitrary.
- Convert relative dates to absolute ("today" → the concrete date).
- Do NOT record what is already in the code, git history, or SPEC/STATE/HISTORY.
- Consolidate from time to time: merge duplicates, drop stale or wrong entries.

## Language Rules

- Source code, technical docs, code comments: English.
- Conversation with the user: Russian.
- End-user UI text: Russian, with ability to extend to other languages.
- Existing docs already written in another language are an established contract — keep editing them in that language; don't silently translate. In this repo `AGENTS/SPEC.md` is in Russian.
- Mandatory: any Russian prose an agent writes or edits (docs, UI strings, LLM prompt texts, user-facing comments) goes through the humanizer-ru skill vendored at `.claude/skills/humanizer-ru/SKILL.md` (upstream: https://github.com/smixs/humanizer-ru, v1.2.0). Claude Code auto-loads it as a project skill; other agents read that file and apply its rules before delivering the text.

## Project Rules

Hard constraints and invariants this project must not violate. Keep each rule one line.

- Respect the crawler's exchange contract (`~/repo/positive-news-crawler/docs/database-contract.md`): read `exchange_news_for_selection` / `exchange_latest_reviews` / `exchange_evaluation_characteristics` / `exchange_latest_evaluation_scores`, append rows only to `exchange_review_events` and `exchange_evaluation_scores`, never touch other tables.
- Scores are integers 0–10 on independent axes; the axis set is fixed in `AGENTS/SPEC.md` (v1) — changing it is a SPEC change first.
- All Russian prose must pass the vendored humanizer-ru skill — no exceptions (see Language Rules).
- `evaluator.py` stays stdlib-only: host deploy is a plain file copy to `/opt/posinus/pipeline`, no venv to maintain.
- Creating system principals (users, groups) is the server owner's call — ask, don't create them from the agent.

## Stack & Commands

Python 3.12, standard library only (sqlite3 + urllib): no dependencies to install, deploy is a file copy. Full cheat-sheet with the host run command: `AGENTS/ENV.md`.

```bash
# install      — nothing: Python 3.12 stdlib only
# test         — python3 -m unittest discover -s tests
# dry run      — ROUTER_AUTH_TOKEN=... python3 evaluator.py --dry-run --limit 1
# prepare 1    — ROUTER_AUTH_TOKEN=... python3 preparer.py --dry-run --news-id N
# publish 1    — python3 publisher.py --dry-run --news-id N (no model, no router)
# host deploy  — sudo bash deploy/install.sh (user, config, systemd timers)
# host status  — systemctl list-timers 'news-*.timer'; journalctl -u posinus-publisher.service
# lint         — none yet
```

## Architecture

```
evaluator.py   scoring + selection: MCP HTTP client, prompt builder (axes from the
               DB reference), reply validation, selection profile, DB writer, backfill
preparer.py    prepares selected news: article re-fetch, illustration+caption
               extraction, Russian retelling, HTML page, evaluator-owned SQLite
publisher.py   posts prepared news to Telegram / wildcar.ru (Эгея) / VK; stdlib
               HTTP + cookie session, idempotent per (news_id, platform)
tests/         unittest suite for all three scripts (no network, no crawler DB)
deploy/        host install: systemd services + timers, env template, install.sh
AGENTS/        agent docs: SPEC (contract), STATE, HISTORY, MEMORY, ENV
docs/adr/      architecture decision records
```

## Code Style

- Python 3.12 with type hints; stdlib only (see Project Rules). No formatter pinned yet.
- Validation error strings are Russian (they are fed back to the model whose instruction is Russian); log messages are English.
- Match the conventions of surrounding code: comment density, naming, idiom.
