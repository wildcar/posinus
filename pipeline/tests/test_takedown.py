"""«Снять новость»: every platform is tried, and the record says what is still public."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import publisher
import takedown
from publisher import PublishError, PublisherConfig

BODY = "# Замок с затонувшего корабля\n\nТекст.\n\nИсточник: [BBC](https://bbc.com/x)"


class TakedownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = PublisherConfig()
        self.cfg.own_db = str(root / "own.sqlite3")
        self.cfg.requests_dir = str(root / "requests")
        self.cfg.wildcar_content_dir = str(root / "site")
        self.cfg.wildcar_base = "https://wildcar.org"
        Path(self.cfg.requests_dir).mkdir()
        con = publisher.open_own_db(self.cfg.own_db)
        for news_id, status in ((1, "published"), (2, "published"), (3, "prepared")):
            con.execute("INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md) "
                        "VALUES (?, ?, 'Замок', ?)", (news_id, status, BODY))
        rows = [
            (1, "wildcar_org", "ok", "https://wildcar.org/news/1/"),
            (1, "telegram", "ok", "https://t.me/posinus/1422"),
            (1, "vk", "ok", "https://vk.ru/wall-233237778_965"),
            (1, "site", "error", None),
            (2, "wildcar_org", "ok", "https://wildcar.org/news/2/"),
            (3, "telegram", "ok", "https://t.me/posinus/1500"),
        ]
        for news_id, platform, status, url in rows:
            con.execute("INSERT INTO publication (news_id, platform, status, url, attempts, updated_at) "
                        "VALUES (?, ?, ?, ?, 1, '2026-11-08T14:00:00+00:00')", (news_id, platform, status, url))
        con.commit()
        con.close()
        for news_id in (1, 2):
            page = Path(self.cfg.wildcar_content_dir) / "news" / str(news_id)
            page.mkdir(parents=True)
            (page / "index.md").write_text("# Замок", encoding="utf-8")
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _fake(self, platform, fail=None):
        def remover(cfg, news_id, url):
            self.calls.append((platform, news_id, url))
            if fail:
                raise PublishError(fail)
        return remover

    def _removers(self, **failures):
        removers = {p: self._fake(p, failures.get(p)) for p in ("telegram", "vk", "site")}
        removers["wildcar_org"] = takedown.remove_wildcar_org
        return removers

    def _rows(self, news_id):
        con = sqlite3.connect(self.cfg.own_db)
        con.row_factory = sqlite3.Row
        try:
            return {r["platform"]: dict(r) for r in con.execute(
                "SELECT * FROM publication WHERE news_id = ?", (news_id,))}
        finally:
            con.close()

    def _request(self, payload, news_id=1):
        (Path(self.cfg.requests_dir) / f"takedown-{news_id}.json").write_text(
            json.dumps(payload), encoding="utf-8")

    def test_every_live_platform_is_taken_down_and_marked_removed(self):
        self._request({"news_id": 1, "operator": "wildcar", "reason": "решение редактора"})

        takedown.run(self.cfg, removers=self._removers())

        rows = self._rows(1)
        for platform in ("wildcar_org", "telegram", "vk"):
            self.assertEqual(rows[platform]["status"], "removed", platform)
            self.assertEqual(rows[platform]["error"], "решение редактора (wildcar)")
        self.assertEqual(rows["site"]["status"], "error")   # never went out: untouched
        self.assertEqual([c[0] for c in self.calls], ["telegram", "vk"])
        self.assertFalse(list(Path(self.cfg.requests_dir).glob("takedown-*.json")))

    def test_wildcar_org_page_goes_and_the_index_and_feed_forget_it(self):
        self._request({"news_id": 1, "operator": "wildcar"})

        takedown.run(self.cfg, removers=self._removers())

        section = Path(self.cfg.wildcar_content_dir) / "news"
        self.assertFalse((section / "1").exists())
        self.assertTrue((section / "2").exists())
        index = (section / "index.md").read_text(encoding="utf-8")
        feed = (section / "rss.xml").read_text(encoding="utf-8")
        self.assertIn("2/index.md", index)
        self.assertNotIn("1/index.md", index)
        self.assertIn("/news/2/", feed)
        self.assertNotIn("/news/1/", feed)
        self.assertTrue((Path(self.cfg.requests_dir) / publisher.WILDCAR_REBUILD_MARKER).exists())

    def test_a_platform_that_refuses_stays_ok_with_the_reason_and_is_retried_alone(self):
        self._request({"news_id": 1, "operator": "wildcar"})
        takedown.run(self.cfg, removers=self._removers(telegram="message can't be deleted"))

        rows = self._rows(1)
        self.assertEqual(rows["telegram"]["status"], "ok")
        self.assertIn("снять не удалось", rows["telegram"]["error"])
        self.assertEqual(rows["vk"]["status"], "removed")

        self.calls.clear()
        self._request({"news_id": 1, "operator": "wildcar"})
        takedown.run(self.cfg, removers=self._removers())
        self.assertEqual([c[0] for c in self.calls], ["telegram"])
        self.assertEqual(self._rows(1)["telegram"]["status"], "removed")

    def test_an_item_still_in_the_queue_is_refused(self):
        self._request({"news_id": 3, "operator": "wildcar"}, news_id=3)

        takedown.run(self.cfg, removers=self._removers())

        self.assertEqual(self.calls, [])
        self.assertEqual(self._rows(3)["telegram"]["status"], "ok")
        self.assertFalse(list(Path(self.cfg.requests_dir).glob("takedown-*.json")))

    def test_default_reason_is_the_editors_decision(self):
        self._request({"news_id": 1})
        takedown.run(self.cfg, removers=self._removers())
        self.assertEqual(self._rows(1)["vk"]["error"], "решение редактора")

    def test_ids_are_read_from_the_recorded_urls(self):
        self.assertEqual(takedown.telegram_message_id("https://t.me/posinus/1422"), 1422)
        self.assertEqual(takedown.telegram_message_id("tg:-100123:77"), 77)
        self.assertEqual(takedown.vk_post_id("https://vk.ru/wall-233237778_965"), 965)
        self.assertEqual(takedown.site_note_id('<article data-note-id="1333">'), "1333")
        with self.assertRaises(PublishError):
            takedown.telegram_message_id("https://wildcar.org/news/1/")


if __name__ == "__main__":
    unittest.main()
