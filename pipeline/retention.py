#!/usr/bin/env python3
"""How long the pipeline keeps the pictures it downloaded.

Nothing was ever deleted here. The crawler purges its own news at 90 and at 3
days; the pipeline kept every illustration of every candidate forever, and the
media directory grew by about forty megabytes a day — a linear line with nothing
at the end of it.

Two clocks, because the two kinds of file are worth different things:

- a candidate that waited too long for its turn loses its pictures once the
  publisher has taken it off the queue (`status = 'expired'`, past
  `PUB_EXPIRE_AFTER_DAYS`, ten days). There are far more of these than of
  anything else and nobody will ever look at them;
- a published item keeps its pictures for `KEEP_PUBLISHED_DAYS`. The copies that
  matter are already in Telegram and VK; the local file only serves the operator
  screen, and a month of history is more than the screen shows.

The order of those two is the safety rail. This module never touches a
`prepared` row — it deletes files only for items that are already out of the
queue — so «картинки удалили, а новость всё ещё выйдет» cannot happen no matter
when the two timers fire relative to each other. Taking an item off the queue is
the publisher's job, not this one's: the queue belongs to the service that reads
it, and doing it there means a stale item stops being publishable within the
hour rather than at half past three.

What is never deleted is the rows. A thousand news items cost a quarter of a
megabyte, and they are the whole history: what was prepared, what went out and
when, what the feed was made of. The moment a retention period touches them,
«Состав ленты за 30 дней» starts lying.

So a purged item keeps its `prepared_item` row and gets `images_purged_at`; its
`illustration` rows go, because a row pointing at a file that is not there is
worse than no row — the operator screen would offer a picture and show a hole.

Single-file, stdlib-only, like every script here.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import preparer
import runlog

log = logging.getLogger("posinus-retention")

# How long a published item keeps its pictures. There is deliberately no second
# number here for unpublished ones: that period is `PUB_EXPIRE_AFTER_DAYS` in the
# publisher, which owns the queue, and one rule with two copies drifts apart.
KEEP_PUBLISHED_DAYS = 30

# Only items that are out of the queue for good. This is the safety rail of the
# whole design: a `prepared` row is still publishable, so its pictures are never
# touched here, and a `published` one cannot be published a second time. The
# publisher is what takes a stale item off the queue (`status = 'expired'`), and
# only after that may its files go — so the boundary case «картинок уже нет, а
# новость ещё выйдет» cannot happen, whatever order the two timers fire in.
#
# `expired` needs no date test: the publisher only sets it past the same period.
DUE_SQL = """
SELECT news_id, status, prepared_at, published_at, expired_at
FROM prepared_item
WHERE images_purged_at IS NULL
  AND ((status = 'published' AND published_at IS NOT NULL AND published_at < :published_before)
       OR status = 'expired')
"""


# How long the daily pictures live. One file a day means megabytes a month,
# so the period is generous; the rows stay forever, like everything else here,
# and the gallery says «файл удалён по сроку» instead of showing a hole.
DAYPIC_KEEP_DAYS = 90

# Issues whose picture files are past their date and not yet purged. Both
# renditions of one issue (vertical + horizontal) age together.
DAYPIC_DUE_SQL = """
SELECT id, day, slot, file_path, file_path_wide
FROM daypic_item
WHERE file_purged_at IS NULL
  AND (file_path IS NOT NULL OR file_path_wide IS NOT NULL)
  AND day < :day_before
