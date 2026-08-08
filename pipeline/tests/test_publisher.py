"""Unit tests for publisher.py: content builders, HTTP encoding, own DB, run loop.

No network: platform sends are exercised through monkeypatched adapters."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import publisher
from publisher import (
    PreparedNews,
    PublishError,
    PublisherConfig,
    WildcarEntry,
    build_site_text,
    build_tg_message,
    build_vk_message,
    build_wildcar_feed,
    build_wildcar_index,
    build_wildcar_page,
    encode_multipart,
    input_val,
    open_own_db,
    parse_markdown,
    publication_status,
    source_name_from_url,
    _abs_url,
    _tg_len,
)

MARKDOWN_DOC = (
    "# Заголовок\n\n"
    "Первый абзац с «кавычками» и амперсандом & знаком.\n\n"
    "Второй абзац.\n\n"
    "Третий абзац.\n\n"
    "Источник: [site.test](https://site.test/a)"
)


class ParseMarkdownTests(unittest.TestCase):
    def test_parses_title_paragraphs_source(self):
        title, paras, url, name = parse_markdown(MARKDOWN_DOC)
        self.assertEqual(title, "Заголовок")
        self.assertEqual(len(paras), 3)
        self.assertEqual(paras[0], "Первый абзац с «кавычками» и амперсандом & знаком.")
        self.assertEqual(paras[2], "Третий абзац.")
        self.assertEqual((url, name), ("https://site.test/a", "site.test"))

    def test_source_line_not_in_paragraphs(self):
        _, paras, _, _ = parse_markdown(MARKDOWN_DOC)
        self.assertTrue(all("Источник" not in p for p in paras))

    def test_empty(self):
        self.assertEqual(parse_markdown(""), ("", [], "", ""))

    def test_source_name_falls_back_to_host(self):
        _, _, url, name = parse_markdown("# T\n\nabc\n\nИсточник: [](https://www.foo.test/x)")
        self.assertEqual((url, name), ("https://www.foo.test/x", "foo.test"))


class SourceNameTests(unittest.TestCase):
    def test_strips_www(self):
        self.assertEqual(source_name_from_url("https://www.upi.com/x/y"), "upi.com")
        self.assertEqual(source_name_from_url("https://ria.ru/z"), "ria.ru")


class TelegramMessageTests(unittest.TestCase):
    def test_structure_and_escaping(self):
        msg = build_tg_message("A & B", ["one", "two"], "https://site.test/a", "site.test", 4096)
        self.assertIn("<b>A &amp; B</b>", msg)
        self.assertIn("one\n\ntwo", msg)
        self.assertIn('<a href="https://site.test/a">Источник: site.test</a>', msg)

    def test_full_text_fits_without_truncation(self):
        paras = ["п" * 400] * 4  # ~1600 visible chars: a typical whole retelling
        msg = build_tg_message("T", paras, "https://s.test/a", "s.test", 4096)
        self.assertEqual(msg.count("п" * 400), 4)

    def test_limit_counts_visible_text_not_raw_html(self):
        # 977 visible chars but >1024 with tags and the href URL: both
        # paragraphs must survive, the old raw-HTML count dropped one.
        long_url = "https://example.test/" + "u" * 120
        paras = ["x" * 430, "y" * 430]
        msg = build_tg_message("T" * 66, paras, long_url, "example.test", 1024)
        self.assertIn("x" * 430, msg)
        self.assertIn("y" * 430, msg)

    def test_truncates_paragraphs_to_fit(self):
        paras = ["x" * 500, "y" * 500, "z" * 500]
        msg = build_tg_message("T", paras, "https://s.test/a", "s.test", 1024)
        self.assertIn("x" * 500, msg)
        self.assertNotIn("z" * 500, msg)
        # the link (source) is kept even when paragraphs are dropped
        self.assertIn("Источник", msg)

    def test_title_only_when_nothing_fits(self):
        msg = build_tg_message("Title", ["x" * 5000], "", "", 1024)
        self.assertNotIn("x" * 5000, msg)
        self.assertIn("Title", msg)

    def test_tg_len_counts_utf16_units(self):
        self.assertEqual(_tg_len("абв"), 3)
        self.assertEqual(_tg_len("🎬"), 2)  # non-BMP emoji is two UTF-16 units

    def test_truncated_message_links_the_full_text(self):
        paras = ["x" * 600, "y" * 600]
        msg = build_tg_message("T", paras, "https://s.test/a", "s.test", 1024,
                               more_url="https://wildcar.org/news/5/")
        self.assertIn("x" * 600, msg)
        self.assertNotIn("y" * 600, msg)
        self.assertIn('<a href="https://wildcar.org/news/5/">Полный текст на wildcar.org</a>', msg)
        self.assertIn("Источник", msg)

    def test_untruncated_message_has_no_full_text_link(self):
        msg = build_tg_message("T", ["a"], "https://s.test/a", "s.test", 1024,
                               more_url="https://wildcar.org/news/5/")
        self.assertNotIn("Полный текст", msg)


class VkAndSiteTextTests(unittest.TestCase):
    def test_vk_message_has_title_body_source(self):
        msg = build_vk_message("Заголовок", ["a", "b"], "https://s.test/a", "s.test")
        self.assertTrue(msg.startswith("Заголовок"))
        self.assertIn("a\n\nb", msg)
        self.assertIn("Источник: https://s.test/a", msg)

    def test_site_text_neasden_markup(self):
        text = build_site_text([("pic.jpg", "")], ["a", "b"], "https://s.test/a", "s.test")
        self.assertTrue(text.startswith("pic.jpg\n\n"))
        self.assertIn("Источник: ((https://s.test/a s.test))", text)

    def test_site_text_without_image(self):
        text = build_site_text([], ["a"], "https://s.test/a", "s.test")
        self.assertFalse(text.startswith("\n"))
        self.assertTrue(text.startswith("a"))

    def test_site_text_mirrors_the_wildcar_page(self):
        """Lead picture, paragraphs, the rest of the pictures; a caption sits on
        the very next line — that is how Neasden draws it inside the block."""
        text = build_site_text(
            [("1.jpg", "Первая подпись"), ("2.jpg", ""), ("3.jpg", "Третья")],
            ["a", "b"], "https://s.test/a", "s.test")
        self.assertEqual(
            text,
            "1.jpg\nПервая подпись\n\na\n\nb\n\n2.jpg\n\n3.jpg\nТретья\n\n"
            "Источник: ((https://s.test/a s.test))")


class TagTests(unittest.TestCase):
    def test_split_tags(self):
        self.assertEqual(publisher.split_tags("добрые новости, экология , ,"),
                         ["добрые новости", "экология"])
        self.assertEqual(publisher.split_tags(None), [])
        self.assertEqual(publisher.split_tags(""), [])

    def test_merge_tags_first_occurrence_wins(self):
        merged = publisher.merge_tags(["экология", "Новость"],
                                      publisher.NEWS_TAGS)
        self.assertEqual(merged, ["экология", "Новость", "позитивная", "позитивная новость"])

    def test_site_tags_default_is_empty(self):
        # The owner asked for exactly the item tags on wildcar.ru — no
        # «добрые новости» base unless EGEYA_TAGS is set on purpose.
        self.assertEqual(PublisherConfig().site_tags, "")

    def test_front_matter_quotes_tags(self):
        fm = publisher.build_front_matter(["позитивная новость", "эко"])
        self.assertTrue(fm.startswith("---\ntags:\n"))
        self.assertIn('  - "позитивная новость"', fm)
        self.assertTrue(fm.endswith("---\n\n"))
        self.assertEqual(publisher.build_front_matter([]), "")

    def test_build_item_merges_stored_tags_with_news_tags(self):
        con = open_own_db(":memory:")
        con.execute("INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md, tags) "
                    "VALUES (1, 'prepared', 'T', '# Т' || char(10) || 'текст', 'экология, новость')")
        row = con.execute(publisher.PREPARED_SQL).fetchone()
        item = publisher.build_item(con, row)
        self.assertEqual(item.tags, ["экология", "новость", "позитивная", "позитивная новость"])
        con.close()

    def test_wildcar_page_starts_with_the_front_matter(self):
        entry = publisher.WildcarEntry(
            news_id=1, title="Т", paragraphs=["Абзац."], source_url="", source_name="",
            images=[], published_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
            tags=["позитивная"])
        page = publisher.build_wildcar_page(entry, ZoneInfo("Europe/Moscow"))
        self.assertTrue(page.startswith('---\ntags:\n  - "позитивная"\n---\n\n# Т'))


class MultipartTests(unittest.TestCase):
    def test_encodes_fields_and_file(self):
        ctype, body = encode_multipart(
            {"chat_id": "-100", "caption": "hi"},
            {"photo": ("p.jpg", b"\xff\xd8bytes", "image/jpeg")},
        )
        self.assertIn("multipart/form-data; boundary=", ctype)
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(b'filename="p.jpg"', body)
        self.assertIn(b"\xff\xd8bytes", body)
        self.assertTrue(body.rstrip().endswith(b"--"))


class InputValTests(unittest.TestCase):
    def test_by_id_then_name_and_unescape(self):
        page = '<input id="token" value="ab&amp;cd"><input name="old-stamp" value="123">'
        self.assertEqual(input_val(page, "token"), "ab&cd")
        self.assertEqual(input_val(page, "old-stamp"), "123")
        self.assertEqual(input_val(page, "missing"), "")

    def test_abs_url(self):
        self.assertEqual(_abs_url("https://x.test", "/a/b/"), "https://x.test/a/b/")
        self.assertEqual(_abs_url("https://x.test", "https://x.test/c"), "https://x.test/c")
        self.assertEqual(_abs_url("https://x.test", ""), "")


class ConfigTests(unittest.TestCase):
    def test_enabled_platforms_gate_on_secrets(self):
        self.assertEqual(PublisherConfig().enabled_platforms(), [])
        cfg = PublisherConfig(tg_token="t", site_password="p", vk_token="v", vk_group_id="7")
        self.assertEqual(cfg.enabled_platforms(), ["telegram", "site", "vk"])
        # VK needs both token and group id
        self.assertEqual(PublisherConfig(vk_token="v").enabled_platforms(), [])

    def test_wildcar_org_goes_first(self):
        # telegram takes its picture link from wildcar.org, so the page must
        # be written and live before telegram's turn inside one run
        cfg = PublisherConfig(tg_token="t", wildcar_base="https://wildcar.org")
        self.assertEqual(cfg.enabled_platforms(), ["wildcar_org", "telegram"])


class WildcarOrgTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cfg = PublisherConfig(
            wildcar_base="https://wildcar.org",
            wildcar_content_dir=str(root / "content"),
            own_db=str(root / "own.sqlite3"),
            requests_dir=str(root / "requests"),
            wildcar_wait_seconds=0,
        )
        Path(self.cfg.requests_dir).mkdir()
        open_own_db(self.cfg.own_db).close()
        image = root / "1.jpg"
        image.write_bytes(b"\xff\xd8img")
        self.item = PreparedNews(
            news_id=7169, title="Заголовок дня.", paragraphs=["Абзац один.", "Абзац два."],
            lead_image=str(image), source_url="https://src.test/a", source_name="src.test",
            images=[(str(image), "Подпись к фото")],
        )

    def publish(self, status=200):
        with mock.patch.object(publisher, "http_send", return_value=(status, b"")):
            return publisher.publish_wildcar_org(self.cfg, self.item, dry_run=False)

    def test_writes_page_image_index_feed_and_marker(self):
        url = self.publish()
        self.assertEqual(url, "https://wildcar.org/news/7169/")
        section = Path(self.cfg.wildcar_content_dir) / "news"
        page = (section / "7169" / "index.md").read_text(encoding="utf-8")
        self.assertTrue(page.startswith("# Заголовок дня."))
        self.assertIn("![](1.jpg)", page)
        self.assertIn("*Подпись к фото*", page)
        self.assertIn("Абзац два.", page)
        self.assertIn("[src.test](https://src.test/a)", page)
        self.assertEqual((section / "7169" / "1.jpg").read_bytes(), b"\xff\xd8img")
        index = (section / "index.md").read_text(encoding="utf-8")
        self.assertIn("[Заголовок дня.](7169/index.md)", index)
        self.assertIn("(rss.xml)", index)
        self.assertIn("Позитивные новости", (section / ".nav.yml").read_text(encoding="utf-8"))
        self.assertTrue((Path(self.cfg.requests_dir) / "rebuild-wildcar-org").exists())

    def test_page_not_live_raises_and_files_stay_for_the_retry(self):
        with self.assertRaises(PublishError):
            self.publish(status=404)
        section = Path(self.cfg.wildcar_content_dir) / "news"
        self.assertTrue((section / "7169" / "index.md").exists())

    def test_dry_run_writes_nothing(self):
        out = publisher.publish_wildcar_org(self.cfg, self.item, dry_run=True)
        self.assertEqual(out, "(dry-run)")
        self.assertFalse(Path(self.cfg.wildcar_content_dir).exists())
        self.assertFalse((Path(self.cfg.requests_dir) / "rebuild-wildcar-org").exists())

    def test_feed_is_dzen_compliant_xml(self):
        self.publish()
        feed_path = Path(self.cfg.wildcar_content_dir) / "news" / "rss.xml"
        root = ET.fromstring(feed_path.read_text(encoding="utf-8"))
        self.assertEqual(root.tag, "rss")
        self.assertEqual(root.get("version"), "2.0")
        channel = root.find("channel")
        for tag in ("title", "link", "description", "language"):
            self.assertIsNotNone(channel.find(tag), tag)
        item = channel.find("item")
        self.assertEqual(item.findtext("title"), "Заголовок дня")  # no trailing period
        self.assertEqual(item.findtext("link"), "https://wildcar.org/news/7169/")
        self.assertEqual(item.findtext("guid"), "https://wildcar.org/news/7169/")
        self.assertEqual(item.findtext("category"), "Позитивные новости")
        self.assertRegex(item.findtext("pubDate"), r"^\w{3}, \d{2} \w{3} \d{4}")
        enclosure = item.find("enclosure")
        self.assertEqual(enclosure.get("url"), "https://wildcar.org/news/7169/1.jpg")
        self.assertEqual(enclosure.get("type"), "image/jpeg")
        body = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
        self.assertIn('<img src="https://wildcar.org/news/7169/1.jpg">', body)
        self.assertIn("<figcaption>Подпись к фото</figcaption>", body)
        self.assertIn("<p>Абзац один.</p>", body)
        self.assertIn('<a href="https://src.test/a">src.test</a>', body)

    def test_feed_and_index_remember_previous_items(self):
        # an earlier item, already recorded as published on wildcar_org
        earlier = PreparedNews(
            news_id=100, title="Старая новость", paragraphs=["Текст."],
            lead_image=None, source_url="https://old.test/x", source_name="old.test",
            images=[],
        )
        self.item, self.item2 = earlier, self.item
        self.publish()
        con = open_own_db(self.cfg.own_db)
        con.execute(
            "INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md) "
            "VALUES (100, 'published', 'Старая новость', ?)",
            ("# Старая новость\n\nТекст.\n\nИсточник: [old.test](https://old.test/x)\n",))
        con.commit()
        publisher.record_publication(
            con, 100, "wildcar_org", "ok", "https://wildcar.org/news/100/", None)
        con.close()
        self.item = self.item2
        self.publish()
        section = Path(self.cfg.wildcar_content_dir) / "news"
        index = (section / "index.md").read_text(encoding="utf-8")
        self.assertIn("[Заголовок дня.](7169/index.md)", index)
        self.assertIn("[Старая новость](100/index.md)", index)
        feed = (section / "rss.xml").read_text(encoding="utf-8")
        self.assertEqual(feed.count("<item>"), 2)
        # newest first: the fresh item precedes the old one
        self.assertLess(feed.find("7169"), feed.find(">Старая новость<"))


class WildcarBuildersTests(unittest.TestCase):
    ENTRY = WildcarEntry(
        news_id=5, title="Т", paragraphs=["А.", "Б."], source_url="https://s.test/a",
        source_name="s.test", images=[("1.jpg", ""), ("2.png", "Вторая")],
        published_at=datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc),
    )

    def test_page_places_lead_first_and_the_rest_after_text(self):
        page = build_wildcar_page(self.ENTRY, ZoneInfo("Europe/Moscow"))
        self.assertLess(page.find("![](1.jpg)"), page.find("А."))
        self.assertLess(page.find("Б."), page.find("![](2.png)"))
        self.assertIn("*Вторая*", page)
        self.assertIn("*27 июля 2026 · Источник: [s.test](https://s.test/a)*", page)

    def test_index_dates_in_the_window_zone(self):
        # 23:30 UTC on the 26th is already the 27th in Moscow
        entry = WildcarEntry(5, "Т", [], "", "", [],
                             datetime(2026, 7, 26, 23, 30, tzinfo=timezone.utc))
        index = build_wildcar_index([entry], ZoneInfo("Europe/Moscow"))
        self.assertIn("- 27.07.2026 — [Т](5/index.md)", index)

    def test_cdata_terminator_in_text_survives_the_feed(self):
        # "]]>" in a paragraph is html-escaped before the CDATA wrapper, so the
        # XML stays parsable and the HTML renders the original text back.
        entry = WildcarEntry(5, "Т", ["в тексте есть ]]> внутри"], "", "", [],
                             datetime(2026, 7, 27, tzinfo=timezone.utc))
        cfg = PublisherConfig(wildcar_base="https://wildcar.org")
        body = ET.fromstring(build_wildcar_feed(cfg, [entry])).find(
            "channel/item/{http://purl.org/rss/1.0/modules/content/}encoded")
        self.assertIn("]]&gt; внутри", body.text)


class TelegramSendTests(unittest.TestCase):
    def make_item(self, image_path: str | None):
        images = [(image_path, "")] if image_path else []
        return PreparedNews(
            news_id=7169, title="Т", paragraphs=["Абзац."], lead_image=image_path,
            source_url="https://s.test/a", source_name="s.test", images=images,
        )

    def test_image_goes_as_photo_upload_with_caption(self):
        cfg = PublisherConfig(tg_token="tok", tg_channel_username="posinus",
                              wildcar_base="https://wildcar.org")
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            f.write(b"\xff\xd8img")
            f.flush()
            sent = {}

            def fake_post(url, data, content_type, timeout):
                sent["url"], sent["data"] = url, data
                return {"ok": True, "result": {"message_id": 6}}

            with mock.patch.object(publisher, "_post_json_result", fake_post):
                out = publisher.publish_telegram(cfg, self.make_item(f.name), dry_run=False)
        self.assertEqual(out, "https://t.me/posinus/6")
        self.assertIn("/sendPhoto", sent["url"])
        self.assertIn("caption".encode(), sent["data"])
        self.assertIn(b"\xff\xd8img", sent["data"])

    def test_no_image_sends_text_with_the_preview_off(self):
        cfg = PublisherConfig(tg_token="tok", wildcar_base="https://wildcar.org")
        sent = {}

        def fake_post(url, data, content_type, timeout):
            sent["url"], sent["data"] = url, data
            return {"ok": True, "result": {"message_id": 7}}

        with mock.patch.object(publisher, "_post_json_result", fake_post):
            publisher.publish_telegram(cfg, self.make_item(None), dry_run=False)
        self.assertIn("/sendMessage", sent["url"])
        fields = urllib.parse.parse_qs(sent["data"].decode("utf-8"))
        preview = json.loads(fields["link_preview_options"][0])
        self.assertTrue(preview["is_disabled"])
        self.assertIn("<b>Т</b>", fields["text"][0])

    def test_long_text_without_image_respects_tg_text_limit(self):
        # 1500 is what the Дзен autopublisher carries over, so a text post is
        # cut there even though Telegram itself would allow 4096
        cfg = PublisherConfig(tg_token="tok", wildcar_base="https://wildcar.org")
        item = PreparedNews(
            news_id=7169, title="Т", paragraphs=["п" * 700] * 4, lead_image=None,
            source_url="https://s.test/a", source_name="s.test", images=[],
        )
        sent = {}

        def fake_post(url, data, content_type, timeout):
            sent["data"] = data
            return {"ok": True, "result": {"message_id": 8}}

        with mock.patch.object(publisher, "_post_json_result", fake_post):
            publisher.publish_telegram(cfg, item, dry_run=False)
        text = urllib.parse.parse_qs(sent["data"].decode("utf-8"))["text"][0]
        self.assertEqual(text.count("п" * 700), 2)  # 2 of 4 paragraphs fit in 1500
        self.assertIn('href="https://wildcar.org/news/7169/"', text)


class SitePublishTests(unittest.TestCase):
    """publish_site against a fake Эгея session: every picture is uploaded and
    the note carries the merged tags, mirroring the wildcar.org page."""

    FORM_PAGE = ('<div class="form-note"><input id="token" value="tok"/>'
                 '<input id="old-stamp" value="1"/></div>')

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.uploads = []
            self.forms = []

        def get(self, url, headers=None, max_redirects=5):
            return publisher._Response(200, {}, SitePublishTests.FORM_PAGE.encode(), url)

        def post_form(self, url, fields, headers=None):
            self.forms.append((url, fields))
            if "note-process" in url:
                return publisher._Response(302, {"Location": "/all/zametka/"}, b"", url)
            return publisher._Response(200, {}, b"", url)

        def post_multipart(self, url, fields, files, headers=None):
            self.uploads.append(files["file"][0])
            name = f"srv-{len(self.uploads)}.jpg"
            body = json.dumps({"success": True, "data": {"new-name": name}}).encode()
            return publisher._Response(200, {}, body, url)

    def test_uploads_every_picture_and_sends_merged_tags(self):
        # site_tags set explicitly: the default is empty (the owner asked for
        # exactly the item tags), but a configured base must still go first
        cfg = PublisherConfig(site_password="pw", site_tags="добрые новости")
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name in ("1.jpg", "2.jpg"):
                path = Path(tmp) / name
                path.write_bytes(b"\xff\xd8img")
                paths.append(str(path))
            item = PreparedNews(
                news_id=5, title="Т", paragraphs=["Абзац."], lead_image=paths[0],
                source_url="https://s.test/a", source_name="s.test",
                images=[(paths[0], "Подпись"), (paths[1], "")],
                tags=["экология", "позитивная", "новость", "позитивная новость"],
            )
            session = self.FakeSession()
            with mock.patch.object(publisher, "Session", lambda *a, **k: session):
                url = publisher.publish_site(cfg, item, dry_run=False)
        self.assertEqual(url, "https://wildcar.ru/all/zametka/")
        self.assertEqual(session.uploads, ["1.jpg", "2.jpg"])
        form = next(fields for u, fields in session.forms if "note-process" in u)
        self.assertEqual(form["tags[]"],
                         ["добрые новости", "экология", "позитивная", "новость",
                          "позитивная новость"])
        self.assertIn("srv-1.jpg\nПодпись", form["text"])
        self.assertIn("srv-2.jpg", form["text"])
        # the fake page carries no "Неопубликовано", so no note-publish round
        self.assertTrue(form["text"].index("Абзац.") < form["text"].index("srv-2.jpg"))


class LockedForFirstWrites:
    """Connection wrapper whose first N publication writes report a locked DB.

    sqlite3.Connection rejects attribute patching, so the retry path is exercised
    through a wrapper that delegates everything else to the real connection.
    """

    def __init__(self, con: sqlite3.Connection, fail_times: int):
        self._con = con
        self._left = fail_times
        self.refused = 0

    def __enter__(self):
        return self._con.__enter__()

    def __exit__(self, *exc):
        return self._con.__exit__(*exc)

    def execute(self, sql, *args):
        if sql.startswith("INSERT INTO publication") and self._left > 0:
            self._left -= 1
            self.refused += 1
            raise sqlite3.OperationalError("database is locked")
        return self._con.execute(sql, *args)


class OwnDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "own.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_publication_upserts_and_counts_attempts(self):
        con = open_own_db(self.path)
        con.execute("INSERT INTO prepared_item (news_id, status) VALUES (5, 'prepared')")
        con.commit()
        publisher.record_publication(con, 5, "telegram", "error", None, "boom")
        publisher.record_publication(con, 5, "telegram", "ok", "https://t.me/x/1", None)
        rows = con.execute("SELECT status, url, attempts FROM publication WHERE news_id=5").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(rows[0]["attempts"], 2)
        self.assertEqual(publication_status(con, 5), {"telegram": "ok"})
        con.close()

    def test_own_db_is_wal(self):
        """A reader must not be able to block the write that records a sent post."""
        con = open_own_db(self.path)
        self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        con.close()

    def test_record_publication_retries_while_locked(self):
        con = open_own_db(self.path)
        con.execute("INSERT INTO prepared_item (news_id, status) VALUES (7, 'prepared')")
        con.commit()
        flaky = LockedForFirstWrites(con, fail_times=2)

        with mock.patch.object(publisher.time, "sleep") as slept:
            publisher.record_publication(flaky, 7, "telegram", "ok", "https://t.me/x/2", None)

        self.assertEqual(flaky.refused, 2)
        self.assertEqual([call.args[0] for call in slept.call_args_list], [0.5, 1.0])
        row = con.execute("SELECT status, url FROM publication WHERE news_id=7").fetchone()
        self.assertEqual(row["status"], "ok")
        con.close()

    def test_unrecorded_ok_send_leaves_a_marker_and_raises(self):
        """A post that went out but could not be recorded must not stay silent."""
        con = open_own_db(self.path)
        con.execute("INSERT INTO prepared_item (news_id, status) VALUES (8, 'prepared')")
        con.commit()
        always_locked = LockedForFirstWrites(con, fail_times=99)

        with mock.patch.object(publisher.time, "sleep"):
            with self.assertRaises(sqlite3.OperationalError):
                publisher.record_publication(always_locked, 8, "vk", "ok", "https://vk.ru/wall-1_2", None)

        markers = list(Path(self.tmp.name).glob("unrecorded-8-vk-*.txt"))
        self.assertEqual(len(markers), 1)
        self.assertIn("https://vk.ru/wall-1_2", markers[0].read_text(encoding="utf-8"))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM publication").fetchone()[0], 0)
        con.close()

    def test_lead_image_follows_a_moved_media_dir(self):
        """Absolute paths go stale when media moves; the file is found anyway."""
        media = Path(self.tmp.name) / "media"
        (media / "42").mkdir(parents=True)
        (media / "42" / "1.jpg").write_bytes(b"pretend jpeg")
        con = open_own_db(self.path)
        con.execute("INSERT INTO prepared_item (news_id, status) VALUES (42, 'prepared')")
        con.execute(
            "INSERT INTO illustration (news_id, position, file_path) VALUES (42, 1, ?)",
            ("/var/lib/news-evaluator/media/42/1.jpg",),
        )
        con.commit()

        self.assertIsNone(publisher.lead_image_path(con, 42))
        self.assertEqual(publisher.lead_image_path(con, 42, str(media)),
                         str(media / "42" / "1.jpg"))
        con.close()

    def test_lead_image_none_when_the_file_is_gone_everywhere(self):
        con = open_own_db(self.path)
        con.execute("INSERT INTO prepared_item (news_id, status) VALUES (43, 'prepared')")
        con.execute(
            "INSERT INTO illustration (news_id, position, file_path) VALUES (43, 1, ?)",
            ("/gone/43/1.jpg",),
        )
        con.commit()
        self.assertIsNone(publisher.lead_image_path(con, 43, str(Path(self.tmp.name) / "media")))
        con.close()

    def test_mark_published(self):
        con = open_own_db(self.path)
        con.execute("INSERT INTO prepared_item (news_id, status) VALUES (9, 'prepared')")
        con.commit()
        publisher.mark_published(con, 9)
        row = con.execute("SELECT status, published_at FROM prepared_item WHERE news_id=9").fetchone()
        self.assertEqual(row["status"], "published")
        self.assertIsNotNone(row["published_at"])
        con.close()


class RunLoopTests(unittest.TestCase):
    """End-to-end run() with fake adapters: idempotency and label transition."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.own_path = str(Path(self.tmp.name) / "own.sqlite3")
        own = open_own_db(self.own_path)
        own.execute(
            # prepared_at must stay fresh: a fixed date will cross PUB_EXPIRE_AFTER_DAYS
            "INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md, prepared_at) "
            "VALUES (1, 'prepared', 'T', ?, ?)",
            ("# T\n\npara one\n\npara two\n\nИсточник: [site.test](https://site.test/a)",
             (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")),
        )
        own.commit()
        own.close()
        # telegram + site enabled, vk off
        # window off: these tests must behave the same whatever time they run at
        self.cfg = PublisherConfig(own_db=self.own_path, slots="", window_start="", requests_dir=str(Path(self.own_path).parent), tg_token="tok", site_password="pw")
        self._orig = dict(publisher.ADAPTERS)

    def tearDown(self):
        publisher.ADAPTERS.clear()
        publisher.ADAPTERS.update(self._orig)
        self.tmp.cleanup()

    def test_all_platforms_ok_marks_published(self):
        calls: list[str] = []
        publisher.ADAPTERS["telegram"] = lambda cfg, item, dry: (calls.append("tg"), "https://t.me/x/1")[1]
        publisher.ADAPTERS["site"] = lambda cfg, item, dry: (calls.append("site"), "https://site/x")[1]
        rc = publisher.run(self.cfg, limit=10, dry_run=False, only=None)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(calls), ["site", "tg"])
        con = open_own_db(self.own_path)
        self.assertEqual(con.execute("SELECT status FROM prepared_item WHERE news_id=1").fetchone()["status"], "published")
        con.close()

    def test_partial_failure_keeps_prepared_then_retries_only_failed(self):
        def ok(cfg, item, dry):
            return "https://ok"

        def boom(cfg, item, dry):
            raise PublishError("site down")

        publisher.ADAPTERS["telegram"] = ok
        publisher.ADAPTERS["site"] = boom
        rc = publisher.run(self.cfg, limit=10, dry_run=False, only=None)
        self.assertEqual(rc, 0)  # recorded platform failures do not fail the run
        con = open_own_db(self.own_path)
        self.assertEqual(con.execute("SELECT status FROM prepared_item WHERE news_id=1").fetchone()["status"], "prepared")
        self.assertEqual(publication_status(con, 1), {"telegram": "ok", "site": "error"})
        con.close()

        # second run: telegram is already ok, so only site is retried — make it succeed
        seen: list[str] = []
        publisher.ADAPTERS["telegram"] = lambda cfg, item, dry: (seen.append("tg"), "x")[1]
        publisher.ADAPTERS["site"] = lambda cfg, item, dry: (seen.append("site"), "https://site/ok")[1]
        rc = publisher.run(self.cfg, limit=10, dry_run=False, only=None)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["site"])  # telegram skipped, only the failed platform retried
        con = open_own_db(self.own_path)
        self.assertEqual(con.execute("SELECT status FROM prepared_item WHERE news_id=1").fetchone()["status"], "published")
        con.close()

    def test_dry_run_writes_nothing(self):
        publisher.ADAPTERS["telegram"] = lambda cfg, item, dry: "should-not-record"
        publisher.ADAPTERS["site"] = lambda cfg, item, dry: "should-not-record"
        publisher.run(self.cfg, limit=10, dry_run=True, only=None)
        con = open_own_db(self.own_path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM publication").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT status FROM prepared_item WHERE news_id=1").fetchone()["status"], "prepared")
        con.close()


