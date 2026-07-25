#!/usr/bin/env python3
"""Telegram notifications: a daily line, and an alarm when something is broken.

The most common complaint about this system was that it tells nobody anything —
you learn about a broken platform only by opening the site. There is already a
bot, so a message costs ten lines.

Two kinds, and the difference matters:

- `--digest`: one sentence a day. «Вчера вышло 2 поста, проблем нет.»
- `--check`: an alarm, and only for things that are actually wrong — a platform
  that failed three times running, a whole day with no post inside an open
  window, an empty queue for three days. An empty channel is an editorial
  failure too, and it deserves the same volume as a broken platform.

The bar for an alarm is high on purpose: noise makes the channel worthless in a
week, and then the one message that mattered gets ignored with the rest.

Where it goes: `NOTIFY_CHAT_ID`, which is the owner's own chat — never the public
channel. With no chat id set, nothing is sent and the run says so. Defaulting to
silence is the only safe default here: the alternative is posting diagnostics to
readers.

Repeats are suppressed for `REPEAT_AFTER_HOURS` (12 by default) per alarm kind,
so a platform that stays broken says so twice a day, not every hour.

Single-file, stdlib-only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import publisher
import runlog

log = logging.getLogger("posinus-notify")

REPEAT_AFTER_HOURS = 12
PLATFORM_FAIL_ATTEMPTS = 3
# Only failures this fresh count as an alarm. A platform that is really broken
# keeps failing every half hour, while an old row is a dead tail: prod carries
# one from before the VK token was fixed, 24 attempts, last touched days ago.
# Without this window the first alarm the operator ever got would have been a
# false one, which is how notification channels start being ignored.
PLATFORM_FAIL_WINDOW_HOURS = 24
SILENT_HOURS = 24          # no post at all for this long, inside an open window
EMPTY_QUEUE_DAYS = 3

PLATFORM_TITLES = {"telegram": "Telegram", "site": "wildcar.ru", "vk": "ВКонтакте"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notification (
    kind TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL,
    text TEXT NOT NULL
);
"""


@dataclass
class Alarm:
    kind: str
    text: str


def open_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    con.executescript(SCHEMA_SQL)
    con.commit()
    return con


def send(token: str, chat_id: str, text: str, dry_run: bool = False) -> bool:
    """Send one message. Never raises: a failed notification must not fail a run."""
    if not token or not chat_id:
        log.warning("NOTIFY_CHAT_ID or the bot token is missing; nothing sent")
        return False
    if dry_run:
        log.info("[dry-run] would send: %s", text)
        return True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    try:
        status, body = publisher.http_send(
            url, data, {"Content-Type": "application/x-www-form-urlencoded"}
        )
    except Exception as exc:  # network, DNS, anything
        log.error("notification failed: %s", exc)
        return False
    if status != 200:
        log.error("notification refused with %s: %s", status, body[:200])
        return False
    return True


def _moment(raw) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def recently_sent(con: sqlite3.Connection, kind: str, now: datetime) -> bool:
    row = con.execute("SELECT sent_at FROM notification WHERE kind = ?", (kind,)).fetchone()
    sent_at = _moment(row["sent_at"]) if row else None
    return bool(sent_at and now - sent_at < timedelta(hours=REPEAT_AFTER_HOURS))


def remember(con: sqlite3.Connection, kind: str, text: str, now: datetime) -> None:
    with con:
        con.execute(
            "INSERT INTO notification (kind, sent_at, text) VALUES (?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET sent_at = excluded.sent_at, text = excluded.text",
            (kind, now.isoformat(timespec="seconds"), text),
        )


