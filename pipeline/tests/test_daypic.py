"""Картина дня: the slot gate, the two-stage generation and the publish flow."""

from __future__ import annotations

import base64
import json
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

CHAT_REPLY = {
    "text": json.dumps(
        {"prompt": "готовый промпт", "description": "Сегодня день дружбы."},
        ensure_ascii=False,
    ),
    "model_id": "chat-m",
}

SLOT_COLUMNS = (
    "slot", "enabled", "title", "generate_at", "prompt", "system_prompt", "styles",
    "chat_provider", "chat_model", "image_provider", "image_model",
    "image_size", "image_size_wide",
)

CREATE_SLOT_TABLE = (
    "CREATE TABLE exchange_daypic_slot (slot TEXT PRIMARY KEY, enabled INTEGER, "
    "title TEXT, generate_at TEXT, prompt TEXT, system_prompt TEXT, styles TEXT, "
    "chat_provider TEXT, chat_model TEXT, image_provider TEXT, image_model TEXT, "
    "image_size TEXT, image_size_wide TEXT)"
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
    def test_a_used_style_is_not_picked_again(self):
        styles = ("a", "b", "c")
        for _ in range(20):
            self.assertEqual(daypic.pick_style(styles, {"a", "c"}), "b")

    def test_all_styles_spent_means_any_style(self):
        styles = ("a", "b")
        self.assertIn(daypic.pick_style(styles, {"a", "b"}), styles)

    def test_no_styles_means_no_style(self):
        self.assertEqual(daypic.pick_style((), set()), "")

    def test_the_slot_waits_for_its_time(self):
        slot = make_slot(generate_at="08:00")
        before = datetime(2026, 7, 29, 7, 59, tzinfo=MSK)
        after = datetime(2026, 7, 29, 8, 0, tzinfo=MSK)
        self.assertFalse(daypic.slot_due(slot, before))
        self.assertTrue(daypic.slot_due(slot, after))

    def test_an_unparsable_time_fails_towards_generating(self):
        slot = make_slot(generate_at="skoro")
        self.assertTrue(daypic.slot_due(slot, datetime(2026, 7, 29, 0, 1, tzinfo=MSK)))

    def test_used_styles_cover_only_this_slot_and_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = daypic.open_own_db(str(Path(tmp) / "own.sqlite3"))
            for day, slot, style in (
                ("2026-07-01", "day", "a"), ("2026-07-02", "day", "b"),
                ("2026-06-30", "day", "c"),        # last month
                ("2026-07-03", "evening", "d"),    # another slot
            ):
                con.execute(
                    "INSERT INTO daypic_item (day, slot, status, style) VALUES (?, ?, 'published', ?)",
                    (day, slot, style),
                )
            con.commit()
            self.assertEqual(daypic.used_styles(con, "day", DAY), {"a", "b"})
            con.close()


class PromptTests(unittest.TestCase):
    def test_the_request_carries_date_style_task_and_the_format(self):
        slot = make_slot()
        request = daypic.build_prompt_request(slot, NOW.astimezone(MSK), "surrealism")
        self.assertIn("2026-07-29", request)
        self.assertIn("среда", request)
        self.assertIn("surrealism", request)
        self.assertIn(slot.prompt, request)
        self.assertIn('"description"', request)
        self.assertIn("ориентацию", request.lower())

    def test_the_json_reply_becomes_prompt_and_description(self):
        cfg = evaluator.Config()
        with mock.patch.object(evaluator, "chat", return_value=CHAT_REPLY):
            prompt, description, model = daypic.build_prompt(
                cfg, make_slot(), NOW.astimezone(MSK), "low-poly")
        self.assertEqual(prompt, "готовый промпт")
        self.assertEqual(description, "Сегодня день дружбы.")
        self.assertEqual(model, "chat-m")

    def test_slot_model_hints_override_the_config(self):
        cfg = evaluator.Config(provider="deepseek", model_id="deepseek-v4-pro")
        seen = {}

        def fake_chat(chat_cfg, messages):
            seen["provider"], seen["model"] = chat_cfg.provider, chat_cfg.model_id
            return CHAT_REPLY

        with mock.patch.object(evaluator, "chat", side_effect=fake_chat):
            daypic.build_prompt(cfg, make_slot(chat_provider="codex-oauth", chat_model="gpt-6"),
                                NOW.astimezone(MSK), "")
        self.assertEqual(seen, {"provider": "codex-oauth", "model": "gpt-6"})

    def test_a_plain_text_reply_is_still_a_prompt(self):
        """The model wrote a prompt, just not the JSON envelope: use it."""
        cfg = evaluator.Config()
        with mock.patch.object(evaluator, "chat", return_value={"text": "просто промпт", "model_id": "m"}):
            prompt, description, _ = daypic.build_prompt(cfg, make_slot(), NOW.astimezone(MSK), "")
        self.assertEqual(prompt, "просто промпт")
        self.assertEqual(description, "")

    def test_two_failures_fall_back_to_the_template(self):
        cfg = evaluator.Config()
        with mock.patch.object(evaluator, "chat", side_effect=evaluator.McpError("down")):
            prompt, description, model = daypic.build_prompt(
                cfg, make_slot(), NOW.astimezone(MSK), "surrealism")
        self.assertEqual(model, "fallback-template")
        self.assertEqual(description, "")
        self.assertIn("2026-07-29", prompt)
        self.assertIn("surrealism", prompt)


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = daypic.DaypicConfig(daypic_dir=self.tmp.name, image_provider="codex-oauth")
        self.router = evaluator.Config()

    def tearDown(self):
        self.tmp.cleanup()

    def test_both_renditions_land_under_stable_names(self):
        reply = {"image_b64": [base64.b64encode(PNG).decode()], "model_id": "gpt-image-2"}
        with mock.patch.object(evaluator, "call_tool", return_value=reply) as call:
            vertical, wide, model = daypic.generate_pictures(
                self.cfg, self.router, make_slot(), "prompt", DAY)
        self.assertEqual(Path(vertical).name, f"{DAY}-day.png")
        self.assertEqual(Path(wide).name, f"{DAY}-day-wide.png")
        self.assertEqual(model, "gpt-image-2")
        sizes = [call_args.args[2]["params"]["size"] for call_args in call.call_args_list]
        self.assertEqual(sizes, ["1024x1536", "1536x1024"])

    def test_the_orientation_travels_in_the_prompt_too(self):
        """codex-oauth drops params.size, so the frame has to be said in words."""
        reply = {"image_b64": [base64.b64encode(PNG).decode()], "model_id": "m"}
        with mock.patch.object(evaluator, "call_tool", return_value=reply) as call:
            daypic.generate_pictures(self.cfg, self.router, make_slot(), "сцена", DAY)
        prompts = [args.args[2]["prompt"] for args in call.call_args_list]
        self.assertTrue(prompts[0].startswith(daypic.ORIENTATIONS["vertical"]))
        self.assertTrue(prompts[1].startswith(daypic.ORIENTATIONS["horizontal"]))
        self.assertIn("сцена", prompts[0])

    def test_a_frame_that_comes_back_the_wrong_way_is_logged(self):
        landscape = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 4 + b"IHDR"
                     + (1536).to_bytes(4, "big") + (1024).to_bytes(4, "big") + b"x" * 4000)
        reply = {"image_b64": [base64.b64encode(landscape).decode()], "model_id": "m"}
        with mock.patch.object(evaluator, "call_tool", return_value=reply):
            with self.assertLogs("posinus-daypic", level="WARNING") as logs:
                daypic.generate_picture(self.cfg, self.router, make_slot(), "prompt", DAY,
                                        "1024x1536", "vertical")
        self.assertIn("asked for a vertical frame, got 1536x1024", "\n".join(logs.output))

    def test_a_failed_horizontal_does_not_hold_the_day(self):
        reply = {"image_b64": [base64.b64encode(PNG).decode()], "model_id": "m"}
        calls = []

        def fake_call(url, tool, arguments, token=None):
            calls.append(arguments)
            if arguments["params"]["size"] == "1536x1024":
                raise evaluator.McpError("no wide today")
            return reply

        with mock.patch.object(evaluator, "call_tool", side_effect=fake_call):
            vertical, wide, _ = daypic.generate_pictures(
                self.cfg, self.router, make_slot(), "prompt", DAY)
        self.assertTrue(Path(vertical).exists())
        self.assertIsNone(wide)

    def test_a_refusal_retries_once_with_the_safe_suffix(self):
        reply = {"image_b64": [base64.b64encode(PNG).decode()], "model_id": "m"}
        calls = []

        def fake_call(url, tool, arguments, token=None):
            calls.append(arguments["prompt"])
            if len(calls) == 1:
                raise evaluator.McpError("safety refusal")
            return reply

        with mock.patch.object(evaluator, "call_tool", side_effect=fake_call):
            daypic.generate_picture(self.cfg, self.router, make_slot(), "prompt", DAY,
                                    "1024x1536", "vertical")
        self.assertEqual(len(calls), 2)
        self.assertIn(daypic.SAFE_SUFFIX, calls[1])

    def test_two_refusals_fail_the_day(self):
        with mock.patch.object(evaluator, "call_tool", side_effect=evaluator.McpError("no")):
            with self.assertRaises(daypic.DaypicError):
                daypic.generate_picture(self.cfg, self.router, make_slot(), "prompt", DAY,
                                    "1024x1536", "vertical")

    def test_an_implausibly_small_image_is_rejected(self):
        reply = {"image_b64": [base64.b64encode(b"tiny").decode()]}
        with mock.patch.object(evaluator, "call_tool", return_value=reply):
            with self.assertRaises(daypic.DaypicError):
                daypic.generate_picture(self.cfg, self.router, make_slot(), "prompt", DAY,
                                    "1024x1536", "vertical")


class SlotLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.news_db = str(Path(self.tmp.name) / "news.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_table(self, rows=()):
        con = sqlite3.connect(self.news_db)
        con.execute(CREATE_SLOT_TABLE)
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
             "", "", "", "", "", "1600x900"),
            ("evening", 0, "Картина вечера", "20:00", "з", "с", "", "", "", "", "", "", ""),
        ])
        slots = daypic.load_slots(self.news_db)
        self.assertEqual([slot.slot for slot in slots], ["day"])
        self.assertEqual(slots[0].styles, ("a", "b"))
        self.assertEqual(slots[0].image_size_wide, "1600x900")


class WildcarOrgTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.own_db = str(root / "own.sqlite3")
        self.requests = root / "requests"
        self.requests.mkdir()
        self.content = root / "content"
        self.cfg = daypic.DaypicConfig(own_db=self.own_db, wildcar_section="kartina")
        self.pub_cfg = publisher.PublisherConfig(
            wildcar_base="https://wildcar.org", wildcar_content_dir=str(self.content),
            requests_dir=str(self.requests), wildcar_wait_seconds=1,
        )
        self.picture = root / "2026-07-29-day-wide.png"
        self.picture.write_bytes(PNG)

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self, con):
        con.execute(
            "INSERT INTO daypic_item (day, slot, status, title, caption, file_path) "
            "VALUES (?, 'day', 'generated', 'Картина дня · 29 июля 2026', 'Сегодня праздник.', ?)",
            (DAY, str(self.picture)),
        )
        con.commit()
        return con.execute("SELECT * FROM daypic_item").fetchone()

    def test_the_issue_page_index_and_marker_are_written(self):
        con = daypic.open_own_db(self.own_db)
        row = self._row(con)
        item = publisher.PreparedNews(
            news_id=row["id"], title=row["title"], paragraphs=[row["caption"]],
            lead_image=str(self.picture), source_url="", source_name="",
            images=[(str(self.picture), "")],
        )
        with mock.patch.object(publisher, "http_send", return_value=(200, b"")):
            url = daypic.publish_wildcar_org(self.cfg, self.pub_cfg, con, row, item)
        con.close()

        self.assertEqual(url, "https://wildcar.org/kartina/2026-07-29/")
        page = (self.content / "kartina" / "2026-07-29" / "index.md").read_text(encoding="utf-8")
        self.assertIn("# Картина дня · 29 июля 2026", page)
        self.assertIn("![](2026-07-29-day-wide.png)", page)
        self.assertIn("Сегодня праздник.", page)
        self.assertTrue((self.content / "kartina" / "2026-07-29" / "2026-07-29-day-wide.png").exists())
        index = (self.content / "kartina" / "index.md").read_text(encoding="utf-8")
        self.assertIn("[Картина дня · 29 июля 2026](2026-07-29/index.md)", index)
        self.assertTrue((self.requests / publisher.WILDCAR_REBUILD_MARKER).exists())

    def test_a_page_that_never_comes_live_raises(self):
        con = daypic.open_own_db(self.own_db)
        row = self._row(con)
        item = publisher.PreparedNews(
            news_id=row["id"], title=row["title"], paragraphs=[],
            lead_image=str(self.picture), source_url="", source_name="",
            images=[(str(self.picture), "")],
        )
        with mock.patch.object(publisher, "http_send", return_value=(404, b"")):
            with self.assertRaises(publisher.PublishError):
                daypic.publish_wildcar_org(self.cfg, self.pub_cfg, con, row, item)
        con.close()


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
        con.execute(CREATE_SLOT_TABLE)
        con.execute(
            "INSERT INTO exchange_daypic_slot VALUES "
            "('day', 1, 'Картина дня', '08:00', 'задание', 'система', 'low-poly', "
            "'', '', '', '', '', '')"
        )
        con.commit()
        con.close()
        daypic.open_own_db(self.own_db).close()
        self.cfg = daypic.DaypicConfig(
            news_db=self.news_db, own_db=self.own_db,
            daypic_dir=str(root / "daypic"),
        )
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
            mock.patch.object(evaluator, "chat", return_value=dict(CHAT_REPLY)),
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

    def test_a_due_slot_generates_both_pictures_and_publishes(self):
        telegram = mock.Mock(return_value="https://t.me/posinus/1")
        code, counters = self._run(adapters={"telegram": telegram})
        self.assertEqual(code, 0)
        self.assertEqual(counters["generated"], 1)
        self.assertEqual(counters["published"], 1)
        items = self._rows("SELECT * FROM daypic_item")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "published")
        self.assertEqual(items[0]["day"], DAY)
        self.assertEqual(items[0]["style"], "low-poly")
        self.assertEqual(items[0]["title"], "Картина дня · 29 июля 2026")
        self.assertEqual(items[0]["caption"], "Сегодня день дружбы.")
        self.assertTrue(Path(items[0]["file_path"]).name.endswith("-day.png"))
        self.assertTrue(Path(items[0]["file_path_wide"]).name.endswith("-day-wide.png"))
        # Telegram got the vertical picture and the caption text.
        sent = telegram.call_args.args[1]
        self.assertEqual(sent.lead_image, items[0]["file_path"])
        self.assertEqual(sent.paragraphs, ["Сегодня день дружбы."])
        pubs = self._rows("SELECT * FROM daypic_publication")
        self.assertEqual([(p["platform"], p["status"]) for p in pubs], [("telegram", "ok")])

    def test_non_telegram_platforms_take_the_horizontal_picture(self):
        vk = mock.Mock(return_value="https://vk.ru/wall-1_2")
        self.pub_cfg.vk_token, self.pub_cfg.vk_group_id = "vk-token", "1"
        self._run(adapters={"telegram": mock.Mock(return_value="u"), "vk": vk})
        items = self._rows("SELECT * FROM daypic_item")
        self.assertEqual(vk.call_args.args[1].lead_image, items[0]["file_path_wide"])

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

    def test_the_run_request_lifts_the_time_gate(self):
        """«Прогнать сейчас» means now, not at the slot's hour."""
        (self.requests / "run-daypic").touch()
        early = datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc)  # 06:00 Moscow
        code, counters = self._run(now=early)
        self.assertEqual(counters["generated"], 1)
        self.assertFalse((self.requests / "run-daypic").exists())

    def test_a_pause_still_consumes_the_run_request(self):
        """Otherwise the .path unit would loop the service until the pause lifts."""
        (self.requests / "run-daypic").touch()
        (self.requests / "pause").write_text("reason=проверка\n", encoding="utf-8")
        code, counters = self._run()
        self.assertTrue(counters.get("paused"))
        self.assertFalse((self.requests / "run-daypic").exists())
        self.assertEqual(self._rows("SELECT * FROM daypic_item"), [])

    def test_a_dry_run_leaves_no_row_and_spends_no_image_call(self):
        with mock.patch.object(daypic, "generate_pictures") as generate:
            code, _ = self._run(dry_run=True)
        generate.assert_not_called()
        self.assertEqual(self._rows("SELECT * FROM daypic_item"), [])

    def test_a_failed_generation_is_recorded_and_retried(self):
        with mock.patch.object(daypic, "generate_pictures",
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
        with mock.patch.object(daypic, "generate_pictures",
                               side_effect=daypic.DaypicError("отказ")):
            self._run()
            self._run()
            code, counters = self._run()
        items = self._rows("SELECT * FROM daypic_item")
        self.assertEqual(items[0]["attempts"], 2)
        self.assertEqual(counters["failed"], 0)  # the third run did not even try

    def test_a_new_day_avoids_the_styles_this_month_spent(self):
        con = daypic.open_own_db(self.own_db)
        con.execute(
            "INSERT INTO daypic_item (day, slot, status, style) VALUES ('2026-07-28', 'day', 'published', 'low-poly')")
        con.commit()
        con.close()
        news = sqlite3.connect(self.news_db)
        news.execute("UPDATE exchange_daypic_slot SET styles = 'low-poly\nvaporwave'")
        news.commit()
        news.close()
        self._run()
        items = self._rows("SELECT * FROM daypic_item WHERE day = ?", (DAY,))
        self.assertEqual(items[0]["style"], "vaporwave")

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


class MigrateTests(unittest.TestCase):
    def test_a_first_deploy_schema_gains_the_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "own.sqlite3")
            con = sqlite3.connect(path)
            con.execute(
                "CREATE TABLE daypic_item (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, "
                "slot TEXT, status TEXT, title TEXT, style TEXT, prompt TEXT, file_path TEXT, "
                "prompt_model_id TEXT, image_model_id TEXT, attempts INTEGER DEFAULT 0, "
                "error TEXT, generated_at TEXT, published_at TEXT, file_purged_at TEXT, "
                "UNIQUE (day, slot))"
            )
            con.execute("INSERT INTO daypic_item (day, slot, status) VALUES ('2026-07-28', 'day', 'published')")
            con.commit()
            con.close()

            con = daypic.open_own_db(path)
            columns = {row["name"] for row in con.execute("PRAGMA table_info(daypic_item)")}
            con.close()
        self.assertIn("file_path_wide", columns)
        self.assertIn("caption", columns)


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

    def _item(self, day: str) -> tuple[Path, Path]:
        vertical = self.pictures / f"{day}-day.png"
        wide = self.pictures / f"{day}-day-wide.png"
        vertical.write_bytes(b"x" * 1024)
        wide.write_bytes(b"x" * 1024)
        con = daypic.open_own_db(self.own_db)
        con.execute(
            "INSERT INTO daypic_item (day, slot, status, file_path, file_path_wide) "
            "VALUES (?, 'day', 'published', ?, ?)",
            (day, str(vertical), str(wide)),
        )
        con.commit()
        con.close()
        return vertical, wide

    def test_old_pictures_go_both_renditions_and_their_rows_stay(self):
        now = datetime(2026, 7, 29, tzinfo=timezone.utc)
        old_vertical, old_wide = self._item((now - timedelta(days=120)).date().isoformat())
        fresh_vertical, _ = self._item((now - timedelta(days=5)).date().isoformat())
        con = daypic.open_own_db(self.own_db)
        removed, _ = retention.purge_daypic(con, self.cfg, now, dry_run=False)
        con.close()
        self.assertEqual(removed, 2)
        self.assertFalse(old_vertical.exists())
        self.assertFalse(old_wide.exists())
        self.assertTrue(fresh_vertical.exists())
        rows = {row[0]: row[1] for row in sqlite3.connect(self.own_db).execute(
            "SELECT day, file_purged_at FROM daypic_item")}
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[old_vertical.name[:10]])

    def test_a_database_without_daypic_tables_is_fine(self):
        bare = str(Path(self.tmp.name) / "bare.sqlite3")
        con = sqlite3.connect(bare)
        removed, freed = retention.purge_daypic(
            con, self.cfg, datetime.now(timezone.utc), dry_run=False)
        con.close()
        self.assertEqual((removed, freed), (0, 0))


if __name__ == "__main__":
    unittest.main()
