"""Картина дня: the slot gate, the two-stage generation and the publish flow."""

from __future__ import annotations

import base64
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import daypic
import evaluator
import publisher
import retention

# 10:00 Moscow on 2026-07-29 — past the default slot time in most tests.
NOW = datetime(2026, 7, 29, 7, 0, tzinfo=timezone.utc)
MSK = ZoneInfo("Europe/Moscow")
DAY = "2026-07-29"

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 4000

SLOT_COLUMNS = (
    "slot", "enabled", "title", "generate_at", "prompt", "system_prompt", "styles",
    "chat_provider", "chat_model", "image_provider", "image_model", "image_size",
)


def make_slot(**overrides) -> daypic.Slot:
    values = dict(
        slot="day", title="Картина дня", generate_at="08:00",
        prompt="Подготовь промпт для картины дня.", system_prompt="Ты готовишь промпт.",
        styles=("low-poly", "vaporwave", "surrealism"),
    )
    values.update(overrides)
    return daypic.Slot(**values)


class StyleAndGateTests(unittest.TestCase):
    def test_style_follows_the_day_of_month(self):
        styles = ("a", "b", "c")
        self.assertEqual(daypic.pick_style(styles, 1), "a")
        self.assertEqual(daypic.pick_style(styles, 3), "c")

    def test_a_day_past_the_list_runs_into_the_last_style(self):
        self.assertEqual(daypic.pick_style(("a", "b"), 31), "b")

    def test_no_styles_means_no_style(self):
        self.assertEqual(daypic.pick_style((), 5), "")

    def test_the_slot_waits_for_its_time(self):
        slot = make_slot(generate_at="08:00")
        before = datetime(2026, 7, 29, 7, 59, tzinfo=MSK)
        after = datetime(2026, 7, 29, 8, 0, tzinfo=MSK)
        self.assertFalse(daypic.slot_due(slot, before))
        self.assertTrue(daypic.slot_due(slot, after))

    def test_an_unparsable_time_fails_towards_generating(self):
        slot = make_slot(generate_at="skoro")
        self.assertTrue(daypic.slot_due(slot, datetime(2026, 7, 29, 0, 1, tzinfo=MSK)))


