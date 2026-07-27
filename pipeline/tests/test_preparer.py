"""Unit tests for preparer.py: image extraction, retelling parse, markdown, own DB."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_og_image_resized_duplicate_is_folded_and_donates_its_caption(self):
        # og:image is the lead figure again with different sizing parameters —
        # the pair used to publish as two identical photographs
        html = (
            b'<html><head><meta property="og:image" '
            b'content="https://site.test/img/one.jpg?w=1200&h=630"></head><body>'
            b'<figure><img src="/img/one.jpg?w=800"><figcaption>Signed</figcaption></figure>'
            b'<figure><img src="/img/two.jpg"></figure></body></html>'
        )
        items = extract_illustrations(html, "https://site.test/", limit=10)
        self.assertEqual([i["url"] for i in items],
                         ["https://site.test/img/one.jpg?w=1200&h=630",
                          "https://site.test/img/two.jpg"])
        # the kept og:image copy adopted the figure's caption
        self.assertEqual(items[0]["caption"], "Signed")

    def test_caption_donated_even_past_the_limit(self):
        html = (
            b'<html><head><meta property="og:image" '
            b'content="https://site.test/img/one.jpg?w=1200"></head><body>'
            b'<figure><img src="/img/one.jpg"><figcaption>Late caption</figcaption></figure>'
            b"</body></html>"
        )
        items = extract_illustrations(html, "https://site.test/", limit=1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["caption"], "Late caption")


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

    def test_a_published_item_is_never_prepared_again(self):
        """It was, for three days: 215 preparations over 129 news items in a day.

        A published item is not «prepared», so the old check let it back into the
        queue; it was retold at the price of a model call, went back to
        `prepared`, and the publisher rewrote its publication date to today.
        """
        save_prepared(self.con, 5, "t", "md", "m", [])
        self.con.execute("UPDATE prepared_item SET status = 'published' WHERE news_id = 5")

        self.assertEqual(prepared_ids(self.con), {5})

    def test_an_item_taken_off_the_queue_is_not_resurrected(self):
        save_prepared(self.con, 5, "t", "md", "m", [])
        self.con.execute("UPDATE prepared_item SET status = 'expired' WHERE news_id = 5")

        self.assertEqual(prepared_ids(self.con), {5})

    def test_error_then_recovery(self):
        record_error(self.con, 7, "boom")
        self.assertEqual(prepared_ids(self.con), set())  # errors are the one retryable status
        row = self.con.execute("SELECT status, error FROM prepared_item WHERE news_id=7").fetchone()
        self.assertEqual((row["status"], row["error"]), ("error", "boom"))
        save_prepared(self.con, 7, "t", "md", "m", [])
        self.assertEqual(prepared_ids(self.con), {7})


class DownloadDedupTests(unittest.TestCase):
    def test_byte_identical_files_are_saved_once(self):
        # distinct URLs, same bytes: CDN aliases of one photograph
        bodies = {
            "https://a.test/1.jpg": b"X" * 4000,
            "https://b.test/same.jpg": b"X" * 4000,
            "https://a.test/2.jpg": b"Y" * 4000,
        }

        def fake_fetch(url, user_agent, timeout=0):
            return url, "image/jpeg", bodies[url]

        with tempfile.TemporaryDirectory() as tmp:
            cfg = preparer.PreparerConfig(media_dir=tmp, fetch_delay=0)
            candidates = [{"url": url, "caption": ""} for url in bodies]
            with mock.patch.object(preparer, "allowed_by_robots", return_value=True), \
                 mock.patch.object(preparer, "fetch", fake_fetch):
                saved = preparer.download_illustrations(cfg, 9, candidates)
        self.assertEqual(len(saved), 2)
        self.assertEqual([s["source_url"] for s in saved],
                         ["https://a.test/1.jpg", "https://a.test/2.jpg"])


class ParseCaptionsTests(unittest.TestCase):
    def test_valid_captions_normalized(self):
        payload = {"captions": ["Кот — герой", "  два   слова "]}
        self.assertEqual(preparer.parse_captions(payload, 2), ["Кот - герой", "два слова"])

    def test_unusable_reply_returns_none(self):
        self.assertIsNone(preparer.parse_captions({}, 2))
        self.assertIsNone(preparer.parse_captions({"captions": ["одна"]}, 2))
        self.assertIsNone(preparer.parse_captions({"captions": "строкой"}, 1))
        self.assertIsNone(preparer.parse_captions({"captions": [1, 2]}, 2))

    def test_nothing_expected_returns_none(self):
        self.assertIsNone(preparer.parse_captions({"captions": ["лишняя"]}, 0))


class RetellCaptionsTests(unittest.TestCase):
    NEWS = {"news_id": 1, "title": "T", "body_text": "Body.", "language": "en"}

    def test_captions_ride_in_the_same_call_and_come_back(self):
        def fake_chat(cfg, messages):
            self.assertIn("Подписи к иллюстрациям:", messages[1]["content"])
            self.assertIn("1. First cap", messages[1]["content"])
            return {"text": '{"title": "Т", "body": ["А."], "captions": ["Первая"]}',
                    "model_id": "m"}

        with mock.patch.object(evaluator, "chat", fake_chat):
            title, paras, captions, model_id = preparer.retell(mock.Mock(), self.NEWS, ["First cap"])
        self.assertEqual(captions, ["Первая"])
        self.assertEqual((title, paras, model_id), ("Т", ["А."], "m"))

    def test_bad_captions_do_not_fail_the_retelling(self):
        def fake_chat(cfg, messages):
            return {"text": '{"title": "Т", "body": ["А."]}', "model_id": "m"}

        with mock.patch.object(evaluator, "chat", fake_chat):
            title, paras, captions, _ = preparer.retell(mock.Mock(), self.NEWS, ["First cap"])
        self.assertIsNone(captions)  # the originals are kept downstream
        self.assertEqual(title, "Т")

    def test_no_captions_no_block_in_the_prompt(self):
        def fake_chat(cfg, messages):
            self.assertNotIn("Подписи к иллюстрациям", messages[1]["content"])
            return {"text": '{"title": "Т", "body": ["А."]}', "model_id": "m"}

        with mock.patch.object(evaluator, "chat", fake_chat):
            preparer.retell(mock.Mock(), self.NEWS, [])


class PrepareOneCaptionTests(unittest.TestCase):
    def test_translations_land_on_the_captioned_candidates_only(self):
        news = {"news_id": 1, "primary_url": "https://s.test/a",
                "title": "T", "body_text": "B.", "language": "en"}
        candidates = [
            {"url": "https://s.test/1.jpg", "caption": ""},
            {"url": "https://s.test/2.jpg", "caption": "First"},
            {"url": "https://s.test/3.jpg", "caption": "Second"},
        ]

        def fake_chat(cfg, messages):
            return {"text": '{"title": "Т", "body": ["А."], "captions": ["Первая", "Вторая"]}',
                    "model_id": "m"}

        cfg = preparer.PreparerConfig(fetch_delay=0)
        with mock.patch.object(preparer, "allowed_by_robots", return_value=True), \
             mock.patch.object(preparer, "fetch", return_value=("https://s.test/a", "text/html", b"")), \
             mock.patch.object(preparer, "extract_illustrations", return_value=candidates), \
             mock.patch.object(evaluator, "chat", fake_chat):
            result = preparer.prepare_one(cfg, mock.Mock(), news, dry_run=True)
        self.assertEqual([img["caption"] for img in result["images"]],
                         ["", "Первая", "Вторая"])


class RouterIdentityTests(unittest.TestCase):
    """The preparer must not spend the evaluator's identity at the router."""

    def _router_cfg_of_a_dry_run(self, env: dict[str, str]) -> evaluator.Config:
        captured: list[evaluator.Config] = []

        def fake_run(cfg, router_cfg, **kwargs):
            captured.append(router_cfg)
            return 0

        original_run = preparer.run
        preparer.run = fake_run
        try:
            with mock.patch.dict(os.environ, {"ROUTER_AUTH_TOKEN": "t", **env}):
                self.assertEqual(preparer.main(["--dry-run"]), 0)
        finally:
            preparer.run = original_run
        return captured[0]

    def test_default_router_user_is_the_preparer(self):
        cfg = self._router_cfg_of_a_dry_run({})
        self.assertEqual(evaluator.build_chat_arguments(cfg, [])["external_user_id"], "news-preparer")

    def test_router_user_is_configurable(self):
        cfg = self._router_cfg_of_a_dry_run({"PREPARER_ROUTER_USER_ID": "retelling-bot"})
        self.assertEqual(evaluator.build_chat_arguments(cfg, [])["external_user_id"], "retelling-bot")


if __name__ == "__main__":
    unittest.main()
