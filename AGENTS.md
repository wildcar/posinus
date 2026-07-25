# Agent Instructions

Primary entrypoint for any agent (Claude, Codex, DeepSeek, …) working in this repository.
Read this file first, whichever service you were pointed at.

## Project

posinus — a machine that collects public news, scores it, prepares the strong items and
publishes them. Two services, one host, one SQLite contract between them.

```text
crawler/    collects public news into SQLite and offers the exchange_* SQL contract
pipeline/   evaluator -> preparer -> publisher: scores, retells, posts
```

Published as Telegram @posinus, the wildcar.ru blog («Позитивные новости») and the VK
community @positivenus.

## Pick your service before you edit

The two services have different stacks, different rules and their own state documents.
Working from the wrong one is the most likely way to break something here.

| | `crawler/` | `pipeline/` |
|---|---|---|
| Stack | Django 5.2, venv, Playwright, pytest | Python 3.12 **stdlib only**, unittest |
| Deploy | venv + `scripts/update-ubuntu.sh` | file copy, no venv, `deploy/install.sh` |
| Owns | `/var/lib/posinus/posinus.sqlite3`, its schema | `/var/lib/posinus/pipeline/evaluator.sqlite3` |
| Local docs | `crawler/AGENTS.md` | `pipeline/AGENTS.md` |

Adding a dependency to `pipeline/` breaks its deploy model, and `pipeline/tests/test_stdlib_only.py`
will fail. Reaching into `crawler/` code from `pipeline/` breaks the contract boundary; the
same test guards that too.

## The boundary between them

The services talk **only** through the SQLite exchange contract in
[docs/contracts/database-contract.md](docs/contracts/database-contract.md). No shared Python,
no imports across the boundary, no second process writing the crawler's own tables.

- The crawler's Django migrations own the `exchange_*` schema. The pipeline only reads
  `exchange_news_for_selection`, `exchange_latest_reviews`,
  `exchange_evaluation_characteristics`, `exchange_latest_evaluation_scores` and appends to
  `exchange_review_events` and `exchange_evaluation_scores`.
- A change to the characteristic axes is a crawler migration **and** a pipeline prompt
  change. That is one commit now — make it one commit.
- `exchange_review_events` is append-only. Corrections are new events, never updates or deletes.
- `selector_name` is `news-evaluator`, and it stays that string. It identifies ~6200 existing
  rows; renaming it would push the whole reviewed corpus back into the evaluation queue.

## Document Map

Shared, at the root:

| File | Role |
|------|------|
| `AGENTS.md` | This file. Service boundaries, shared rules, where to go next. |
| `CLAUDE.md` | Compatibility pointer to `AGENTS.md`. |
| `AGENTS/MEMORY.md` | Durable cross-session facts and working agreements, both services. |
| `AGENTS/ENV.md` | Host, tools, credential pointers, command cheat-sheet. One machine. |
| `docs/contracts/database-contract.md` | The stable SQL interface between the services. |
| `docs/deployment.md` | Ubuntu production deployment for both services. |
| `.claude/skills/humanizer-ru/SKILL.md` | Mandatory rules for agent-authored Russian prose. |

Per service, and this is the part that matters when a task touches both:

| File | Role |
|------|------|
| `<service>/AGENTS.md` | Stack, commands, architecture, code style for that service. |
| `<service>/AGENTS/SPEC.md` | Functional source of truth for that service. |
| `<service>/AGENTS/STATE.md` | Current snapshot: goal, now, next, open questions. |
| `<service>/AGENTS/HISTORY.md` | Append-only iteration log, newest first. |
| `<service>/README.md` | User-facing readme. |
| `<service>/docs/adr/` | Architecture Decision Records for that service. |

## Startup Checklist

1. Read this file.
2. Read `AGENTS/MEMORY.md`.
3. Read `<service>/AGENTS.md`, `<service>/AGENTS/SPEC.md` and `<service>/AGENTS/STATE.md`
   for the service you are about to touch.
