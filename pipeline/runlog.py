#!/usr/bin/env python3
"""One row per service run, in the pipeline-owned SQLite.

«Что делала машина» is unanswerable today: the three scripts leave nothing
behind but journal lines, and the operator UI has no way to read those. This
module is the whole mechanism — a row written when a run starts and closed when
it ends, with its counters and the configuration it ran under (never a secret).

Rules that matter more than the schema:

- Recording a run must never break the run. Every function here swallows its own
  errors and logs them; a service that cannot write its diary still does its job.
- A process killed mid-run leaves its row on 'running' forever, so a row older
  than twice its timer interval reads as 'interrupted' rather than live. That
  judgement belongs to the reader, and `STALE_AFTER_SECONDS` carries the number.
- The schema is created if missing, so a rollback to older code cannot leave a
  service unable to start.

Single-file, stdlib-only, like every script here.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("posinus-runlog")

DEFAULT_DB = "/var/lib/posinus/pipeline/evaluator.sqlite3"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS service_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,              -- evaluator | preparer | publisher | evaluator-backfill
    status TEXT NOT NULL,               -- running | ok | failed
    started_at TEXT NOT NULL,
    finished_at TEXT,
    counters TEXT NOT NULL DEFAULT '{}',
    config TEXT NOT NULL DEFAULT '{}',  -- effective settings, no secrets
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_service_run_recent ON service_run(service, started_at DESC);
"""

# How long an unfinished row stays believable, per service: twice the timer
# interval plus a little. Past it the reader shows «прервался».
STALE_AFTER_SECONDS = {
    "evaluator": 25 * 60,
    "preparer": 35 * 60,
    "publisher": 65 * 60,
    "evaluator-backfill": 30 * 60,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_runlog(path: str = DEFAULT_DB) -> sqlite3.Connection | None:
    """Connection to the run log, or None when it cannot be opened.

    Same WAL settings as the rest of the pipeline-owned database: a reading web
    process must never block a writing service.
    """
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA busy_timeout = 30000")
        con.executescript(SCHEMA_SQL)
        con.commit()
        return con
    except (sqlite3.Error, OSError) as exc:
        # OSError too: the parent may not be a directory, or not writable at all.
        log.warning("cannot open the run log at %s: %s", path, exc)
        return None


def start_run(con: sqlite3.Connection | None, service: str, config: dict[str, Any]) -> int | None:
    if con is None:
        return None
    try:
        with con:
            cur = con.execute(
                "INSERT INTO service_run (service, status, started_at, config) VALUES (?, 'running', ?, ?)",
                (service, _now(), json.dumps(config, ensure_ascii=False, sort_keys=True)),
            )
        return cur.lastrowid
    except sqlite3.Error as exc:
        log.warning("cannot record the start of a %s run: %s", service, exc)
        return None


def finish_run(
    con: sqlite3.Connection | None,
    run_id: int | None,
    status: str,
    counters: dict[str, Any],
    error: str = "",
) -> None:
    if con is None or run_id is None:
        return
    try:
        with con:
            con.execute(
                "UPDATE service_run SET status = ?, finished_at = ?, counters = ?, error = ? WHERE id = ?",
                (status, _now(), json.dumps(counters, ensure_ascii=False, sort_keys=True), error[:2000], run_id),
            )
    except sqlite3.Error as exc:
        log.warning("cannot close run %s: %s", run_id, exc)


@contextmanager
def record(service: str, db_path: str, config: dict[str, Any]):
    """Wrap a run: open the row, hand over a counters dict, close it either way.

    The counters dict is the run's own scratch space — whatever the service puts
    in it is what the operator screen will show.
    """
    con = open_runlog(db_path)
    run_id = start_run(con, service, config)
    counters: dict[str, Any] = {}
    try:
        yield counters
    except BaseException as exc:  # including KeyboardInterrupt: the row must close
        finish_run(con, run_id, "failed", counters, f"{type(exc).__name__}: {exc}")
        raise
    else:
        finish_run(con, run_id, "ok", counters)
    finally:
        if con is not None:
            con.close()