class ThrottleAndRetryTests(unittest.TestCase):
    """Rate limit for new items + giving up on a failing platform (no head-of-line)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.own_path = str(Path(self.tmp.name) / "own.sqlite3")
        self._orig = dict(publisher.ADAPTERS)

    def tearDown(self):
        publisher.ADAPTERS.clear()
        publisher.ADAPTERS.update(self._orig)
        self.tmp.cleanup()

    def _prepare(self, con, news_id):
        # fresh and ordered by id: a fixed date would cross PUB_EXPIRE_AFTER_DAYS
        con.execute(
            "INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md, prepared_at) "
            "VALUES (?, 'prepared', 'T', ?, ?)",
            (news_id, f"# T\n\nтекст\n\nИсточник: [s.test](https://s.test/{news_id})",
             (datetime.now(timezone.utc) - timedelta(hours=24 - news_id)).isoformat(timespec="seconds")),
        )

    @staticmethod
    def _ago(**kw):
        return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")

    def _already_published(self, con, news_id, when):
        """A prior fully-published item plus its successful post at time `when`."""
        con.execute("INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md) "
                    "VALUES (?, 'published', 'X', '# X\n\ny')", (news_id,))
        con.execute("INSERT INTO publication (news_id, platform, status, url, attempts, updated_at) "
                    "VALUES (?, 'telegram', 'ok', 'u', 1, ?)", (news_id, when))

    def test_new_item_throttled_when_last_post_recent(self):
        con = open_own_db(self.own_path)
        self._prepare(con, 1)  # brand-new
        self._already_published(con, 99, self._ago(minutes=5))
        con.commit()
        con.close()
        calls = []
        publisher.ADAPTERS["telegram"] = lambda c, i, d: (calls.append(i.news_id), "u")[1]
        publisher.run(PublisherConfig(own_db=self.own_path, slots="", window_start="", requests_dir=str(Path(self.own_path).parent), tg_token="t"), limit=1, dry_run=False, only=None)
        self.assertEqual(calls, [])  # last post 5 min ago (< 120) -> new item held back

    def test_new_item_allowed_when_last_post_old(self):
        con = open_own_db(self.own_path)
        self._prepare(con, 1)
        self._already_published(con, 99, self._ago(hours=3))
        con.commit()
        con.close()
        calls = []
        publisher.ADAPTERS["telegram"] = lambda c, i, d: (calls.append(i.news_id), "u")[1]
        publisher.run(PublisherConfig(own_db=self.own_path, slots="", window_start="", requests_dir=str(Path(self.own_path).parent), tg_token="t"), limit=1, dry_run=False, only=None)
        self.assertEqual(calls, [1])  # last post 3h ago (> 120) -> allowed

    def test_only_one_new_item_per_run(self):
        con = open_own_db(self.own_path)
        self._prepare(con, 1)
        self._prepare(con, 2)
        con.commit()
        con.close()
        calls = []
        publisher.ADAPTERS["telegram"] = lambda c, i, d: (calls.append(i.news_id), "u")[1]
        publisher.run(PublisherConfig(own_db=self.own_path, slots="", window_start="", requests_dir=str(Path(self.own_path).parent), tg_token="t"), limit=1, dry_run=False, only=None)
        self.assertEqual(calls, [1])  # only the first (oldest) new item; the second waits

    def test_failing_platform_gives_up_and_does_not_block_others(self):
        con = open_own_db(self.own_path)
        # item 1: already public on telegram, site failed and is out of attempts
        self._prepare(con, 1)
        con.execute("INSERT INTO publication (news_id, platform, status, url, attempts, updated_at) "
                    "VALUES (1, 'telegram', 'ok', 'u', 1, ?)", (self._ago(hours=5),))
        con.execute("INSERT INTO publication (news_id, platform, status, error, attempts, updated_at) "
                    "VALUES (1, 'site', 'error', 'boom', 8, ?)", (self._ago(hours=5),))
        self._prepare(con, 2)  # brand-new
        con.commit()
        con.close()
        calls = []
        publisher.ADAPTERS["telegram"] = lambda c, i, d: (calls.append(("tg", i.news_id)), "u")[1]
        publisher.ADAPTERS["site"] = lambda c, i, d: (calls.append(("site", i.news_id)), "u")[1]
        cfg = PublisherConfig(own_db=self.own_path, slots="", window_start="", requests_dir=str(Path(self.own_path).parent), tg_token="t", site_password="p", max_attempts=8)
        publisher.run(cfg, limit=1, dry_run=False, only=None)
        con = open_own_db(self.own_path)
        # item 1 gave up on the exhausted platform and was finalized, not retried
        self.assertEqual(con.execute("SELECT status FROM prepared_item WHERE news_id=1").fetchone()["status"], "published")
        self.assertNotIn(("site", 1), calls)
        # the new item 2 was not blocked by item 1
        self.assertEqual(con.execute("SELECT status FROM prepared_item WHERE news_id=2").fetchone()["status"], "published")
        con.close()

    def test_news_id_override_ignores_throttle(self):
        con = open_own_db(self.own_path)
        self._prepare(con, 1)
        self._already_published(con, 99, self._ago(minutes=1))
        con.commit()
        con.close()
        calls = []
        publisher.ADAPTERS["telegram"] = lambda c, i, d: (calls.append(i.news_id), "u")[1]
        publisher.run(PublisherConfig(own_db=self.own_path, slots="", window_start="", requests_dir=str(Path(self.own_path).parent), tg_token="t"), limit=1, dry_run=False, only=1)
        self.assertEqual(calls, [1])  # explicit --news-id bypasses the rate limit


class PauseAndWindowTests(unittest.TestCase):
    """Stop cock in the mailbox and the publication window."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.own_path = str(Path(self.tmp.name) / "own.sqlite3")
        self.requests_dir = Path(self.tmp.name) / "requests"
        self.requests_dir.mkdir()
        con = open_own_db(self.own_path)
        con.execute(
            # prepared_at must stay fresh: a fixed date will cross PUB_EXPIRE_AFTER_DAYS
            "INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md, prepared_at) "
            "VALUES (1, 'prepared', 'T', ?, ?)",
            ("# T\n\nтекст\n\nИсточник: [s.test](https://s.test/1)",
             (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")),
        )
        con.commit()
        con.close()
        self._orig = dict(publisher.ADAPTERS)

    def tearDown(self):
        publisher.ADAPTERS.clear()
        publisher.ADAPTERS.update(self._orig)
        self.tmp.cleanup()

    def _cfg(self, **kw):
        base = dict(own_db=self.own_path, tg_token="t", slots="", window_start="",
                    requests_dir=str(self.requests_dir))
        base.update(kw)
        return PublisherConfig(**base)

    def _write_pause(self, text):
        (self.requests_dir / publisher.PAUSE_FILE).write_text(text, encoding="utf-8")

    def _run(self, cfg=None, only=None):
        calls: list[int] = []
        publisher.ADAPTERS["telegram"] = lambda c, i, d: (calls.append(i.news_id), "u")[1]
        publisher.run(cfg or self._cfg(), limit=1, dry_run=False, only=only)
        return calls

    def test_pause_without_deadline_sends_nothing(self):
        self._write_pause("reason=день траура\n")
        self.assertEqual(self._run(), [])

    def test_pause_holds_even_an_explicit_news_id(self):
        self._write_pause("reason=день траура\n")
        self.assertEqual(self._run(only=1), [])

    def test_future_deadline_keeps_the_pause(self):
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self._write_pause(f"until={until}\nreason=пауза на час\n")
        self.assertEqual(self._run(), [])
        self.assertTrue((self.requests_dir / publisher.PAUSE_FILE).exists())

    def test_expired_pause_is_removed_and_publication_resumes(self):
        until = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self._write_pause(f"until={until}\n")
        self.assertEqual(self._run(), [1])
        self.assertFalse((self.requests_dir / publisher.PAUSE_FILE).exists())

    def test_missing_mailbox_does_not_stop_publication(self):
        cfg = self._cfg(requests_dir=str(Path(self.tmp.name) / "nope"))
        self.assertEqual(self._run(cfg), [1])

    def test_unparsable_pause_file_still_stops_publication(self):
        self._write_pause("until=не дата\n")
        self.assertEqual(self._run(), [])

    def test_new_item_waits_for_the_window(self):
        # 03:40 Moscow: the window is closed, the queue keeps the item
        with mock.patch.object(publisher, "window_state",
                               return_value=(False, datetime(2026, 7, 26, 5, 0, tzinfo=timezone.utc))):
            self.assertEqual(self._run(self._cfg(window_start="08:00", window_end="22:00")), [])

    def test_closed_window_does_not_hold_an_already_public_item(self):
        con = open_own_db(self.own_path)
        con.execute("INSERT INTO publication (news_id, platform, status, url, attempts, updated_at) "
                    "VALUES (1, 'site', 'ok', 'u', 1, '2026-07-23T10:00:00+00:00')")
        con.commit()
        con.close()
        cfg = self._cfg(site_password="p")
        with mock.patch.object(publisher, "window_state", return_value=(False, None)):
            self.assertEqual(self._run(cfg), [1])  # telegram still finishes the item

    def test_window_bounds_and_next_opening(self):
        cfg = PublisherConfig(window_start="08:00", window_end="22:00", window_tz="Europe/Moscow")
        midday = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)      # 12:00 MSK
        night = datetime(2026, 7, 26, 0, 40, tzinfo=timezone.utc)      # 03:40 MSK
        self.assertEqual(publisher.window_state(cfg, midday), (True, None))
        is_open, opens = publisher.window_state(cfg, night)
        self.assertFalse(is_open)
        self.assertEqual(opens, datetime(2026, 7, 26, 5, 0, tzinfo=timezone.utc))  # 08:00 MSK

    def test_window_can_wrap_past_midnight_and_can_be_switched_off(self):
        wrapping = PublisherConfig(window_start="22:00", window_end="06:00", window_tz="UTC")
        self.assertTrue(publisher.window_state(wrapping, datetime(2026, 7, 25, 23, 0, tzinfo=timezone.utc))[0])
        self.assertTrue(publisher.window_state(wrapping, datetime(2026, 7, 25, 2, 0, tzinfo=timezone.utc))[0])
        self.assertFalse(publisher.window_state(wrapping, datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc))[0])
        off = PublisherConfig(window_start="", window_end="22:00")
        self.assertEqual(off_state := publisher.window_state(off, datetime(2026, 7, 25, 2, 0, tzinfo=timezone.utc)), (True, None))
        self.assertEqual(off_state, (True, None))


