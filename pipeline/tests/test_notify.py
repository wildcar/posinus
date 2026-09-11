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

import daypic
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

    def _daypic_tables(self):
        """daypic.py owns these; notify only reads them, so borrow its schema."""
        self.con.executescript(daypic.OWN_SCHEMA_SQL)
        self.con.commit()

    def _daypic_item(self, day, status, attempts=1, error=None, slot="day", title="Картина дня"):
        cur = self.con.execute(
            "INSERT INTO daypic_item (day, slot, status, title, attempts, error, file_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (day, slot, status, title, attempts, error, "/x.jpg" if status == "published" else None),
        )
        self.con.commit()
        return cur.lastrowid

    def _daypic_publication(self, item_id, platform, status, attempts=1, when="2026-07-25T11:00:00+00:00", error=""):
        self.con.execute(
            "INSERT INTO daypic_publication (item_id, platform, status, error, attempts, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, platform, status, error, attempts, when),
        )
        self.con.commit()

    def _quiet_news(self):
        """A healthy news side, so only the daypic alarms can fire."""
        self._prepared(9)
        self._publication(3, "telegram", "ok", when="2026-07-25T11:30:00+00:00")

    def test_a_platform_failing_three_times_is_an_alarm(self):
        self._prepared(9)  # a non-empty queue, so only the platform alarm can fire
        self._publication(3, "telegram", "ok", when="2026-07-25T11:30:00+00:00")
        self._publication(1, "vk", "error", attempts=4, error="код 214")
        self._publication(2, "telegram", "error", attempts=1)  # one failure is not news

        alarms = notify.collect_alarms(self.con, self.cfg, self.now)

        self.assertEqual([alarm.kind for alarm in alarms], ["platform:vk"])
        self.assertIn("214", alarms[0].text)
        self.assertIn("ВКонтакте", alarms[0].text)

    def test_an_old_failure_is_a_dead_tail_not_an_alarm(self):
        """Prod carries a VK row from before the token was fixed: 24 attempts, days old."""
        self._prepared(9)
        self._publication(3, "telegram", "ok", when="2026-07-25T11:30:00+00:00")
        self._publication(1, "vk", "error", attempts=24, when="2026-07-20T10:00:00+00:00", error="код 27")

        self.assertEqual(notify.collect_alarms(self.con, self.cfg, self.now), [])

    def test_a_platform_accepting_posts_again_is_not_an_alarm(self):
        """2026-09-11, 07:09 MSK: «ВКонтакте не принимает посты» 19 hours after the last refusal
        and 11 accepted posts later — the dead token's given-up rows were still inside the window."""
        self._prepared(9)
        self._publication(1, "vk", "error", attempts=8, when="2026-07-25T08:45:00+00:00", error="9 Flood control")
        self._publication(2, "vk", "ok", when="2026-07-25T09:40:00+00:00")

        self.assertEqual(notify.collect_alarms(self.con, self.cfg, self.now), [])

    def test_a_refusal_after_the_last_accepted_post_is_still_an_alarm(self):
        self._prepared(9)
        self._publication(2, "vk", "ok", when="2026-07-25T09:40:00+00:00")
        self._publication(1, "vk", "error", attempts=3, when="2026-07-25T11:00:00+00:00", error="код 214")

        alarms = notify.collect_alarms(self.con, self.cfg, self.now)

        self.assertEqual([alarm.kind for alarm in alarms], ["platform:vk"])

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

    def test_a_daypic_day_given_up_is_an_alarm(self):
        """2026-09-05: four failed generations, nobody told, the same again next morning."""
        self._quiet_news()
        self._daypic_tables()
        self._daypic_item("2026-07-25", "error", attempts=4, error="Codex backend error HTTP 400")

        alarms = notify.collect_alarms(self.con, self.cfg, self.now)

        self.assertEqual([alarm.kind for alarm in alarms], ["daypic:day"])
        self.assertIn("Картина дня за 25 июля 2026 не вышла", alarms[0].text)
        self.assertIn("4 попыток", alarms[0].text)
        self.assertIn("HTTP 400", alarms[0].text)

    def test_a_daypic_failure_still_retrying_is_not_an_alarm(self):
        """The timer retries every 15 minutes; one bad call heals itself."""
        self._quiet_news()
        self._daypic_tables()
        self._daypic_item("2026-07-25", "error", attempts=1, error="timeout")

        self.assertEqual(notify.collect_alarms(self.con, self.cfg, self.now), [])

    def test_a_later_issue_silences_the_old_daypic_failure(self):
        self._quiet_news()
        self._daypic_tables()
        self._daypic_item("2026-07-24", "error", attempts=4, error="HTTP 400")
        self._daypic_item("2026-07-25", "published")

        self.assertEqual(notify.collect_alarms(self.con, self.cfg, self.now), [])

    def test_a_stale_daypic_failure_is_not_an_alarm(self):
        """A slot switched off after a bad morning must not report it forever."""
        self._quiet_news()
        self._daypic_tables()
        self._daypic_item("2026-07-10", "error", attempts=4, error="HTTP 400")

        self.assertEqual(notify.collect_alarms(self.con, self.cfg, self.now), [])

    def test_a_platform_refusing_the_daypic_is_an_alarm(self):
        self._quiet_news()
        self._daypic_tables()
        item = self._daypic_item("2026-07-25", "generated")
        self._daypic_publication(item, "telegram", "ok")
        self._daypic_publication(item, "vk", "error", attempts=3, error="код 214")

        alarms = notify.collect_alarms(self.con, self.cfg, self.now)

        self.assertEqual([alarm.kind for alarm in alarms], ["daypic-platform:vk"])
        self.assertIn("ВКонтакте не принимает картину дня за 25 июля 2026", alarms[0].text)
        self.assertIn("214", alarms[0].text)

    def test_a_daypic_accepted_after_the_refusal_silences_the_platform_alarm(self):
        self._quiet_news()
        self._daypic_tables()
        old = self._daypic_item("2026-07-24", "published")
        self._daypic_publication(old, "vk", "error", attempts=8, when="2026-07-24T14:00:00+00:00",
                                 error="9 Flood control")
        new = self._daypic_item("2026-07-25", "published")
        self._daypic_publication(new, "vk", "ok", when="2026-07-25T05:15:00+00:00")

        self.assertEqual(notify.collect_alarms(self.con, self.cfg, self.now), [])

    def test_without_daypic_tables_nothing_breaks(self):
        """An installation that never ran daypic.py has no such tables."""
        self._quiet_news()

        self.assertEqual(notify.collect_alarms(self.con, self.cfg, self.now), [])

    def test_digest_names_a_missed_picture(self):
        yesterday = (self.now - timedelta(days=1)).replace(hour=10)
        self._prepared(1, status="published", published_at=yesterday.isoformat())
        self._prepared(2)
        self._daypic_tables()
        self._daypic_item(yesterday.date().isoformat(), "error", attempts=4, error="HTTP 400")

        text = notify.digest_text(self.con, self.now)

        self.assertEqual(text, "Вчера вышел 1 пост. Не вышла: Картина дня. Сейчас проблем нет, в очереди 1.")

    def test_digest_says_nothing_about_a_published_picture(self):
        yesterday = (self.now - timedelta(days=1)).replace(hour=10)
        self._prepared(1, status="published", published_at=yesterday.isoformat())
        self._prepared(2)
        self._daypic_tables()
        self._daypic_item(yesterday.date().isoformat(), "published")

        self.assertEqual(notify.digest_text(self.con, self.now),
                         "Вчера вышел 1 пост. Сейчас проблем нет, в очереди 1.")

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