def collect_alarms(con: sqlite3.Connection, cfg: publisher.PublisherConfig, now: datetime) -> list[Alarm]:
    """Only what a person has to act on. Everything else belongs on the screen."""
    alarms: list[Alarm] = []

    fresh = (now - timedelta(hours=PLATFORM_FAIL_WINDOW_HOURS)).isoformat()
    for row in con.execute(
        "SELECT platform, COUNT(*) AS items, MAX(attempts) AS attempts, MAX(error) AS error "
        "FROM publication WHERE status = 'error' AND attempts >= ? AND updated_at >= ? "
        "GROUP BY platform",
        (PLATFORM_FAIL_ATTEMPTS, fresh),
    ):
        title = PLATFORM_TITLES.get(row["platform"], row["platform"])
        alarms.append(
            Alarm(
                kind=f"platform:{row['platform']}",
                text=(
                    f"{title} не принимает посты: неудачных отправок {row['items']}, "
                    f"попыток до {row['attempts']}. Последняя ошибка: {row['error'] or 'без текста'}"
                ),
            )
        )

    queue_size = con.execute(
        "SELECT COUNT(*) AS count FROM prepared_item WHERE status = 'prepared'"
    ).fetchone()["count"]
    last_ok = _moment(
        (con.execute("SELECT MAX(updated_at) AS last FROM publication WHERE status = 'ok'").fetchone() or {})["last"]
    )
    window_open, _ = publisher.window_state(cfg, now)

    if window_open and last_ok and now - last_ok > timedelta(hours=SILENT_HOURS):
        alarms.append(
            Alarm(
                kind="silence",
                text=(
                    f"Сутки без публикаций, хотя окно открыто. Последний пост "
                    f"{last_ok:%d.%m %H:%M} UTC, в очереди {queue_size}."
                ),
            )
        )

    if queue_size == 0:
        last_prepared = _moment(
            (con.execute("SELECT MAX(prepared_at) AS last FROM prepared_item").fetchone() or {})["last"]
        )
        if last_prepared is None or now - last_prepared > timedelta(days=EMPTY_QUEUE_DAYS):
            alarms.append(
                Alarm(
                    kind="empty-queue",
                    text=(
                        f"Очередь пуста {EMPTY_QUEUE_DAYS}-й день: публиковать нечего. "
                        "Стоит посмотреть отбор — возможно, профиль стал слишком строгим."
                    ),
                )
            )
    return alarms


def digest_text(con: sqlite3.Connection, now: datetime) -> str:
    """Yesterday in one sentence: what went out, and whether anything is broken."""
    start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    published = con.execute(
        "SELECT COUNT(*) AS count FROM prepared_item "
        "WHERE status = 'published' AND published_at >= ? AND published_at < ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()["count"]
    queue_size = con.execute(
        "SELECT COUNT(*) AS count FROM prepared_item WHERE status = 'prepared'"
    ).fetchone()["count"]
    broken = con.execute(
        "SELECT COUNT(DISTINCT platform) AS count FROM publication "
        "WHERE status = 'error' AND attempts >= ? AND updated_at >= ?",
        (PLATFORM_FAIL_ATTEMPTS, (now - timedelta(hours=PLATFORM_FAIL_WINDOW_HOURS)).isoformat()),
    ).fetchone()["count"]

    posts = f"вышло {published} постов" if published != 1 else "вышел 1 пост"
    state = f"проблем нет, в очереди {queue_size}" if not broken else f"площадок с ошибками: {broken}"
    return f"Вчера {posts}. Сейчас {state}."


def run(cfg: publisher.PublisherConfig, mode: str, dry_run: bool) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("NOTIFY_CHAT_ID", "")
    if not chat_id:
        log.warning(
            "NOTIFY_CHAT_ID is not set: notifications stay off. It must be the owner's own "
            "chat, never the public channel."
        )
        return 0

    now = datetime.now(timezone.utc)
    con = open_db(cfg.own_db)
    try:
        if mode == "digest":
            text = digest_text(con, now)
            sent = send(token, chat_id, text, dry_run)
            log.info("digest %s: %s", "sent" if sent else "not sent", text)
            return 0

        alarms = collect_alarms(con, cfg, now)
        sent_count = 0
        for alarm in alarms:
            if recently_sent(con, alarm.kind, now):
                log.info("alarm %s suppressed: already sent within %dh", alarm.kind, REPEAT_AFTER_HOURS)
                continue
            if send(token, chat_id, alarm.text, dry_run):
                sent_count += 1
                if not dry_run:
                    remember(con, alarm.kind, alarm.text, now)
        log.info("alarms: %d found, %d sent", len(alarms), sent_count)
        return 0
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram notifications for the operator.")
    parser.add_argument("--digest", action="store_true", help="send the daily one-line summary")
    parser.add_argument("--dry-run", action="store_true", help="build the message and log it, send nothing")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cfg = publisher.PublisherConfig.from_env()
    mode = "digest" if args.digest else "check"
    if args.dry_run:
        return run(cfg, mode, dry_run=True)
    with runlog.record(f"notify-{mode}", cfg.own_db, {"mode": mode}):
        return run(cfg, mode, dry_run=False)


if __name__ == "__main__":
    sys.exit(main())