class SlotGridTests(unittest.TestCase):
    """The slot grid: fixed local times, one fresh item per slot."""

    CFG = PublisherConfig(window_tz="Europe/Moscow")  # default slots 09:00–23:00

    @staticmethod
    def _utc(hour, minute=0, day=3):
        return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)

    def test_parse_slots(self):
        self.assertEqual(publisher._parse_slots("09:00, 23:00"),
                         [dt_time(9, 0), dt_time(23, 0)])
        self.assertEqual(publisher._parse_slots("23:00,09:00,09:00"),
                         [dt_time(9, 0), dt_time(23, 0)])
        self.assertEqual(publisher._parse_slots(""), [])
        self.assertEqual(publisher._parse_slots("09:00,кабанчик"), [])

    def test_slot_opens_at_its_exact_time(self):
        allowed, grid_open, note = publisher.slot_state(self.CFG, self._utc(6, 0), None)  # 09:00 MSK
        self.assertEqual((allowed, grid_open, note), (True, True, "slot 09:00 open"))

    def test_slot_takes_exactly_one_item(self):
        # posted at 09:05 MSK: slot 09:00 is served until 11:00
        allowed, grid_open, note = publisher.slot_state(self.CFG, self._utc(7, 0), self._utc(6, 5))
        self.assertEqual((allowed, grid_open), (False, True))
        self.assertEqual(note, "slot 09:00 served, next 11:00")
        # 11:00 MSK: a fresh slot
        self.assertTrue(publisher.slot_state(self.CFG, self._utc(8, 0), self._utc(6, 5))[0])

    def test_missed_minute_still_posts_inside_the_slot(self):
        # 09:47 MSK, nothing posted since yesterday: the 09:00 issue still goes out
        self.assertTrue(publisher.slot_state(self.CFG, self._utc(6, 47), self._utc(17, 0, day=2))[0])

    def test_grid_closed_between_midnight_and_the_first_slot(self):
        allowed, grid_open, note = publisher.slot_state(self.CFG, self._utc(3, 0), None)  # 06:00 MSK
        self.assertEqual((allowed, grid_open), (False, False))
        self.assertIn("09:00", note)

    def test_last_slot_ends_at_midnight(self):
        # 23:59 MSK with the 23:00 issue out: served, next is tomorrow
        allowed, _, note = publisher.slot_state(self.CFG, self._utc(20, 59), self._utc(20, 1))
        self.assertFalse(allowed)
        self.assertEqual(note, "slot 23:00 served, next 09:00 tomorrow")
        # 23:59 MSK with the last post in the 21:00 slot: 23:00 still unserved
        self.assertTrue(publisher.slot_state(self.CFG, self._utc(20, 59), self._utc(18, 1))[0])
        # 00:30 MSK: closed until 09:00
        self.assertFalse(publisher.slot_state(self.CFG, self._utc(21, 30), self._utc(20, 1))[1])