4. Read the top 3–5 entries of that service's `AGENTS/HISTORY.md`.
5. **If the task touches both services, or the exchange contract, do step 3 for both.**
   A change on one side of the contract is almost never complete on its own.
6. Read `AGENTS/ENV.md` when host, deploy or credential details matter.
7. Run `git status --short` before editing; do not overwrite unrelated user changes.

## Change Workflow

For every iteration that changes code or behavior:

1. Update the affected `<service>/AGENTS/SPEC.md` first when the functional contract changes.
2. Implement and verify the change.
3. Overwrite `<service>/AGENTS/STATE.md` with the new live snapshot.
4. Prepend an entry to `<service>/AGENTS/HISTORY.md`. When a change spans both services,
   write an entry in each, and say so in both.
5. Update `AGENTS/MEMORY.md` only for durable facts not derivable from code, spec or history.
6. Commit and push to `main` after verification, without asking.

History entries use at most five lines:

```text
## YYYY-MM-DD · <title>
- What: <change>
- Why: <reason>
- Files: <key paths>
- Next: <immediate next work>
```

`HISTORY.md` is a log of what happened. Never rewrite old entries, including their file
paths — those paths were correct on that date.

## Memory

`AGENTS/MEMORY.md` at the root is the only durable agent memory store. Do not use external
or per-tool memory stores: memory must travel with the repository when cloned. One short
fact per bullet, a brief **why** for working agreements, absolute dates, never a secret value.

## Language Rules

- Source code, technical documentation, code comments: English.
- Conversation with the user: Russian.
- End-user UI: Russian, designed for later localization.
- Documents already written in another language are an established contract; keep editing
  them in that language rather than silently translating. `crawler/README.md`,
  `pipeline/AGENTS/SPEC.md`, `docs/deployment.md` and `docs/contracts/` are Russian.

## Mandatory Skill: humanizer-ru

Any Russian prose an agent writes or edits — replies to the user, operator UI strings, model
prompt text, Russian documentation — must follow
`.claude/skills/humanizer-ru/SKILL.md` (source: <https://github.com/smixs/humanizer-ru>,
v1.2.0, MIT) before delivery. Claude Code discovers it as a project skill; other agents read
the file and apply its rules manually.

Collected article content, quotes and proper names are data. Never "humanize" collected news.

## Project Rules

Hard constraints. One line each.

- SQLite lives on a local disk. Never on SMB, NFS or OneDrive, never accessed from another host.
- Exactly one crawler worker per database; the worker lock is a hard invariant.
- Crawl public HTTP(S) only; obey `robots.txt`; never bypass login, paywall, CAPTCHA, or
  private/reserved network boundaries.
- Preserve the `exchange_*` SQL contract, or ship the breaking change together with a
  migration, a spec update and updated selector documentation.
- Use Django migrations for schema, views, indexes, constraints and triggers. Never mutate
  the production schema by hand.
- `pipeline/` stays stdlib-only: its deploy is a file copy with no venv to maintain.
- Creating system principals (users, groups) is the server owner's call. Prepare an installer
  and hand it over; do not run `useradd` from an agent.
- Never commit `.env`, SQLite files, backups, logs, caches, browser binaries or credentials.

## Production Layout

One host, one checkout at `/opt/posinus`, both services inside it. Full procedure in
[docs/deployment.md](docs/deployment.md); host specifics in `AGENTS/ENV.md`.

```text
/opt/posinus/                 git checkout, root:root
  crawler/  .venv/            posinus-web.service, posinus-worker.service   (user posinus)
  pipeline/                   posinus-{evaluator,preparer,publisher}.timer  (user posinus-pipeline)
/etc/posinus/crawler.env      crawler config
/etc/posinus/pipeline.env     pipeline config, holds the platform secrets
/etc/posinus/update-services  units the crawler update must stop before migrating
/var/lib/posinus/             posinus.sqlite3 (group posinus) + pipeline/ (pipeline-owned)
/var/log/posinus/             crawler logs
```

Updating the crawler pulls new pipeline code too, because the pipeline scripts run straight
out of the checkout. Rerun `pipeline/deploy/install.sh` only for unit or config changes.
