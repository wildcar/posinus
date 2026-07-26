"""Retention deletes files and never deletes history."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import preparer
import retention

NOW = datetime(2026, 8, 1, 3, 30, tzinfo=timezone.utc)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.media = Path(self.tmp.name) / "media"
        self.media.mkdir()
        self.own_path = str(Path(self.tmp.name) / "own.sqlite3")
        self.cfg = retention.RetentionConfig(own_db=self.own_path, media_dir=str(self.media))
        preparer.open_own_db(self.own_path).close()

    def tearDown(self):
        self.tmp.cleanup()

    def _item(self, news_id, status, age_days, images=2):
        moment = (NOW - timedelta(days=age_days)).isoformat()
        con = preparer.open_own_db(self.own_path)
        con.execute(
            "INSERT INTO prepared_item (news_id, status, retold_title, prepared_at, published_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (news_id, status, f"Новость {news_id}", moment, moment if status == "published" else None),
        )
        directory = self.media / str(news_id)
        directory.mkdir(exist_ok=True)
        for position in range(images):
            path = directory / f"{position}.jpg"
            path.write_bytes(b"x" * 1024)
            con.execute(
                "INSERT INTO illustration (news_id, position, file_path) VALUES (?, ?, ?)",
                (news_id, position, str(path)),
            )
        con.commit()
        con.close()

    def _rows(self, sql, params=()):
        con = sqlite3.connect(self.own_path)
        con.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in con.execute(sql, params)]
        finally:
            con.close()

    def _run(self, **kwargs):
        counters = {}
        retention.run(self.cfg, counters=counters, now=NOW, **kwargs)
        return counters

    def test_a_candidate_older_than_ten_days_loses_its_pictures(self):
        self._item(1, "prepared", age_days=11)

        counters = self._run()

        self.assertFalse((self.media / "1").exists())
        self.assertEqual(counters["removed"], 2)
        self.assertEqual(self._rows("SELECT * FROM illustration"), [])

    def test_the_row_and_its_history_stay(self):
        """The whole point: «Состав ленты» is built on these rows."""
        self._item(1, "published", age_days=40)

        self._run()

        row = self._rows("SELECT * FROM prepared_item WHERE news_id = 1")[0]
        self.assertEqual(row["retold_title"], "Новость 1")
        self.assertEqual(row["status"], "published")
        self.assertTrue(row["images_purged_at"])

    def test_a_fresh_candidate_is_left_alone(self):
        self._item(1, "prepared", age_days=9)

        counters = self._run()

        self.assertTrue((self.media / "1" / "0.jpg").exists())
        self.assertEqual(counters["checked"], 0)

    def test_a_published_item_keeps_its_pictures_for_thirty_days(self):
        """Ten days is for the pile nobody looks at; a published post is different."""
        self._item(1, "published", age_days=20)
        self._item(2, "published", age_days=31)

        self._run()

        self.assertTrue((self.media / "1" / "0.jpg").exists())
        self.assertFalse((self.media / "2").exists())

    def test_dry_run_deletes_nothing(self):
        self._item(1, "prepared", age_days=30)

        self._run(dry_run=True)

        self.assertTrue((self.media / "1" / "0.jpg").exists())
        self.assertIsNone(self._rows("SELECT images_purged_at FROM prepared_item")[0]["images_purged_at"])

    def test_a_purged_item_is_not_visited_twice(self):
        self._item(1, "prepared", age_days=30)
        self._run()

        counters = self._run()

        self.assertEqual(counters["checked"], 0)

    def test_an_item_still_being_prepared_has_no_clock_yet(self):
        con = preparer.open_own_db(self.own_path)
        con.execute("INSERT INTO prepared_item (news_id, status) VALUES (7, 'pending')")
        con.commit()
        con.close()

        self.assertEqual(self._run()["checked"], 0)

    def test_a_directory_no_row_knows_about_goes(self):
        """The media directory moved once; nothing else would ever collect these."""
        orphan = self.media / "999"
        orphan.mkdir()
        (orphan / "0.jpg").write_bytes(b"x" * 2048)

        counters = self._run()

        self.assertFalse(orphan.exists())
        self.assertEqual(counters["orphans"], 1)

    def test_missing_files_still_close_the_item(self):
        """A crash between the files and the rows must not loop forever."""
        self._item(1, "prepared", age_days=30)
        for path in (self.media / "1").iterdir():
            path.unlink()
        (self.media / "1").rmdir()

        self._run()

        self.assertTrue(self._rows("SELECT images_purged_at FROM prepared_item")[0]["images_purged_at"])

    def test_the_periods_can_be_set_from_the_environment(self):
        cfg = retention.RetentionConfig.from_env(
            {"KEEP_UNPUBLISHED_DAYS": "3", "KEEP_PUBLISHED_DAYS": "7"}
        )
        self.assertEqual((cfg.keep_unpublished_days, cfg.keep_published_days), (3, 7))


if __name__ == "__main__":
    unittest.main()
