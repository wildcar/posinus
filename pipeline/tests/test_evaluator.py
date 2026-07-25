"""Unit tests for evaluator.py: JSON extraction, validation, DB write."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evaluator
from evaluator import (
    Config,
    DEFAULT_PROFILE,
    EvaluationInvalid,
    SelectionProfile,
    _coerce_score,
    build_chat_arguments,
    extract_json_object,
    validate_evaluation,
    write_review,
)

AXIS_KEYS = [
    "positivity", "negativity",
    "heartwarming", "cuteness", "humor", "pride_humanity", "pride_russia",
    "heroism", "inspiration", "beauty",
    "interestingness", "surprise", "uniqueness", "memorability",
    "importance", "impact_scale", "usefulness",
    "clickbait", "controversy", "promo",
]


def full_scores(value: int = 5) -> dict[str, int]:
    return {key: value for key in AXIS_KEYS}


class ExtractJsonTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json_object('{"a": 1}'), {"a": 1})

    def test_markdown_fence(self):
        text = 'Вот оценка:\n```json\n{"a": 1}\n```\nГотово.'
        self.assertEqual(extract_json_object(text), {"a": 1})

    def test_prose_around_object(self):
        text = 'Конечно! {"a": {"b": 2}} Надеюсь, это поможет.'
        self.assertEqual(extract_json_object(text), {"a": {"b": 2}})

    def test_braces_inside_strings(self):
        text = '{"comment": "скобки } в строке", "a": 1}'
        self.assertEqual(extract_json_object(text)["a"], 1)

    def test_trailing_comma(self):
        self.assertEqual(extract_json_object('{"a": 1,}'), {"a": 1})

    def test_no_json(self):
        with self.assertRaises(EvaluationInvalid):
            extract_json_object("Не могу оценить эту новость.")

    def test_top_level_array_rejected(self):
        with self.assertRaises(EvaluationInvalid):
            extract_json_object("[1, 2, 3]")


class CoerceScoreTests(unittest.TestCase):
    def test_int(self):
        self.assertEqual(_coerce_score(7), 7)

    def test_integral_float(self):
        self.assertEqual(_coerce_score(7.0), 7)

    def test_numeric_string(self):
        self.assertEqual(_coerce_score(" 7 "), 7)
        self.assertEqual(_coerce_score("7.0"), 7)

    def test_rejects_bool(self):
        with self.assertRaises(ValueError):
            _coerce_score(True)

    def test_rejects_fraction(self):
        with self.assertRaises(ValueError):
            _coerce_score(6.5)

    def test_rejects_out_of_range(self):
        for bad in (-1, 11, "12"):
            with self.assertRaises(ValueError):
                _coerce_score(bad)

    def test_rejects_garbage(self):
        for bad in ("high", None, [7]):
            with self.assertRaises(ValueError):
                _coerce_score(bad)


class ValidateEvaluationTests(unittest.TestCase):
    def test_happy_path(self):
        payload = {"news_id": 5, "scores": full_scores(), "comment": "норм"}
        scores, comment, warnings = validate_evaluation(payload, 5, AXIS_KEYS)
        self.assertEqual(scores, full_scores())
        self.assertEqual(comment, "норм")
        self.assertEqual(warnings, [])

    def test_flat_payload_accepted_with_warning(self):
        payload = {**full_scores(), "news_id": 5, "comment": "ок"}
        scores, _, warnings = validate_evaluation(payload, 5, AXIS_KEYS)
        self.assertEqual(scores, full_scores())
        self.assertTrue(warnings)

    def test_news_id_mismatch(self):
        payload = {"news_id": 6, "scores": full_scores()}
        with self.assertRaises(EvaluationInvalid):
            validate_evaluation(payload, 5, AXIS_KEYS)

    def test_news_id_optional(self):
        scores, _, _ = validate_evaluation({"scores": full_scores()}, 5, AXIS_KEYS)
        self.assertEqual(len(scores), 20)

    def test_missing_axis(self):
        scores = full_scores()
        del scores["beauty"]
        with self.assertRaises(EvaluationInvalid) as ctx:
            validate_evaluation({"scores": scores}, 5, AXIS_KEYS)
        self.assertIn("beauty", str(ctx.exception))

    def test_all_problems_reported_at_once(self):
        scores = full_scores()
        scores["humor"] = "funny"
        scores["promo"] = 15
        with self.assertRaises(EvaluationInvalid) as ctx:
            validate_evaluation({"scores": scores}, 5, AXIS_KEYS)
        message = str(ctx.exception)
        self.assertIn("humor", message)
        self.assertIn("promo", message)

    def test_extra_keys_ignored_with_warning(self):
        scores = full_scores()
        scores["vibes"] = 9
        result, _, warnings = validate_evaluation({"scores": scores}, 5, AXIS_KEYS)
        self.assertNotIn("vibes", result)
        self.assertTrue(any("vibes" in w for w in warnings))

    def test_string_scores_coerced(self):
        scores = {key: "7" for key in AXIS_KEYS}
        result, _, _ = validate_evaluation({"scores": scores}, 5, AXIS_KEYS)
        self.assertEqual(result, full_scores(7))

    def test_comment_normalized_and_capped(self):
        payload = {"scores": full_scores(), "comment": "  много \n пробелов  " + "х" * 600}
        _, comment, _ = validate_evaluation(payload, 5, AXIS_KEYS)
        self.assertLessEqual(len(comment), evaluator.MAX_COMMENT_CHARS)
        self.assertNotIn("\n", comment)

    def test_non_string_comment_tolerated(self):
        payload = {"scores": full_scores(), "comment": 42}
        _, comment, warnings = validate_evaluation(payload, 5, AXIS_KEYS)
        self.assertEqual(comment, "")
        self.assertTrue(warnings)

    def test_scores_not_a_dict(self):
        with self.assertRaises(EvaluationInvalid):
            validate_evaluation({"scores": [1, 2]}, 5, AXIS_KEYS)


SCHEMA_SQL = """
CREATE TABLE exchange_review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    score REAL,
    reason TEXT NOT NULL,
    selector_name TEXT NOT NULL,
    selector_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (selector_name, idempotency_key)
);
CREATE TABLE exchange_evaluation_characteristics (
    key TEXT PRIMARY KEY
);
CREATE TABLE exchange_evaluation_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_event_id INTEGER NOT NULL REFERENCES exchange_review_events (id),
    characteristic_key TEXT NOT NULL REFERENCES exchange_evaluation_characteristics (key),
    value INTEGER NOT NULL CHECK (value BETWEEN 0 AND 10),
    UNIQUE (review_event_id, characteristic_key)
);
"""


class WriteReviewTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self.con.executescript(SCHEMA_SQL)
        self.con.executemany(
            "INSERT INTO exchange_evaluation_characteristics (key) VALUES (?)",
            [(key,) for key in AXIS_KEYS],
        )
        self.cfg = Config(selector_name="test-evaluator", model_id="test-model")

    def tearDown(self):
        self.con.close()

    def test_event_and_scores_written(self):
        event_id = write_review(
            self.con, self.cfg, 5, full_scores(), "комментарий", "actual-model", "positive"
        )
        event = self.con.execute(
            "SELECT * FROM exchange_review_events WHERE id = ?", (event_id,)
        ).fetchone()
        self.assertEqual(event["decision"], "positive")
        self.assertIsNone(event["score"])
        self.assertEqual(event["reason"], "комментарий")
        # the model that answered is recorded, not the configured one
        self.assertEqual(
            event["selector_version"], f"{evaluator.EVALUATOR_VERSION}+actual-model"
        )
        rows = self.con.execute(
            "SELECT COUNT(*) FROM exchange_evaluation_scores WHERE review_event_id = ?",
            (event_id,),
        ).fetchone()[0]
        self.assertEqual(rows, 20)

    def test_unknown_model_still_recorded(self):
        event_id = write_review(self.con, self.cfg, 5, full_scores(), "", "", "not_positive")
        event = self.con.execute(
            "SELECT selector_version, decision FROM exchange_review_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        self.assertEqual(
            event["selector_version"], f"{evaluator.EVALUATOR_VERSION}+router-choice"
        )
        self.assertEqual(event["decision"], "not_positive")

    def test_foreign_key_enforced(self):
        scores = full_scores()
        scores["unknown_axis"] = 5
        del scores["promo"]
        with self.assertRaises(sqlite3.IntegrityError):
            write_review(self.con, self.cfg, 5, scores, "", "actual-model", "positive")
        events = self.con.execute("SELECT COUNT(*) FROM exchange_review_events").fetchone()[0]
        self.assertEqual(events, 0)  # transaction rolled back entirely


class SelectionProfileTests(unittest.TestCase):
    """The owner's default rule: positivity>=8, heroism/clickbait/promo<=4,
    and at least one bright axis >=9."""

    def _base(self) -> dict[str, int]:
        # passes every hard gate; no bright axis yet -> not selected on its own
        scores = full_scores(0)
        scores["positivity"] = 8
        return scores

    def test_bright_axis_selects(self):
        for axis in ("pride_humanity", "pride_russia", "inspiration", "beauty",
                     "interestingness", "surprise", "uniqueness"):
            scores = self._base()
            scores[axis] = 9
            self.assertTrue(DEFAULT_PROFILE.selects(scores), axis)
            self.assertEqual(DEFAULT_PROFILE.decide(scores), "positive", axis)

    def test_no_bright_axis_rejected(self):
        scores = self._base()  # gates fine, but nothing reaches 9
        self.assertFalse(DEFAULT_PROFILE.selects(scores))
        self.assertEqual(DEFAULT_PROFILE.decide(scores), "not_positive")

    def test_low_positivity_rejected(self):
        scores = self._base()
        scores["positivity"] = 7  # below the >7 gate
        scores["beauty"] = 10
        self.assertFalse(DEFAULT_PROFILE.selects(scores))

    def test_upper_gates_block_selection(self):
        for axis in ("heroism", "clickbait", "promo"):
            scores = self._base()
            scores["beauty"] = 10
            scores[axis] = 5  # one over the <=4 bound
            self.assertFalse(DEFAULT_PROFILE.selects(scores), axis)

    def test_boundary_values(self):
        scores = self._base()
        scores["beauty"] = 9
        scores["heroism"] = 4
        scores["clickbait"] = 4
        scores["promo"] = 4
        self.assertTrue(DEFAULT_PROFILE.selects(scores))  # all bounds inclusive

    def test_missing_axis_reads_as_zero(self):
        profile = SelectionProfile(
            name="t", gates_min={"positivity": 8}, gates_max={}, highlight_min={}
        )
        self.assertFalse(profile.selects({}))
        self.assertTrue(profile.selects({"positivity": 8}))


class ChatArgumentsTests(unittest.TestCase):
    MESSAGES = [{"role": "user", "content": "hi"}]

    def test_all_hints_passed(self):
        cfg = Config(model_id="m1", provider="p1", tier="cheap")
        args = build_chat_arguments(cfg, self.MESSAGES)
        self.assertEqual(args["model_id"], "m1")
        self.assertEqual(args["provider"], "p1")
        self.assertEqual(args["tier"], "cheap")

    def test_empty_hints_omitted_router_decides(self):
        cfg = Config(model_id="", provider="", tier="")
        args = build_chat_arguments(cfg, self.MESSAGES)
        for hint in ("model_id", "provider", "tier"):
            self.assertNotIn(hint, args)
        self.assertEqual(args["messages"], self.MESSAGES)


if __name__ == "__main__":
    unittest.main()
