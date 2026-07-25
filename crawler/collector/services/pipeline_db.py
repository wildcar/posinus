"""Read-only access to the pipeline's own SQLite.

Half of the machine — the retellings, the illustrations, the links to published
posts, what each service did on its last run — lives in a database this process
does not own. Three rules make reading it safe:

1. **Never write.** The connection sets `PRAGMA query_only = ON`. The file system
   permissions still allow writing, and that is deliberate: SQLite has to be able
   to recover a journal, and a read-only mount would fail in a random place a week
   after install. The pragma is the honest guard, the group access is the plumbing.
2. **Never block a writer.** A short busy timeout, two seconds. The publisher
   sends a post and then records it; a web page that holds a lock long enough to
   fail that write causes a duplicate post. Better a page that says «нет связи».
3. **Never break the page.** The file does not exist on a development machine and
   may be unreadable on a broken host. Every reader here returns nothing instead
   of raising, and the views show a placeholder.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings

log = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 2000


class PipelineUnavailable(RuntimeError):
    """The pipeline database is missing, locked or unreadable."""


def db_path() -> Path:
    return Path(settings.POSINUS_PIPELINE_DB_PATH)


@contextmanager
def connection():
    """Read-only connection to the pipeline DB, or `PipelineUnavailable`.

    Opened with the `ro` URI mode as well as the pragma: the mode refuses at
    connect time if the file is missing, instead of quietly creating an empty
    database that would then diverge from the real one forever.
    """
    path = db_path()
    try:
        present = path.exists()
    except OSError as exc:
        # Even asking can fail: the directory above is traversable only for the
        # group, and this process may not be in it.
        raise PipelineUnavailable(str(exc)) from exc
    if not present:
        raise PipelineUnavailable(f"{path} does not exist")
    con = None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        con.row_factory = sqlite3.Row
        con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        con.execute("PRAGMA query_only = ON")
        yield con
    except sqlite3.Error as exc:
        raise PipelineUnavailable(str(exc)) from exc
    finally:
        if con is not None:
            con.close()


def fetch_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Rows for a query, or `PipelineUnavailable` — never a half-open connection."""
    with connection() as con:
        try:
            return con.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            # A missing table is normal on a host running older pipeline code.
            raise PipelineUnavailable(str(exc)) from exc


def is_available() -> bool:
    try:
        with connection() as con:
            con.execute("SELECT 1")
        return True
    except PipelineUnavailable as exc:
        log.debug("pipeline database unavailable: %s", exc)
        return False
