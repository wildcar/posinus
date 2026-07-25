"""Notifications: loud about what is broken, silent about everything else."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify
import publisher

SCHEMA = """
CREATE TABLE prepared_item (
    news_id INTEGER PRIMARY KEY, status TEXT NOT NULL, retold_title TEXT,
    retold_body_md TEXT, model_id TEXT, prepared_at TEXT, published_at TEXT, error TEXT
);
CREATE TABLE publication (
    news_id INTEGER NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL, url TEXT,
    error TEXT, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT,
    PRIMARY KEY (news_id, platform)
);
"""


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "own.sqlite3")
        con = sqlite3.connect(self.path)
        con.executescript(SCHEMA)
        con.commit()
        con.close()
        self.con = notify.open_db(self.path)
        self.now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
        self.cfg = publisher.PublisherConfig(own_db=self.path, window_start="")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _publication(self, news_id, platform, status, attempts=1, when="2026-07-25T11:00:00+00:00", error=""):
        self.con.execute(
            "INSERT INTO publication (news_id, platform, status, error, attempts, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (news_id, platform, status, error, attempts, when),
        )
        self.con.commit()

    def _prepared(self, news_id, status="prepared", prepared_at="2026-07-25T09:00:00+00:00", published_at=None):
        self.con.execute(
            "INSERT INTO prepared_item (news_id, status, prepared_at, published_at) VALUES (?, ?, ?, ?)",
            (news_id, status, prepared_at, published_at),
        )
        self.con.commit()

    def test_a_platform_failing_three_times_is_an_alarm(self):
        self._prepared(9)  # a non-empty queue, so only the platform alarm can fire
        self._publication(3, "telegram", "ok", when="2026-07-25T11:30:00+00:00")
        self._publication(1, "vk", "error", attempts=4, error="код 214")
        self._publication(2, "telegram", "error", attempts=1)  # one failure is not news

        alarms = notify.collect_alarms(self.con, self.cfg, self.now)

        self.assertEqual([alarm.kind for alarm in alarms], ["platform:vk"])
        self.assertIn("214", alarms[0].text)

    def test_a_silent_day_inside_an_open_window_is_an_alarm(self):
        self._prepared(1)
        self._publication(2, "telegram", "ok", when="2026-07-24T06:00:00+00:00")

        alarms = notify.collect_alarms(self.con, self.cfg, self.now)

        self.assertEqual([alarm.kind for alarm in alarms], ["silence"])
        self.assertIn("в очереди 1", alarms[0].text)

    def test_a_closed_window_is_not_a_silent_day(self):
        """At night the channel is supposed to be quiet."""
        self._prepared(1)
        self._publication(2, "telegram", "ok", when="2026-07-24T06:00:00+00:00")
        night = publisher.PublisherConfig(
            own_db=self.path, window_start="08:00", window_end="22:00", window_tz="UTC"
        )

        alarms = notify.collect_alarms(self.con, night, datetime(2026, 7, 25, 3, 0, tzinfo=timezone.utc))

        self.assertEqual(alarms, [])

    def test_an_empty_queue_is_an_editorial_failure_too(self):
        alarms = notify.collect_alarms(self.con, self.cfg, self.now)

        self.assertEqual([alarm.kind for alarm in alarms], ["empty-queue"])
        self.assertIn("отбор", alarms[0].text)

    def test_nothing_is_wrong_means_nothing_is_sent(self):
        self._prepared(1)
        self._publication(2, "telegram", "ok", when="2026-07-25T11:30:00+00:00")

        self.assertEqual(notify.collect_alarms(self.con, self.cfg, self.now), [])

    def test_the_same_alarm_is_not_repeated_every_hour(self):
        notify.remember(self.con, "platform:vk", "old text", self.now - timedelta(hours=2))

        self.assertTrue(notify.recently_sent(self.con, "platform:vk", self.now))
        self.assertFalse(notify.recently_sent(self.con, "platform:vk", self.now + timedelta(hours=13)))
        self.assertFalse(notify.recently_sent(self.con, "silence", self.now))

    def test_digest_is_one_sentence(self):
        yesterday = (self.now - timedelta(days=1)).replace(hour=10)
        self._prepared(1, status="published", published_at=yesterday.isoformat())
        self._prepared(2)

        text = notify.digest_text(self.con, self.now)

        self.assertEqual(text, "Вчера вышел 1 пост. Сейчас проблем нет, в очереди 1.")

    def test_without_a_chat_id_nothing_is_sent(self):
        """Diagnostics must never fall out into the public channel by default."""
        with mock.patch.dict("os.environ", {"NOTIFY_CHAT_ID": "", "TELEGRAM_BOT_TOKEN": "t"}, clear=False):
            with mock.patch.object(notify, "send") as sender:
                notify.run(self.cfg, "check", dry_run=False)

        sender.assert_not_called()

    def test_a_failing_send_is_not_fatal(self):
        with mock.patch.object(publisher, "http_send", side_effect=OSError("network down")):
            self.assertFalse(notify.send("token", "chat", "text"))


if __name__ == "__main__":
    unittest.main()
