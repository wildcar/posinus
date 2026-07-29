"""Unit tests for preparer.py: image extraction, retelling parse, markdown, own DB."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
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

    def test_ignored_image_takes_no_slot_and_leaks_no_caption(self):
        # the source site's logo parsed as an illustration — the reason the list exists
        html = (
            b'<html><head><meta property="og:image" '
            b'content="https://site.test/theme/logo.png?v=3"></head><body>'
            b'<img src="/theme/logo.png" alt="Optimist daily">'
            b'<figure><img src="/img/one.jpg"><figcaption>Real caption</figcaption></figure>'
            b'<figure><img src="/img/two.jpg"></figure></body></html>'
        )
        ignored = frozenset({"https://site.test/theme/logo.png"})
        items = extract_illustrations(html, "https://site.test/", limit=2, ignored=ignored)
        # both real figures fit: the ignored logo (og:image AND loose img) took no slot
        self.assertEqual([i["url"] for i in items],
                         ["https://site.test/img/one.jpg", "https://site.test/img/two.jpg"])
        self.assertNotIn("Optimist daily", [i["caption"] for i in items])

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

    def test_long_dashes_are_kept(self):
        # the dash ban was the owner's rule and the owner lifted it (2026-07-28):
        # «Длинные тире — прекрасны»
        title, paras = parse_retelling({"title": "Мышь — рекордсмен", "body": ["Она живёт долго — и активно."]})
        self.assertEqual(title, "Мышь — рекордсмен")
        self.assertEqual(paras, ["Она живёт долго — и активно."])


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


class IgnoredImageTests(unittest.TestCase):
    LOGO = "https://site.test/theme/logo.png"

    def _saved_image(self, media_dir: str, news_id: int, name: str, source_url: str) -> dict[str, str]:
        path = Path(media_dir) / str(news_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"img")
        return {"path": str(path), "caption": "", "source_url": source_url}

    def test_add_purges_the_queue_but_not_public_or_edited_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = preparer.PreparerConfig(own_db=str(Path(tmp) / "own.sqlite3"), media_dir=tmp)
            con = open_own_db(cfg.own_db)
            for news_id, status, edited in ((1, "prepared", None), (2, "published", None),
                                            (3, "prepared", "2026-07-28T00:00:00+00:00")):
                save_prepared(con, news_id, "t", "md", "m", [
                    self._saved_image(tmp, news_id, "1.png", self.LOGO + "?v=3"),
                    self._saved_image(tmp, news_id, "2.jpg", f"https://site.test/photo{news_id}.jpg"),
                ])
                con.execute("UPDATE prepared_item SET status = ?, edited_at = ? WHERE news_id = ?",
                            (status, edited, news_id))
            con.commit()
            con.close()

            self.assertEqual(preparer.add_ignored_image(cfg, self.LOGO + "?v=9", "логотип"), 0)

            con = open_own_db(cfg.own_db)
            urls = {row["news_id"]: row["source_url"] for row in
                    con.execute("SELECT news_id, source_url FROM illustration ORDER BY id")
                    if "logo" in row["source_url"]}
            self.assertEqual(set(urls), {2, 3})  # queued copy of news 1 is gone, the rest stay
            self.assertEqual(con.execute("SELECT COUNT(*) FROM illustration WHERE news_id = 1").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT url_key, note FROM ignored_image").fetchone()[:],
                             (self.LOGO, "логотип"))
            con.close()
            self.assertFalse((Path(tmp) / "1" / "1.png").exists())   # queue copy: file deleted
            self.assertTrue((Path(tmp) / "1" / "2.jpg").exists())
            self.assertTrue((Path(tmp) / "2" / "1.png").exists())    # published: untouched
            self.assertTrue((Path(tmp) / "3" / "1.png").exists())    # edited: untouched

    def test_deletes_via_media_dir_when_the_stored_path_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = preparer.PreparerConfig(own_db=str(Path(tmp) / "own.sqlite3"), media_dir=tmp)
            real = Path(tmp) / "4" / "1.png"
            real.parent.mkdir(parents=True)
            real.write_bytes(b"img")
            con = open_own_db(cfg.own_db)
            save_prepared(con, 4, "t", "md", "m",
                          [{"path": "/var/lib/old-tree/4/1.png", "caption": "", "source_url": self.LOGO}])
            con.close()

            preparer.add_ignored_image(cfg, self.LOGO, "")

            self.assertFalse(real.exists())

    def test_main_ignore_image_needs_no_router_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"EVALUATOR_DB_PATH": str(Path(tmp) / "own.sqlite3"), "MEDIA_DIR": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("ROUTER_AUTH_TOKEN", None)
                self.assertEqual(preparer.main(["--ignore-image", self.LOGO, "--note", "лого"]), 0)
            con = open_own_db(env["EVALUATOR_DB_PATH"])
            self.assertEqual(con.execute("SELECT url_key FROM ignored_image").fetchone()[0], self.LOGO)
            con.close()

    def test_run_feeds_the_ignore_list_into_preparation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = preparer.PreparerConfig(own_db=str(Path(tmp) / "own.sqlite3"), media_dir=tmp)
            con = open_own_db(cfg.own_db)
            con.execute("INSERT INTO ignored_image VALUES (?, '', '')", (self.LOGO,))
            con.commit()
            con.close()
            fake_news_con = mock.Mock()
            fake_news_con.execute.return_value.fetchall.return_value = [{"news_id": 8}]
            seen: dict[str, frozenset] = {}

            def fake_prepare_one(cfg_, router_cfg, news, dry_run, ignored=frozenset()):
                seen["ignored"] = ignored
                return {"title": "t", "paragraphs": [], "model_id": "m", "images": [], "body_md": "md"}

            with mock.patch.object(evaluator, "open_db", return_value=fake_news_con), \
                 mock.patch.object(preparer, "prepare_one", fake_prepare_one):
                preparer.run(cfg, mock.Mock(), limit=5, dry_run=False, only=None)
            self.assertEqual(seen["ignored"], {self.LOGO})

    def test_rejects_a_non_http_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = preparer.PreparerConfig(own_db=str(Path(tmp) / "own.sqlite3"), media_dir=tmp)
            self.assertEqual(preparer.add_ignored_image(cfg, "logo.png", ""), 2)


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


def _noise_png(path: Path, width: int = 2000, height: int = 1500) -> None:
    """A PNG of random noise: incompressible, so reliably over IMAGE_RECODE_BYTES."""
    raw = os.urandom(width * height * 3)
    subprocess.run(
        [preparer.FFMPEG, "-y", "-nostdin", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
         "-i", "-", "-frames:v", "1", str(path)],
        input=raw, check=True, capture_output=True,
    )


@unittest.skipUnless(shutil.which(preparer.FFMPEG), "ffmpeg is not on this host")
class ShrinkImageTests(unittest.TestCase):
    def test_heavy_png_becomes_a_small_jpg(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1.png"
            _noise_png(path)
            original = path.stat().st_size
            self.assertGreater(original, preparer.IMAGE_RECODE_BYTES)
            result = preparer.shrink_image(path)
            self.assertEqual(result, Path(tmp) / "1.jpg")
            self.assertFalse(path.exists())
            self.assertLess(result.stat().st_size, original)
            # even for pure noise — JPEG's worst case — 1600 px stays under
            # half of Telegram's 10 MB photo cap; photographs land far lower
            self.assertLess(result.stat().st_size, 5_000_000)

    def test_light_file_and_gif_pass_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            light = Path(tmp) / "1.jpg"
            light.write_bytes(b"J" * 4000)
            self.assertEqual(preparer.shrink_image(light), light)
            self.assertEqual(light.read_bytes(), b"J" * 4000)
            gif = Path(tmp) / "2.gif"
            gif.write_bytes(b"G" * (preparer.IMAGE_RECODE_BYTES + 1))
            self.assertEqual(preparer.shrink_image(gif), gif)

    def test_ffmpeg_failure_keeps_the_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1.png"
            body = b"\x89PNG not really a picture" * 20_000
            path.write_bytes(body)
            with mock.patch.object(preparer, "FFMPEG", "/nonexistent/ffmpeg"):
                self.assertEqual(preparer.shrink_image(path), path)
            self.assertEqual(path.read_bytes(), body)  # broken input: ffmpeg fails
            self.assertEqual(preparer.shrink_image(path), path)
            self.assertEqual(path.read_bytes(), body)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["1.png"])


class ParseCaptionsTests(unittest.TestCase):
    def test_valid_captions_whitespace_collapsed_dashes_kept(self):
        payload = {"captions": ["Кот — герой", "  два   слова "]}
        self.assertEqual(preparer.parse_captions(payload, 2), ["Кот — герой", "два слова"])

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


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 4000


class GenerateIllustrationTests(unittest.TestCase):
    def _router_cfg(self):
        cfg = evaluator.Config()
        cfg.router_user = "news-preparer"
        return cfg

    def test_generated_image_saved_with_sniffed_extension_and_provenance(self):
        import base64
        reply = {"image_b64": [base64.b64encode(PNG_BYTES).decode()], "model_id": "gpt-image-2"}
        calls = {}

        def fake_call_tool(url, tool, arguments, token=None, timeout=300.0):
            calls.update(tool=tool, arguments=arguments)
            return reply

        with tempfile.TemporaryDirectory() as tmp:
            cfg = preparer.PreparerConfig(media_dir=tmp)
            with mock.patch.object(evaluator, "call_tool", fake_call_tool):
                entry = preparer.generate_illustration(cfg, self._router_cfg(), 42, "Заголовок", ["Абзац."])
            self.assertEqual(entry["path"], str(Path(tmp) / "42" / "1.png"))
            self.assertEqual(Path(entry["path"]).read_bytes(), PNG_BYTES)
        self.assertEqual(entry["caption"], "")
        self.assertEqual(entry["source_url"], "generated://gpt-image-2")
        self.assertEqual(calls["tool"], "generate_image")
        self.assertEqual(calls["arguments"]["provider"], "codex-oauth")
        self.assertEqual(calls["arguments"]["external_user_id"], "news-preparer")
        self.assertIn("Заголовок", calls["arguments"]["prompt"])
        self.assertIn("Без текста", calls["arguments"]["prompt"])

    def test_failures_return_none_and_never_raise(self):
        import base64
        replies = [
            evaluator.McpError("router down"),          # transport failure
            {"model_id": "m"},                           # no image in the reply
            {"image_b64": ["не base64!!"]},              # undecodable payload
            {"image_b64": [base64.b64encode(b"tiny").decode()]},  # below MIN_IMAGE_BYTES
        ]

        def fake_call_tool(url, tool, arguments, token=None, timeout=300.0):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with tempfile.TemporaryDirectory() as tmp:
            cfg = preparer.PreparerConfig(media_dir=tmp)
            with mock.patch.object(evaluator, "call_tool", fake_call_tool):
                for _ in range(4):
                    self.assertIsNone(
                        preparer.generate_illustration(cfg, self._router_cfg(), 42, "Т", ["А."]))

    def test_prompt_caps_the_lead_and_carries_the_no_text_rule(self):
        prompt = preparer.build_image_prompt("Т", ["а" * 1000, "б" * 1000])
        self.assertLessEqual(len(prompt), len(preparer.IMAGE_PROMPT) + preparer.IMAGE_PROMPT_LEAD_CHARS)
        self.assertIn("логотипов", prompt)


class PrepareOneGenerationTests(unittest.TestCase):
    NEWS = {"news_id": 9, "primary_url": "https://s.test/a",
            "title": "T", "body_text": "B.", "language": "en"}
    RETOLD = {"text": '{"title": "Т", "body": ["А."]}', "model_id": "m"}

    def _prepare(self, cfg, dry_run, candidates, downloaded, generated_entry):
        with mock.patch.object(preparer, "allowed_by_robots", return_value=True), \
             mock.patch.object(preparer, "fetch", return_value=("https://s.test/a", "text/html", b"")), \
             mock.patch.object(preparer, "extract_illustrations", return_value=candidates), \
             mock.patch.object(preparer, "download_illustrations", return_value=downloaded), \
             mock.patch.object(preparer, "generate_illustration", return_value=generated_entry) as gen, \
             mock.patch.object(evaluator, "chat", return_value=self.RETOLD):
            result = preparer.prepare_one(cfg, mock.Mock(), self.NEWS, dry_run)
        return result, gen

    def test_zero_pictures_get_one_generated(self):
        entry = {"path": "/m/9/1.png", "caption": "", "source_url": "generated://gpt-image-2"}
        result, gen = self._prepare(preparer.PreparerConfig(fetch_delay=0), False, [], [], entry)
        self.assertEqual(result["images"], [entry])
        self.assertTrue(result["generated"])
        gen.assert_called_once()

    def test_downloaded_pictures_suppress_generation(self):
        downloaded = [{"path": "/m/9/1.jpg", "caption": "", "source_url": "https://s.test/1.jpg"}]
        result, gen = self._prepare(preparer.PreparerConfig(fetch_delay=0), False,
                                    [{"url": "https://s.test/1.jpg", "caption": ""}], downloaded, None)
        self.assertEqual(result["images"], downloaded)
        self.assertFalse(result["generated"])
        gen.assert_not_called()

    def test_dry_run_never_spends_an_image_call(self):
        result, gen = self._prepare(preparer.PreparerConfig(fetch_delay=0), True, [], [], None)
        self.assertEqual(result["images"], [])
        gen.assert_not_called()

    def test_empty_provider_turns_the_feature_off(self):
        cfg = preparer.PreparerConfig(fetch_delay=0, image_provider="")
        result, gen = self._prepare(cfg, False, [], [], None)
        self.assertEqual(result["images"], [])
        gen.assert_not_called()

    def test_a_generation_failure_does_not_fail_the_preparation(self):
        result, gen = self._prepare(preparer.PreparerConfig(fetch_delay=0), False, [], [], None)
        self.assertEqual(result["images"], [])
        self.assertFalse(result["generated"])
        gen.assert_called_once()


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
