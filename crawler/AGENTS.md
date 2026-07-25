# Agent Instructions

Primary entrypoint for any agent (Claude, Codex, DeepSeek, etc.) working in this repository.

## Project

Positive News Crawler — multilingual public-news collector with SQLite feedback loop and operator UI.

## Environment

- OS / shell: see `AGENTS/ENV.md`
- Commit identity: `wildcar <wildcar@mail.ru>`
- Details and command cheat-sheet: `AGENTS/ENV.md`

## Document Map

| File | Role |
|------|------|
| `AGENTS.md` | Workflow, hard rules, architecture map, essential commands. |
| `CLAUDE.md` | Compatibility pointer to `AGENTS.md`. |
| `AGENTS/SPEC.md` | Functional and technical source of truth. |
| `AGENTS/STATE.md` | Current goal, now, next, questions, deferred work. |
| `AGENTS/HISTORY.md` | Append-only iteration log, newest first. |
| `AGENTS/MEMORY.md` | Durable cross-session facts and working agreements. |
| `AGENTS/ENV.md` | Hosts, tools, secrets pointers, command cheat-sheet. |
| `README.md` | User-facing installation, operation, and selector contract. |
| `docs/database-contract.md` | Stable direct-SQLite selector interface. |
| `docs/ubuntu-deployment.md` | Ubuntu production deployment, shared SQLite permissions, and update procedure. |
| `docs/adr/` | Architecture Decision Records. |
| `.claude/skills/humanizer-ru/SKILL.md` | Mandatory editing rules for agent-authored Russian text (vendored skill). |

## Startup Checklist

1. Read `AGENTS.md`, `AGENTS/SPEC.md`, `AGENTS/STATE.md`.
2. Read the top 3–5 entries in `AGENTS/HISTORY.md` and all of `AGENTS/MEMORY.md`.
3. Read `AGENTS/ENV.md` when host or deployment details matter.
4. Run `git status --short` before editing when this directory has been initialized as a Git repository.
5. Do not overwrite unrelated user changes.

## Change Workflow

For every iteration that changes code or behavior:

1. Update `AGENTS/SPEC.md` first when the functional contract changes.
2. Implement and verify the change.
3. Overwrite `AGENTS/STATE.md` with the new live snapshot.
4. Prepend a concise entry to `AGENTS/HISTORY.md`.
5. Update `AGENTS/MEMORY.md` only for durable facts not derivable from code/spec/history.
6. Commit and push only when a Git repository and remote are configured.

History entries use at most five lines:

```text
## YYYY-MM-DD · <title>
- What: <change>
- Why: <reason>
- Files: <key paths>
- Next: <immediate next work>
```

## Memory

`AGENTS/MEMORY.md` is the only durable agent memory store. Do not use external memory stores. Keep one short fact per bullet and never put secret values there.

## Language Rules

- Source code, technical documentation, and comments: English.
- Conversation with the user: Russian.
- End-user UI: Russian, designed for later localization.
- Existing Russian user documentation is an established contract and may remain Russian.

## Mandatory Skill: humanizer-ru

Use of the `humanizer-ru` skill is mandatory. Any Russian prose an agent writes or edits in this project — replies to the user, operator UI strings, Russian user documentation — must follow its rules before delivery.

- The skill is vendored at `.claude/skills/humanizer-ru/SKILL.md` (source: <https://github.com/smixs/humanizer-ru>, v1.2.0, MIT license).
- Claude Code discovers it automatically as a project skill; agents without skill support must read `SKILL.md` and apply its rules manually.
- The rule covers agent-authored text only. Crawled article content, quotes, and proper names are data and must stay verbatim — never "humanize" collected news.

## Project Rules

- SQLite must live on a local disk; never place it on SMB/NFS/OneDrive or access it from another computer.
- Run exactly one crawler worker per database; the worker lock is a hard invariant.
- Crawl public HTTP(S) sources only; obey `robots.txt` and never bypass login, paywall, CAPTCHA, or private/reserved network boundaries.
- `exchange_review_events` is append-only; corrections are new events, never updates or deletes.
- Preserve the stable `exchange_*` SQL contract or accompany a breaking change with a migration, spec update, and selector documentation.
- Do not commit `.env`, SQLite files, backups, logs, caches, browser binaries, or credentials.
- Use Django migrations for schema, views, indexes, constraints, and triggers; do not mutate production schema ad hoc.

## Stack & Commands

Python 3.12/3.13/3.14, Django 5.2 LTS, SQLite WAL, Trafilatura, Feedparser, Playwright Chromium, Waitress, Pytest.

```bash
# install (Windows)
./scripts/install.ps1
# install (Ubuntu)
sh scripts/install.sh
# migrate / operator
python manage.py migrate
python manage.py createoperator operator
# web / worker
python -m waitress --listen=127.0.0.1:8000 newscrawler.wsgi:application
python manage.py runworker
# verify
python manage.py check
python manage.py makemigrations --check --dry-run
python -m pytest
```

## Architecture

```text
Operator browser -> Django/Waitress UI -----------+
                                                    +-> local SQLite (WAL)
Single worker -> feeds/sitemaps/HTML/Playwright ---+
External selector -> exchange_* views/table -------+

collector/models.py                 persistent domain model
collector/services/fetch.py         safe public-web acquisition and extraction
collector/services/ingest.py        normalization and duplicate grouping
collector/services/crawler.py       leases, schedules, crawl runs
collector/services/maintenance.py   source scoring, discovery, retention, backup
collector/management/commands/      worker/operator/maintenance entrypoints
templates/collector/                operator UI
tests/                              unit and SQLite integration tests
deploy/ and scripts/                Ubuntu and Windows operation
```

## Code Style

- Follow Python 3.12+ idioms, PEP 8, type hints on public service boundaries, and snake_case identifiers.
- Keep network, persistence, and policy logic in `collector/services`; views and management commands should stay thin.
- Use timezone-aware UTC datetimes and short SQLite transactions with retry on lock contention.
- Add deterministic fixture tests for parser behavior; real-site smoke tests must remain optional.