class PromptTests(unittest.TestCase):
    def test_the_request_carries_date_style_and_the_task(self):
        slot = make_slot()
        request = daypic.build_prompt_request(slot, NOW.astimezone(MSK))
        self.assertIn("2026-07-29", request)
        self.assertIn("среда", request)
        self.assertIn("День месяца: 29", request)
        self.assertIn("surrealism", request)  # day 29 clamps to the last of three
        self.assertIn(slot.prompt, request)

    def test_the_chat_answer_becomes_the_prompt(self):
        cfg = evaluator.Config()
        with mock.patch.object(evaluator, "chat",
                               return_value={"text": " готовый промпт ", "model_id": "m1"}):
            prompt, model = daypic.build_prompt(cfg, make_slot(), NOW.astimezone(MSK))
        self.assertEqual(prompt, "готовый промпт")
        self.assertEqual(model, "m1")

    def test_slot_model_hints_override_the_config(self):
        cfg = evaluator.Config(provider="deepseek", model_id="deepseek-v4-pro")
        seen = {}

        def fake_chat(chat_cfg, messages):
            seen["provider"], seen["model"] = chat_cfg.provider, chat_cfg.model_id
            return {"text": "p", "model_id": "m"}

        with mock.patch.object(evaluator, "chat", side_effect=fake_chat):
            daypic.build_prompt(cfg, make_slot(chat_provider="codex-oauth", chat_model="gpt-6"),
                                NOW.astimezone(MSK))
        self.assertEqual(seen, {"provider": "codex-oauth", "model": "gpt-6"})

    def test_two_failures_fall_back_to_the_template(self):
        cfg = evaluator.Config()
        with mock.patch.object(evaluator, "chat", side_effect=evaluator.McpError("down")):
            prompt, model = daypic.build_prompt(cfg, make_slot(), NOW.astimezone(MSK))
        self.assertEqual(model, "fallback-template")
        self.assertIn("2026-07-29", prompt)
        self.assertIn("surrealism", prompt)


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = daypic.DaypicConfig(daypic_dir=self.tmp.name, image_provider="codex-oauth")
        self.router = evaluator.Config()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_picture_lands_under_a_stable_name(self):
        reply = {"image_b64": [base64.b64encode(PNG).decode()], "model_id": "gpt-image-2"}
        with mock.patch.object(evaluator, "call_tool", return_value=reply) as call:
            path, model = daypic.generate_picture(self.cfg, self.router, make_slot(), "prompt", DAY)
        self.assertEqual(Path(path).name, f"{DAY}-day.png")
        self.assertEqual(Path(path).read_bytes(), PNG)
        self.assertEqual(model, "gpt-image-2")
        arguments = call.call_args.args[2]
        self.assertEqual(arguments["provider"], "codex-oauth")
        self.assertEqual(arguments["params"], {"size": "1024x1536"})

    def test_a_refusal_retries_once_with_the_safe_suffix(self):
        reply = {"image_b64": [base64.b64encode(PNG).decode()], "model_id": "m"}
        calls = []

        def fake_call(url, tool, arguments, token=None):
            calls.append(arguments["prompt"])
            if len(calls) == 1:
                raise evaluator.McpError("safety refusal")
            return reply

        with mock.patch.object(evaluator, "call_tool", side_effect=fake_call):
            daypic.generate_picture(self.cfg, self.router, make_slot(), "prompt", DAY)
        self.assertEqual(len(calls), 2)
        self.assertIn(daypic.SAFE_SUFFIX, calls[1])

    def test_two_refusals_fail_the_day(self):
        with mock.patch.object(evaluator, "call_tool", side_effect=evaluator.McpError("no")):
            with self.assertRaises(daypic.DaypicError):
                daypic.generate_picture(self.cfg, self.router, make_slot(), "prompt", DAY)

    def test_an_implausibly_small_image_is_rejected(self):
        reply = {"image_b64": [base64.b64encode(b"tiny").decode()]}
        with mock.patch.object(evaluator, "call_tool", return_value=reply):
            with self.assertRaises(daypic.DaypicError):
                daypic.generate_picture(self.cfg, self.router, make_slot(), "prompt", DAY)


class SlotLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.news_db = str(Path(self.tmp.name) / "news.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_table(self, rows=()):
        con = sqlite3.connect(self.news_db)
        con.execute(
            "CREATE TABLE exchange_daypic_slot (slot TEXT PRIMARY KEY, enabled INTEGER, "
            "title TEXT, generate_at TEXT, prompt TEXT, system_prompt TEXT, styles TEXT, "
            "chat_provider TEXT, chat_model TEXT, image_provider TEXT, image_model TEXT, "
            "image_size TEXT)"
        )
        con.executemany(
            f"INSERT INTO exchange_daypic_slot ({', '.join(SLOT_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(SLOT_COLUMNS))})", rows,
        )
        con.commit()
        con.close()

    def test_a_database_without_the_table_means_no_slots(self):
        sqlite3.connect(self.news_db).close()
        self.assertEqual(daypic.load_slots(self.news_db), [])

    def test_only_enabled_slots_are_loaded(self):
        self._create_table([
            ("day", 1, "Картина дня", "08:00", "задание", "система", "a\nb\n",
             "", "", "", "", ""),
            ("evening", 0, "Картина вечера", "20:00", "з", "с", "", "", "", "", "", ""),
        ])
        slots = daypic.load_slots(self.news_db)
        self.assertEqual([slot.slot for slot in slots], ["day"])
        self.assertEqual(slots[0].styles, ("a", "b"))


class RunTests(unittest.TestCase):
    """The whole pass over a real (temporary) pair of databases; only the
    router and the platform adapters are faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.news_db = str(root / "news.sqlite3")
        self.own_db = str(root / "own.sqlite3")
        self.requests = root / "requests"
        self.requests.mkdir()
        con = sqlite3.connect(self.news_db)
        con.execute(
            "CREATE TABLE exchange_daypic_slot (slot TEXT PRIMARY KEY, enabled INTEGER, "
            "title TEXT, generate_at TEXT, prompt TEXT, system_prompt TEXT, styles TEXT, "
            "chat_provider TEXT, chat_model TEXT, image_provider TEXT, image_model TEXT, "
            "image_size TEXT)"
        )
        con.execute(
            "INSERT INTO exchange_daypic_slot VALUES "
            "('day', 1, 'Картина дня', '08:00', 'задание', 'система', 'low-poly', "
            "'', '', '', '', '')"
        )
        con.commit()
        con.close()
        self.cfg = daypic.DaypicConfig(
            news_db=self.news_db, own_db=self.own_db,
            daypic_dir=str(root / "daypic"),
        )
        daypic.open_own_db(self.own_db).close()
        self.router = evaluator.Config(router_token="t")
        self.pub_cfg = publisher.PublisherConfig(
            own_db=self.own_db, tg_token="tg-token", requests_dir=str(self.requests),
            max_attempts=2,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self, sql, params=()):
        con = sqlite3.connect(self.own_db)
        con.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in con.execute(sql, params)]
        finally:
            con.close()

    def _run(self, adapters=None, dry_run=False, ignore_time=False, now=NOW):
        counters = {}
        reply = {"image_b64": [base64.b64encode(PNG).decode()], "model_id": "gpt-image-2"}
        patches = [
            mock.patch.object(publisher.PublisherConfig, "from_env", return_value=self.pub_cfg),
            mock.patch.object(evaluator, "chat", return_value={"text": "промпт", "model_id": "chat-m"}),
            mock.patch.object(evaluator, "call_tool", return_value=reply),
            mock.patch.dict(publisher.ADAPTERS, adapters or
                            {"telegram": mock.Mock(return_value="https://t.me/posinus/1")}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        code = daypic.run(self.cfg, self.router, dry_run=dry_run,
                          ignore_time=ignore_time, counters=counters, now=now)
        return code, counters

    def test_a_due_slot_generates_and_publishes(self):
        code, counters = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(counters["generated"], 1)
        self.assertEqual(counters["published"], 1)
        items = self._rows("SELECT * FROM daypic_item")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "published")
        self.assertEqual(items[0]["day"], DAY)
        self.assertEqual(items[0]["style"], "low-poly")
        self.assertTrue(Path(items[0]["file_path"]).exists())
        pubs = self._rows("SELECT * FROM daypic_publication")
        self.assertEqual([(p["platform"], p["status"]) for p in pubs], [("telegram", "ok")])

    def test_the_second_run_of_the_day_does_nothing(self):
        self._run()
        code, counters = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(counters["generated"], 0)
        self.assertEqual(len(self._rows("SELECT * FROM daypic_item")), 1)

    def test_before_generate_at_nothing_happens(self):
        early = datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc)  # 06:00 Moscow
        code, counters = self._run(now=early)
        self.assertEqual(counters["not_due"], 1)
        self.assertEqual(self._rows("SELECT * FROM daypic_item"), [])

    def test_a_dry_run_leaves_no_row_and_spends_no_image_call(self):
        with mock.patch.object(daypic, "generate_picture") as generate:
            code, _ = self._run(dry_run=True)
        generate.assert_not_called()
        self.assertEqual(self._rows("SELECT * FROM daypic_item"), [])

    def test_a_failed_generation_is_recorded_and_retried(self):
        with mock.patch.object(daypic, "generate_picture",
                               side_effect=daypic.DaypicError("отказ")):
            code, counters = self._run()
        self.assertEqual(code, 1)
        items = self._rows("SELECT * FROM daypic_item")
        self.assertEqual(items[0]["status"], "error")
        self.assertEqual(items[0]["attempts"], 1)
        # The next run succeeds and reuses the same row.
        code, counters = self._run()
        items = self._rows("SELECT * FROM daypic_item")
        self.assertEqual(items[0]["status"], "published")
        self.assertEqual(items[0]["attempts"], 2)

    def test_the_day_is_given_up_after_max_attempts(self):
        self.cfg.max_attempts = 2
        with mock.patch.object(daypic, "generate_picture",
                               side_effect=daypic.DaypicError("отказ")):
            self._run()
            self._run()
            code, counters = self._run()
        items = self._rows("SELECT * FROM daypic_item")
        self.assertEqual(items[0]["attempts"], 2)
        self.assertEqual(counters["failed"], 0)  # the third run did not even try

    def test_a_failing_platform_retries_and_finalizes_best_effort(self):
        boom = mock.Mock(side_effect=publisher.PublishError("vk down"))
        adapters = {"telegram": mock.Mock(return_value="url"), "vk": boom}
        self.pub_cfg.vk_token, self.pub_cfg.vk_group_id = "vk-token", "1"
        self._run(adapters=adapters)
        items = self._rows("SELECT * FROM daypic_item")
        self.assertEqual(items[0]["status"], "generated")  # vk still pending
        code, counters = self._run(adapters=adapters)      # second failure: max_attempts=2
        items = self._rows("SELECT * FROM daypic_item")
        self.assertEqual(items[0]["status"], "published")
        pubs = {p["platform"]: p for p in self._rows("SELECT * FROM daypic_publication")}
        self.assertEqual(pubs["telegram"]["status"], "ok")
        self.assertEqual(pubs["telegram"]["attempts"], 1)  # never re-sent
        self.assertEqual(pubs["vk"]["status"], "error")
        self.assertEqual(pubs["vk"]["attempts"], 2)

    def test_the_stop_cock_holds_the_whole_run(self):
        (self.requests / "pause").write_text("reason=проверка\n", encoding="utf-8")
        code, counters = self._run()
        self.assertTrue(counters.get("paused"))
        self.assertEqual(self._rows("SELECT * FROM daypic_item"), [])

    def test_no_platforms_saves_the_picture_and_keeps_it_generated(self):
        self.pub_cfg.tg_token = ""
        code, counters = self._run()
        items = self._rows("SELECT * FROM daypic_item")
        self.assertEqual(items[0]["status"], "generated")
        self.assertTrue(Path(items[0]["file_path"]).exists())


class DaypicRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.own_db = str(Path(self.tmp.name) / "own.sqlite3")
        self.pictures = Path(self.tmp.name) / "daypic"
        self.pictures.mkdir()
        daypic.open_own_db(self.own_db).close()
        self.cfg = retention.RetentionConfig(own_db=self.own_db, media_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _item(self, day: str) -> Path:
        path = self.pictures / f"{day}-day.png"
        path.write_bytes(b"x" * 1024)
        con = daypic.open_own_db(self.own_db)
        con.execute(
            "INSERT INTO daypic_item (day, slot, status, file_path) VALUES (?, 'day', 'published', ?)",
            (day, str(path)),
        )
        con.commit()
        con.close()
        return path

    def test_old_pictures_go_and_their_rows_stay(self):
        now = datetime(2026, 7, 29, tzinfo=timezone.utc)
        old = self._item((now - timedelta(days=120)).date().isoformat())
        fresh = self._item((now - timedelta(days=5)).date().isoformat())
        con = daypic.open_own_db(self.own_db)
        removed, _ = retention.purge_daypic(con, self.cfg, now, dry_run=False)
        con.close()
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        rows = {row[0]: row[1] for row in sqlite3.connect(self.own_db).execute(
            "SELECT day, file_purged_at FROM daypic_item")}
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[old.name[:10]])

    def test_a_database_without_daypic_tables_is_fine(self):
        bare = str(Path(self.tmp.name) / "bare.sqlite3")
        con = sqlite3.connect(bare)
        removed, freed = retention.purge_daypic(
            con, self.cfg, datetime.now(timezone.utc), dry_run=False)
        con.close()
        self.assertEqual((removed, freed), (0, 0))


if __name__ == "__main__":
    unittest.main()