"""


@dataclass
class RetentionConfig:
    own_db: str = "/var/lib/posinus/pipeline/evaluator.sqlite3"
    media_dir: str = "/var/lib/posinus/pipeline/media"
    keep_published_days: int = KEEP_PUBLISHED_DAYS
    daypic_keep_days: int = DAYPIC_KEEP_DAYS

    @classmethod
    def from_env(cls, env: dict[str, str] = os.environ) -> "RetentionConfig":
        cfg = cls()
        cfg.own_db = env.get("EVALUATOR_DB_PATH", cfg.own_db)
        cfg.media_dir = env.get("MEDIA_DIR", cfg.media_dir)
        if value := env.get("KEEP_PUBLISHED_DAYS"):
            cfg.keep_published_days = int(value)
        if value := env.get("DAYPIC_KEEP_DAYS"):
            cfg.daypic_keep_days = int(value)
        return cfg


def due_items(con: sqlite3.Connection, cfg: RetentionConfig, now: datetime) -> list[sqlite3.Row]:
    return con.execute(
        DUE_SQL,
        {"published_before": (now - timedelta(days=cfg.keep_published_days)).isoformat()},
    ).fetchall()


def _directory_size(path: Path) -> int:
    try:
        return sum(entry.stat().st_size for entry in path.iterdir() if entry.is_file())
    except OSError:
        return 0


def purge_item(con: sqlite3.Connection, cfg: RetentionConfig, news_id: int, now: datetime) -> tuple[int, int]:
    """Delete one item's pictures. Returns (files removed, bytes freed).

    The files go first and the rows second: a crash in between leaves rows
    pointing at missing files, which the next run cleans up, whereas the other
    order would leave files nothing remembers — and those are the ones that fill
    a disk unnoticed.
    """
    directory = Path(cfg.media_dir) / str(news_id)
    freed = _directory_size(directory)
    removed = 0
    try:
        removed = sum(1 for entry in directory.iterdir() if entry.is_file())
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass  # already gone; the row still has to be marked
    except OSError as exc:
        log.warning("news %s: cannot remove %s: %s", news_id, directory, exc)
        return 0, 0
    with con:
        con.execute("DELETE FROM illustration WHERE news_id = ?", (news_id,))
        con.execute(
            "UPDATE prepared_item SET images_purged_at = ? WHERE news_id = ?",
            (now.isoformat(timespec="seconds"), news_id),
        )
    return removed, freed


def orphan_directories(con: sqlite3.Connection, cfg: RetentionConfig) -> list[Path]:
    """Media directories no `prepared_item` row knows about.

    They exist: the media directory moved once and the rows were repointed, and a
    failed preparation can leave a directory behind. Nothing will ever ask for
    them again, and nothing else would ever delete them.
    """
    root = Path(cfg.media_dir)
    try:
        directories = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError as exc:
        log.warning("cannot list %s: %s", root, exc)
        return []
    known = {str(row["news_id"]) for row in con.execute("SELECT news_id FROM prepared_item")}
    return [entry for entry in directories if entry.name not in known]


def purge_daypic(con: sqlite3.Connection, cfg: RetentionConfig, now: datetime,
                 dry_run: bool) -> tuple[int, int]:
    """Delete daily pictures past DAYPIC_KEEP_DAYS. Returns (files, bytes).

    Tolerates a database without the daypic tables: older pipeline code never
    created them, and retention must not fail over a feature that is not there.
    Rows survive with `file_purged_at`, so the gallery stays honest.
    """
    day_before = (now - timedelta(days=cfg.daypic_keep_days)).date().isoformat()
    try:
        rows = con.execute(DAYPIC_DUE_SQL, {"day_before": day_before}).fetchall()
    except sqlite3.OperationalError:
        return 0, 0
    removed = freed = 0
    for row in rows:
        paths = [Path(raw) for raw in (row["file_path"], row["file_path_wide"]) if raw]
        if dry_run:
            for path in paths:
                log.info("daypic %s/%s: %s would go", row["day"], row["slot"], path)
            continue
        blocked = False
        for path in paths:
            try:
                freed += path.stat().st_size
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass  # already gone; the row still has to be marked
            except OSError as exc:
                log.warning("daypic %s/%s: cannot remove %s: %s", row["day"], row["slot"], path, exc)
                blocked = True
        if blocked:
            continue
        with con:
            con.execute(
                "UPDATE daypic_item SET file_purged_at = ? WHERE id = ?",
                (now.isoformat(timespec="seconds"), row["id"]),
            )
    return removed, freed


def run(cfg: RetentionConfig, dry_run: bool = False, counters: dict | None = None,
        now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    con = preparer.open_own_db(cfg.own_db)
    try:
        items = due_items(con, cfg, now)
        checked = len(items)
        removed = freed = 0
        for row in items:
            if dry_run:
                directory = Path(cfg.media_dir) / str(row["news_id"])
                log.info("news %s [%s]: %s would go", row["news_id"], row["status"], directory)
                continue
            files, bytes_freed = purge_item(con, cfg, row["news_id"], now)
            removed += files
            freed += bytes_freed

        orphans = 0
        for directory in orphan_directories(con, cfg):
            orphans += 1
            if dry_run:
                log.info("orphan directory %s would go", directory)
                continue
            freed += _directory_size(directory)
            removed += sum(1 for entry in directory.iterdir() if entry.is_file())
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                log.warning("cannot remove orphan %s: %s", directory, exc)

        daypic_removed, daypic_freed = purge_daypic(con, cfg, now, dry_run)
        removed += daypic_removed
        freed += daypic_freed

        freed_mb = round(freed / (1024 * 1024), 1)
        log.info(
            "%s: %d items past their date, %d pictures removed (%d daily), %d orphan directories, %.1f MB freed",
            "dry-run" if dry_run else "finished", checked, removed, daypic_removed, orphans, freed_mb,
        )
        if counters is not None:
            counters.update(checked=checked, removed=removed, orphans=orphans, freed_mb=freed_mb,
                            daypic_removed=daypic_removed)
        return 0
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete pipeline pictures past their retention period")
    parser.add_argument("--dry-run", action="store_true", help="say what would go, delete nothing")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cfg = RetentionConfig.from_env()
    if args.dry_run:
        return run(cfg, dry_run=True)
    config = {
        "media_dir": cfg.media_dir,
        "keep_published_days": cfg.keep_published_days,
        "daypic_keep_days": cfg.daypic_keep_days,
    }
    with runlog.record("retention", cfg.own_db, config) as counters:
        return run(cfg, dry_run=False, counters=counters)


if __name__ == "__main__":
    sys.exit(main())
