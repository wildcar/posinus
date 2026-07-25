"""Unit tests for publisher.py: content builders, HTTP encoding, own DB, run loop.

No network: platform sends are exercised through monkeypatched adapters."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import publisher
from publisher import (
    PublishError,
    PublisherConfig,
    build_site_text,
    build_tg_caption,
    build_vk_message,
    encode_multipart,
    input_val,
    open_own_db,
    parse_markdown,
    publication_status,
    source_name_from_url,
    _abs_url,
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


class TelegramCaptionTests(unittest.TestCase):
    def test_structure_and_escaping(self):
        cap = build_tg_caption("A & B", ["one", "two"], "https://site.test/a", "site.test", 1024)
        self.assertIn("<b>A &amp; B</b>", cap)
        self.assertIn("one\n\ntwo", cap)
        self.assertIn('<a href="https://site.test/a">Источник: site.test</a>', cap)

    def test_truncates_paragraphs_to_fit(self):
        paras = ["x" * 500, "y" * 500, "z" * 500]
        cap = build_tg_caption("T", paras, "https://s.test/a", "s.test", 1024)
        self.assertLessEqual(len(cap), 1024)
        # the link (source) is kept even when paragraphs are dropped
        self.assertIn("Источник", cap)

    def test_title_only_when_nothing_fits(self):
        cap = build_tg_caption("Title", ["x" * 5000], "", "", 1024)
        self.assertLessEqual(len(cap), 1024)
        self.assertIn("Title", cap)


class VkAndSiteTextTests(unittest.TestCase):
    def test_vk_message_has_title_body_source(self):
        msg = build_vk_message("Заголовок", ["a", "b"], "https://s.test/a", "s.test")
        self.assertTrue(msg.startswith("Заголовок"))
        self.assertIn("a\n\nb", msg)
        self.assertIn("Источник: https://s.test/a", msg)

    def test_site_text_neasden_markup(self):
        text = build_site_text("pic.jpg", ["a", "b"], "https://s.test/a", "s.test")
        self.assertTrue(text.startswith("pic.jpg\n\n"))
        self.assertIn("Источник: ((https://s.test/a s.test))", text)

    def test_site_text_without_image(self):
        text = build_site_text("", ["a"], "https://s.test/a", "s.test")
        self.assertFalse(text.startswith("\n"))
        self.assertTrue(text.startswith("a"))


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
            "INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md, prepared_at) "
            "VALUES (1, 'prepared', 'T', ?, '2026-07-23T10:00:00')",
            ("# T\n\npara one\n\npara two\n\nИсточник: [site.test](https://site.test/a)",),
        )
        own.commit()
        own.close()
        # telegram + site enabled, vk off
        # window off: these tests must behave the same whatever time they run at
        self.cfg = PublisherConfig(own_db=self.own_path, window_start="", requests_dir=str(Path(self.own_path).parent), tg_token="tok", site_password="pw")
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
        con.execute(
            "INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md, prepared_at) "
            "VALUES (?, 'prepared', 'T', ?, ?)",
            (news_id, f"# T\n\nтекст\n\nИсточник: [s.test](https://s.test/{news_id})",
             f"2026-07-23T{news_id:02d}:00:00"),
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
        publisher.run(PublisherConfig(own_db=self.own_path, window_start="", requests_dir=str(Path(self.own_path).parent), tg_token="t"), limit=1, dry_run=False, only=None)
        self.assertEqual(calls, [])  # last post 5 min ago (< 120) -> new item held back

    def test_new_item_allowed_when_last_post_old(self):
        con = open_own_db(self.own_path)
        self._prepare(con, 1)
        self._already_published(con, 99, self._ago(hours=3))
        con.commit()
        con.close()
        calls = []
        publisher.ADAPTERS["telegram"] = lambda c, i, d: (calls.append(i.news_id), "u")[1]
        publisher.run(PublisherConfig(own_db=self.own_path, window_start="", requests_dir=str(Path(self.own_path).parent), tg_token="t"), limit=1, dry_run=False, only=None)
        self.assertEqual(calls, [1])  # last post 3h ago (> 120) -> allowed

    def test_only_one_new_item_per_run(self):
        con = open_own_db(self.own_path)
        self._prepare(con, 1)
        self._prepare(con, 2)
        con.commit()
        con.close()
        calls = []
        publisher.ADAPTERS["telegram"] = lambda c, i, d: (calls.append(i.news_id), "u")[1]
        publisher.run(PublisherConfig(own_db=self.own_path, window_start="", requests_dir=str(Path(self.own_path).parent), tg_token="t"), limit=1, dry_run=False, only=None)
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
        cfg = PublisherConfig(own_db=self.own_path, window_start="", requests_dir=str(Path(self.own_path).parent), tg_token="t", site_password="p", max_attempts=8)
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
        publisher.run(PublisherConfig(own_db=self.own_path, window_start="", requests_dir=str(Path(self.own_path).parent), tg_token="t"), limit=1, dry_run=False, only=1)
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
            "INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md, prepared_at) "
            "VALUES (1, 'prepared', 'T', ?, '2026-07-23T10:00:00')",
            ("# T\n\nтекст\n\nИсточник: [s.test](https://s.test/1)",),
        )
        con.commit()
        con.close()
        self._orig = dict(publisher.ADAPTERS)

    def tearDown(self):
        publisher.ADAPTERS.clear()
        publisher.ADAPTERS.update(self._orig)
        self.tmp.cleanup()

    def _cfg(self, **kw):
        base = dict(own_db=self.own_path, tg_token="t", window_start="",
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


if __name__ == "__main__":
    unittest.main()