class SlotGridRunTests(unittest.TestCase):
    """run() under the grid: new items obey the slot, retries do not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.own_path = str(Path(self.tmp.name) / "own.sqlite3")
        con = open_own_db(self.own_path)
        for news_id in (1, 2):
            con.execute(
                "INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md, prepared_at) "
                "VALUES (?, 'prepared', 'T', ?, ?)",
                (news_id, f"# T\n\nтекст\n\nИсточник: [s.test](https://s.test/{news_id})",
                 (datetime.now(timezone.utc) - timedelta(hours=24 - news_id)).isoformat(timespec="seconds")),
            )
        con.commit()
        con.close()
        self._orig = dict(publisher.ADAPTERS)

    def tearDown(self):
        publisher.ADAPTERS.clear()
        publisher.ADAPTERS.update(self._orig)
        self.tmp.cleanup()

    def _cfg(self, **kw):
        base = dict(own_db=self.own_path, tg_token="t",
                    requests_dir=str(Path(self.own_path).parent))
        base.update(kw)
        return PublisherConfig(**base)

    def _run(self, state, cfg=None, limit=2):
        calls: list[int] = []
        publisher.ADAPTERS["telegram"] = lambda c, i, d: (calls.append(i.news_id), "u")[1]
        with mock.patch.object(publisher, "slot_state", return_value=state):
            publisher.run(cfg or self._cfg(), limit=limit, dry_run=False, only=None)
        return calls

    def test_served_slot_holds_new_items(self):
        self.assertEqual(self._run((False, True, "slot 09:00 served, next 11:00")), [])

    def test_open_slot_posts_exactly_one_item(self):
        self.assertEqual(self._run((True, True, "slot 09:00 open")), [1])

    def test_closed_grid_does_not_hold_an_already_public_item(self):
        con = open_own_db(self.own_path)
        con.execute("INSERT INTO publication (news_id, platform, status, url, attempts, updated_at) "
                    "VALUES (1, 'site', 'ok', 'u', 1, '2026-07-23T10:00:00+00:00')")
        con.commit()
        con.close()
        cfg = self._cfg(site_password="p")
        # item 1 is public on the site already, so telegram finishes it at 03:00 too
        self.assertEqual(self._run((False, False, "slots closed, first at 09:00"), cfg), [1])

    def test_empty_slots_fall_back_to_the_interval(self):
        # the grid is off and the window is off: the interval alone paces new items
        calls: list[int] = []
        publisher.ADAPTERS["telegram"] = lambda c, i, d: (calls.append(i.news_id), "u")[1]
        publisher.run(self._cfg(slots="", window_start=""), limit=1, dry_run=False, only=None)
        self.assertEqual(calls, [1])


class QueueOrderTests(unittest.TestCase):
    """Strongest first, the operator's hand above that, held items not at all."""

    class Row(dict):
        def __getitem__(self, key):
            return dict.__getitem__(self, key)

    def _rows(self, *news_ids):
        return [self.Row(news_id=news_id) for news_id in news_ids]

    def setUp(self):
        self.now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)

    def test_strength_beats_preparation_order(self):
        plan = {
            1: publisher.PlanRow(strength=3.0),
            2: publisher.PlanRow(strength=8.5),
            3: publisher.PlanRow(strength=6.0),
        }

        ordered = publisher.order_queue(self._rows(1, 2, 3), plan, self.now)

        self.assertEqual([row["news_id"] for row in ordered], [2, 3, 1])

    def test_operator_rank_wins_over_strength(self):
        plan = {
            1: publisher.PlanRow(strength=3.0, operator_rank=-1),   # moved up
            2: publisher.PlanRow(strength=8.5),
            3: publisher.PlanRow(strength=6.0, operator_rank=1),    # moved down
        }

        ordered = publisher.order_queue(self._rows(1, 2, 3), plan, self.now)

        self.assertEqual([row["news_id"] for row in ordered], [1, 2, 3])

    def test_held_and_dropped_items_leave_the_queue(self):
        plan = {
            1: publisher.PlanRow(strength=9.0, hold_until="2026-07-25T18:00:00+00:00"),
            2: publisher.PlanRow(strength=8.0, dropped_at="2026-07-25T09:00:00+00:00"),
            3: publisher.PlanRow(strength=1.0, hold_until="2026-07-25T09:00:00+00:00"),  # hold expired
        }

        ordered = publisher.order_queue(self._rows(1, 2, 3), plan, self.now)

        self.assertEqual([row["news_id"] for row in ordered], [3])

    def test_no_plan_keeps_preparation_order(self):
        """An older crawler, or a DB the publisher cannot read: behave as before."""
        ordered = publisher.order_queue(self._rows(5, 6, 7), {}, self.now)

        self.assertEqual([row["news_id"] for row in ordered], [5, 6, 7])

    def test_missing_crawler_db_is_not_fatal(self):
        self.assertEqual(publisher.load_plan("/nonexistent/posinus.sqlite3"), {})


