"""One row per run: «что делала машина» has to survive a crash too."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runlog


class RunLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "own.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in con.execute("SELECT * FROM service_run ORDER BY id")]
        finally:
            con.close()

    def test_successful_run_is_recorded_with_its_counters(self):
        with runlog.record("preparer", self.path, {"batch": 5, "model": "m"}) as counters:
            counters.update(prepared=3, failed=0)

        row = self._rows()[0]
        self.assertEqual(row["service"], "preparer")
        self.assertEqual(row["status"], "ok")
        self.assertTrue(row["finished_at"])
        self.assertEqual(json.loads(row["counters"]), {"prepared": 3, "failed": 0})
        self.assertEqual(json.loads(row["config"]), {"batch": 5, "model": "m"})

    def test_a_crash_closes_the_row_and_keeps_the_exception(self):
        with self.assertRaises(RuntimeError):
            with runlog.record("publisher", self.path, {}) as counters:
                counters["published"] = 1
                raise RuntimeError("platform exploded")

        row = self._rows()[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("platform exploded", row["error"])
        self.assertEqual(json.loads(row["counters"]), {"published": 1})

    def test_an_unwritable_path_does_not_break_the_run(self):
        """A service that cannot write its diary still does its job."""
        Path(self.path).write_text("not a directory", encoding="utf-8")
        unwritable = str(Path(self.path) / "nested.sqlite3")

        with runlog.record("evaluator", unwritable, {}) as counters:
            counters["evaluated"] = 2

        self.assertFalse(Path(unwritable).exists())

    def test_schema_is_created_on_an_existing_database(self):
        sqlite3.connect(self.path).close()

        con = runlog.open_runlog(self.path)
        try:
            self.assertIsNotNone(con)
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        self.assertIn("service_run", tables)

    def test_every_service_has_a_staleness_bound(self):
        """A row left on 'running' by a killed process must be judgeable."""
        self.assertEqual(
            set(runlog.STALE_AFTER_SECONDS),
            {"evaluator", "preparer", "publisher", "evaluator-backfill", "notify-check", "notify-digest"},
        )


if __name__ == "__main__":
    unittest.main()
