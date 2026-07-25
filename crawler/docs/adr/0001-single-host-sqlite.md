# ADR 0001: Single-host SQLite architecture

- Status: Accepted
- Date: 2026-07-13

## Context

The first release handles at most 200 sources. The web UI, crawler, and asynchronous selector run on one Windows or Ubuntu machine. Operational simplicity is more important than horizontal scaling.

## Decision

Use a local SQLite database in WAL mode with a 30-second busy timeout and foreign keys enabled. Run exactly one crawler worker, guarded by an OS file lock. The external selector reads stable `exchange_*` views and inserts append-only review events into the same file.

## Consequences

- No database server, broker, Redis, or Celery is required.
- Short transactions allow the UI, worker, and selector to coexist at the target volume.
- The database cannot be placed on a network/synchronized filesystem or shared across computers.
- Remote selectors, multiple workers, or larger scale require a new ADR and likely a server database or API boundary.