class ExpiryTests(unittest.TestCase):
    """Ten days in the queue and the news is not news any more.

    The boundary matters more than the rule: an item must leave the queue before
    anything deletes its pictures, and it must not leave the queue while it is
    still half published or deliberately held.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "own.sqlite3")
        self.con = open_own_db(self.path)
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _prepared(self, news_id, age_days):
        moment = (self.now - timedelta(days=age_days)).isoformat()
        self.con.execute(
            "INSERT INTO prepared_item (news_id, status, retold_title, prepared_at) "
            "VALUES (?, 'prepared', ?, ?)",
            (news_id, f"Новость {news_id}", moment),
        )
        self.con.commit()

    def _queue(self):
        return self.con.execute(publisher.PREPARED_SQL).fetchall()

    def _status(self, news_id):
        return self.con.execute(
            "SELECT status, expired_at FROM prepared_item WHERE news_id = ?", (news_id,)
        ).fetchone()

    def test_an_item_past_the_period_leaves_the_queue(self):
        self._prepared(1, age_days=11)

        expired = publisher.expire_stale(self.con, self._queue(), {}, self.now, 10)

        self.assertEqual(expired, [1])
        row = self._status(1)
        self.assertEqual(row["status"], "expired")
        self.assertTrue(row["expired_at"])

    def test_an_item_inside_the_period_stays(self):
        """9.5 days is still publishable, and its pictures must still be there."""
        self._prepared(1, age_days=9)
        self._prepared(2, age_days=9.5)

        self.assertEqual(publisher.expire_stale(self.con, self._queue(), {}, self.now, 10), [])
        self.assertEqual(self._status(1)["status"], "prepared")
        self.assertEqual(self._status(2)["status"], "prepared")

    def test_exactly_ten_days_counts_as_waited_too_long(self):
        self._prepared(1, age_days=10)

        self.assertEqual(publisher.expire_stale(self.con, self._queue(), {}, self.now, 10), [1])

    def test_a_half_published_item_is_finished_not_dropped(self):
        """Half a post in public is worse than a late one; the rest are retries."""
        self._prepared(1, age_days=30)
        publisher.record_publication(self.con, 1, "telegram", "ok", "https://t.me/x", None)

        self.assertEqual(publisher.expire_stale(self.con, self._queue(), {}, self.now, 10), [])
        self.assertEqual(self._status(1)["status"], "prepared")

    def test_an_item_the_operator_holds_survives_its_date(self):
        self._prepared(1, age_days=30)
        plan = {1: publisher.PlanRow(hold_until="2026-08-09T12:00:00+00:00")}

        self.assertEqual(publisher.expire_stale(self.con, self._queue(), plan, self.now, 10), [])

    def test_it_expires_once_the_hold_has_passed(self):
        self._prepared(1, age_days=30)
        plan = {1: publisher.PlanRow(hold_until="2026-08-01T12:00:00+00:00")}

        self.assertEqual(publisher.expire_stale(self.con, self._queue(), plan, self.now, 10), [1])

    def test_dry_run_changes_nothing(self):
        self._prepared(1, age_days=30)

        publisher.expire_stale(self.con, self._queue(), {}, self.now, 10, dry_run=True)

        self.assertEqual(self._status(1)["status"], "prepared")

    def test_the_period_can_be_switched_off(self):
        self._prepared(1, age_days=300)

        self.assertEqual(publisher.expire_stale(self.con, self._queue(), {}, self.now, 0), [])

    def test_publishing_twice_keeps_the_first_date(self):
        """A retry pass must not move an old post to today."""
        self._prepared(1, age_days=30)
        publisher.mark_published(self.con, 1)
        first = self.con.execute("SELECT published_at FROM prepared_item WHERE news_id = 1").fetchone()[0]

        publisher.mark_published(self.con, 1)

        again = self.con.execute("SELECT published_at FROM prepared_item WHERE news_id = 1").fetchone()[0]
        self.assertEqual(first, again)


if __name__ == "__main__":
    unittest.main()
