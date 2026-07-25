# Pipeline — Agent Instructions

Local instructions for `pipeline/`. Read the repository root [`AGENTS.md`](../AGENTS.md) first:
it holds the service boundaries, the shared rules, the memory store and the environment. This
file covers only what is specific to this service.

## What it is

Three stages over the news the crawler collects: score it, prepare the selected items, publish
them. One deployable, three timers.

## Documents

| File | Role |
|------|------|
| `AGENTS/SPEC.md` | Functional specification, the source of truth for behavior (Russian). |
| `AGENTS/STATE.md` | Current snapshot: goal, now, next, open questions, deferred. |
| `AGENTS/HISTORY.md` | Append-only iteration log, newest first. |
| `README.md` | What the three scripts do and how to run them. |
| `docs/services.md` | Operational reference: units, timers, env config, own-DB tables, gotchas. |
| `docs/adr/` | Architecture Decision Records for the pipeline. |

Shared, at the root: `../AGENTS/MEMORY.md`, `../AGENTS/ENV.md`,
`../docs/contracts/database-contract.md`, `../docs/deployment.md`.

## Stack & Commands

Python 3.12, **standard library only** (sqlite3 + urllib). Nothing to install; the deploy is a
file copy. Run everything from this directory.

```bash
python3 -m unittest discover -s tests
ROUTER_AUTH_TOKEN=... python3 evaluator.py --dry-run --limit 1
ROUTER_AUTH_TOKEN=... python3 preparer.py --dry-run --news-id N
python3 publisher.py --dry-run --news-id N          # no model, no router
sudo bash deploy/install.sh                         # units + config; the owner runs it
```

## Architecture

```text
evaluator.py   scoring + selection: MCP HTTP client, prompt builder (axes read from the
               crawler's reference table), reply validation, selection profile, DB writer,
               backfill
preparer.py    prepares selected news: article re-fetch, illustration and caption extraction,
               Russian retelling as markdown, pipeline-owned SQLite + media dir
publisher.py   posts prepared news to Telegram / wildcar.ru (Эгея) / VK; stdlib HTTP plus a
               cookie session, idempotent per (news_id, platform)
tests/         unittest suite for all three, no network and no crawler DB
deploy/        systemd services and timers, env template, install.sh
```

The three modules import each other: `preparer` reuses the router client, `Config` and JSON
extraction from `evaluator`; `publisher` reuses the own-DB schema and markdown builder from
`preparer`. That is why this is one deployable and not three.

## Service-specific rules

- **Stdlib only.** Adding a third-party import breaks the file-copy deploy and fails
  `tests/test_stdlib_only.py`. That test also blocks importing `collector`, `posinus_crawler`
  or `django`: the exchange SQL contract is the only interface to the crawler.
- Scores are integers 0–10 on independent axes. The axis set is fixed in `AGENTS/SPEC.md` (v1);
  changing it is a SPEC change first, and then a migration on the crawler side.
- Write only to `exchange_review_events` and `exchange_evaluation_scores`, and only by
  appending. Everything the pipeline owns goes in its own SQLite.
- `selector_name` stays `news-evaluator`. See the root `AGENTS.md`, «The boundary between them».
- The scripts run in place from `/opt/posinus/pipeline`, so a crawler update ships new pipeline
  code. Rerun `deploy/install.sh` only for unit or config changes.

## Code Style

- Python 3.12 with type hints; stdlib only. No formatter pinned yet.
- Validation error strings are Russian: they are fed back to a model whose instruction is
  Russian. Log messages are English.
- Match the conventions of surrounding code: comment density, naming, idiom.
