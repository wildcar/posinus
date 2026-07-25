"""Operator edits arrive as files and must never be lost silently."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import apply_edits
import preparer


class ApplyEditsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.requests = Path(self.tmp.name) / "requests"
        self.requests.mkdir()
        self.own_path = str(Path(self.tmp.name) / "own.sqlite3")
        con = preparer.open_own_db(self.own_path)
        con.execute(
            "INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md) "
            "VALUES (1, 'prepared', 'Спас троих', '# Спас троих\n\nтекст')"
        )
        for position, name in enumerate(("a.jpg", "b.jpg", "c.jpg")):
            con.execute(
                "INSERT INTO illustration (news_id, position, file_path) VALUES (1, ?, ?)",
                (position, f"/media/1/{name}"),
            )
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _request(self, payload, name="edit-1.json"):
        (self.requests / name).write_text(json.dumps(payload), encoding="utf-8")

    def _rows(self, sql, params=()):
        con = sqlite3.connect(self.own_path)
        con.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in con.execute(sql, params)]
        finally:
            con.close()

    def test_the_text_is_replaced_and_marked_as_edited(self):
        self._request({"news_id": 1, "title": "Спас четверых", "body": "правленый текст",
                       "operator": "wildcar"})

        apply_edits.run(str(self.requests), self.own_path)

        row = self._rows("SELECT * FROM prepared_item WHERE news_id = 1")[0]
        self.assertEqual(row["retold_title"], "Спас четверых")
        self.assertEqual(row["retold_body_md"], "правленый текст")
        self.assertTrue(row["edited_at"])
        self.assertEqual(row["edited_by"], "wildcar")
        self.assertFalse(list(self.requests.iterdir()))  # the request is consumed

    def test_an_edited_item_is_never_regenerated(self):
        """The whole point of the flag: a later failure must not undo a human fix."""
        self._request({"news_id": 1, "title": "Правка", "body": "текст"})
        apply_edits.run(str(self.requests), self.own_path)

        con = preparer.open_own_db(self.own_path)
        con.execute("UPDATE prepared_item SET status = 'error' WHERE news_id = 1")
        con.commit()
        try:
            self.assertIn(1, preparer.prepared_ids(con))
        finally:
            con.close()

    def test_choosing_the_lead_picture_reorders_the_rest(self):
        second = self._rows("SELECT id FROM illustration ORDER BY position")[1]["id"]
        self._request({"news_id": 1, "lead_image_id": second})

        apply_edits.run(str(self.requests), self.own_path)

        rows = self._rows("SELECT id, position FROM illustration ORDER BY position")
        self.assertEqual(rows[0]["id"], second)
        self.assertEqual([row["position"] for row in rows], [0, 1, 2])

    def test_dropping_a_picture_removes_it(self):
        unwanted = self._rows("SELECT id FROM illustration ORDER BY position")[2]["id"]
        self._request({"news_id": 1, "drop_image_ids": [unwanted]})

        apply_edits.run(str(self.requests), self.own_path)

        self.assertEqual(len(self._rows("SELECT id FROM illustration")), 2)

    def test_a_published_item_is_refused(self):
        con = preparer.open_own_db(self.own_path)
        con.execute("UPDATE prepared_item SET status = 'published' WHERE news_id = 1")
        con.commit()
        con.close()
        self._request({"news_id": 1, "title": "поздно"})

        apply_edits.run(str(self.requests), self.own_path)

        row = self._rows("SELECT retold_title, edited_at FROM prepared_item WHERE news_id = 1")[0]
        self.assertEqual(row["retold_title"], "Спас троих")
        self.assertIsNone(row["edited_at"])

    def test_a_request_for_an_unknown_item_is_refused_not_crashing(self):
        self._request({"news_id": 999, "title": "нет такой"}, name="edit-999.json")

        self.assertEqual(apply_edits.run(str(self.requests), self.own_path), 0)
        self.assertFalse(list(self.requests.iterdir()))

    def test_a_broken_file_is_thrown_away_rather_than_looping(self):
        (self.requests / "edit-2.json").write_text("{not json", encoding="utf-8")

        apply_edits.run(str(self.requests), self.own_path)

        self.assertFalse(list(self.requests.iterdir()))


if __name__ == "__main__":
    unittest.main()
