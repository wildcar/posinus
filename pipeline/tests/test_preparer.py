"""Unit tests for preparer.py: image extraction, retelling parse, markdown, own DB."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evaluator
import preparer
from preparer import (
    build_markdown,
    extract_illustrations,
    migrate_own_db,
    open_own_db,
    parse_retelling,
    prepared_ids,
    record_error,
    save_prepared,
)

ARTICLE_HTML = b"""
<html><head>
<meta property="og:image" content="https://site.test/lead.jpg">
</head><body>
<article>
<figure><img src="/img/one.jpg" alt="alt one"><figcaption>Caption one</figcaption></figure>
<p>text</p>
<figure><img data-src="https://site.test/img/two.png"><figcaption>Caption two</figcaption></figure>
<img src="lazy.webp" alt="loose alt">
<img src="data:image/gif;base64,AAAA">
</article></body></html>
"""


class ExtractIllustrationsTests(unittest.TestCase):
    def test_order_captions_and_resolution(self):
        items = extract_illustrations(ARTICLE_HTML, "https://site.test/news/1", limit=10)
        urls = [i["url"] for i in items]
        self.assertEqual(urls[0], "https://site.test/lead.jpg")  # og:image first
        self.assertIn("https://site.test/img/one.jpg", urls)     # relative resolved
        self.assertIn("https://site.test/img/two.png", urls)     # data-src (lazy) picked up
        self.assertIn("https://site.test/news/lazy.webp", urls)  # loose img resolved
        self.assertNotIn("data:image/gif;base64,AAAA", urls)     # data: URI dropped
        by_url = {i["url"]: i["caption"] for i in items}
        self.assertEqual(by_url["https://site.test/img/one.jpg"], "Caption one")
        self.assertEqual(by_url["https://site.test/news/lazy.webp"], "loose alt")

    def test_limit_and_dedup(self):
        items = extract_illustrations(ARTICLE_HTML, "https://site.test/", limit=2)
        self.assertEqual(len(items), 2)
        html = b'<img src="/a.jpg"><img src="/a.jpg">'
        self.assertEqual(len(extract_illustrations(html, "https://site.test/", 10)), 1)


class ParseRetellingTests(unittest.TestCase):
    def test_list_body(self):
        title, paras = parse_retelling({"title": "  Заголовок  ", "body": ["Раз", " Два ", ""]})
        self.assertEqual(title, "Заголовок")
        self.assertEqual(paras, ["Раз", "Два"])

    def test_string_body_split_on_blank_lines(self):
        _, paras = parse_retelling({"title": "T", "body": "Первый абзац.\n\nВторой абзац."})
        self.assertEqual(paras, ["Первый абзац.", "Второй абзац."])

    def test_missing_title(self):
        with self.assertRaises(evaluator.EvaluationInvalid):
            parse_retelling({"body": ["x"]})

    def test_empty_body(self):
        with self.assertRaises(evaluator.EvaluationInvalid):
            parse_retelling({"title": "T", "body": []})

    def test_long_dashes_normalized(self):
        title, paras = parse_retelling({"title": "Мышь — рекордсмен", "body": ["Она живёт долго — и активно."]})
        self.assertEqual(title, "Мышь - рекордсмен")
        self.assertEqual(paras, ["Она живёт долго - и активно."])
        self.assertNotIn("—", title + paras[0])


class BuildMarkdownTests(unittest.TestCase):
    def test_structure_and_source_name(self):
        md = build_markdown("Заголовок", ["Абзац раз", "Абзац два"], "https://www.site.test/news/5", "")
        self.assertTrue(md.startswith("# Заголовок\n\n"))
        self.assertIn("Абзац раз\n\nАбзац два", md)
        # www stripped when the name is derived from the host
        self.assertIn("Источник: [site.test](https://www.site.test/news/5)", md)

    def test_no_source(self):
        self.assertEqual(build_markdown("T", ["Один абзац"], "", ""), "# T\n\nОдин абзац\n")


class MigrateOwnDbTests(unittest.TestCase):
    def test_backfills_markdown_from_legacy_html(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(
            "CREATE TABLE prepared_item (news_id INTEGER PRIMARY KEY, status TEXT NOT NULL, "
            "retold_title TEXT, retold_body_html TEXT, page_path TEXT, model_id TEXT, "
            "prepared_at TEXT, published_at TEXT, error TEXT)"
        )
        con.execute(
            "INSERT INTO prepared_item (news_id, status, retold_title, retold_body_html) VALUES (3, 'prepared', 'Заголовок', ?)",
            ('<h1>Заголовок</h1><p>Первый.</p><p>Второй.</p>'
             '<footer>Источник: <a href="https://site.test/a?x=1&amp;y=2">site.test</a></footer>',),
        )
        con.commit()
        migrate_own_db(con)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(prepared_item)")}
        self.assertIn("retold_body_md", cols)
        md = con.execute("SELECT retold_body_md FROM prepared_item WHERE news_id=3").fetchone()["retold_body_md"]
        self.assertIn("# Заголовок", md)
        self.assertIn("Первый.\n\nВторой.", md)
        # the escaped ampersand in the stored href is decoded back
        self.assertIn("Источник: [site.test](https://site.test/a?x=1&y=2)", md)
        con.close()

    def test_idempotent_on_current_schema(self):
        con = open_own_db(":memory:")   # already has retold_body_md
        migrate_own_db(con)             # second call must be a no-op, not raise
        con.close()


class OwnDbTests(unittest.TestCase):
    def setUp(self):
        self.con = open_own_db(":memory:")

    def tearDown(self):
        self.con.close()

    def test_save_and_list_prepared(self):
        images = [{"path": "/m/5/1.jpg", "caption": "c", "source_url": "https://s/1"}]
        save_prepared(self.con, 5, "Заголовок", "# Заголовок\n\nтекст\n", "deepseek-chat", images)
        self.assertEqual(prepared_ids(self.con), {5})
        row = self.con.execute("SELECT status, retold_title, retold_body_md FROM prepared_item WHERE news_id=5").fetchone()
        self.assertEqual((row["status"], row["retold_title"]), ("prepared", "Заголовок"))
        self.assertIn("# Заголовок", row["retold_body_md"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM illustration WHERE news_id=5").fetchone()[0], 1)

    def test_resave_replaces_illustrations(self):
        save_prepared(self.con, 5, "t", "md", "m", [{"path": "/a", "caption": "", "source_url": ""}])
        save_prepared(self.con, 5, "t", "md", "m", [{"path": "/b", "caption": "", "source_url": ""},
                                                    {"path": "/c", "caption": "", "source_url": ""}])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM illustration WHERE news_id=5").fetchone()[0], 2)

    def test_error_then_recovery(self):
        record_error(self.con, 7, "boom")
        self.assertEqual(prepared_ids(self.con), set())  # errors are not 'prepared'
        row = self.con.execute("SELECT status, error FROM prepared_item WHERE news_id=7").fetchone()
        self.assertEqual((row["status"], row["error"]), ("error", "boom"))
        save_prepared(self.con, 7, "t", "md", "m", [])
        self.assertEqual(prepared_ids(self.con), {7})


if __name__ == "__main__":
    unittest.main()
