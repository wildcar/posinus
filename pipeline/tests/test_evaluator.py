"""Unit tests for evaluator.py: JSON extraction, validation, DB write."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
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

# The crawler side of the threshold tables, as migration 0008 creates them.
PROFILE_SCHEMA_SQL = """
CREATE TABLE exchange_selection_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE exchange_selection_bound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES exchange_selection_profile (id),
    characteristic_key TEXT NOT NULL REFERENCES exchange_evaluation_characteristics (key),
    kind TEXT NOT NULL,
    value INTEGER NOT NULL CHECK (value BETWEEN 0 AND 10),
    UNIQUE (profile_id, characteristic_key, kind)
);
CREATE VIEW exchange_active_selection_profile AS
SELECT p.name AS profile_name, p.revision AS profile_revision,
       b.characteristic_key, b.kind, b.value
FROM exchange_selection_profile p
JOIN exchange_selection_bound b ON b.profile_id = p.id
WHERE p.is_active = 1;
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


class LoadProfileTests(unittest.TestCase):
    """One rule for two readers: the thresholds come from the crawler DB."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA_SQL)
        self.con.executemany(
            "INSERT INTO exchange_evaluation_characteristics (key) VALUES (?)",
            [(key,) for key in AXIS_KEYS],
        )

    def tearDown(self):
        self.con.close()

    def _install_profile(self, bounds, name="default", revision=3, is_active=1):
        self.con.executescript(PROFILE_SCHEMA_SQL)
        cur = self.con.execute(
            "INSERT INTO exchange_selection_profile (name, is_active, revision) VALUES (?, ?, ?)",
            (name, is_active, revision),
        )
        self.con.executemany(
            "INSERT INTO exchange_selection_bound (profile_id, characteristic_key, kind, value) "
            "VALUES (?, ?, ?, ?)",
            [(cur.lastrowid, key, kind, value) for key, kind, value in bounds],
        )
        self.con.commit()

    def test_thresholds_come_from_the_view(self):
        self._install_profile([
            ("positivity", "gate_min", 7),
            ("clickbait", "gate_max", 3),
            ("cuteness", "highlight_min", 9),
        ])

        profile = evaluator.load_profile(self.con)

        self.assertEqual(profile.name, "default")
        self.assertEqual(profile.revision, 3)
        self.assertEqual(profile.tag, "default.r3")
        self.assertEqual(profile.gates_min, {"positivity": 7})
        self.assertEqual(profile.gates_max, {"clickbait": 3})
        self.assertEqual(profile.highlight_min, {"cuteness": 9})
        self.assertTrue(profile.selects({"positivity": 7, "clickbait": 3, "cuteness": 9}))
        self.assertFalse(profile.selects({"positivity": 6, "cuteness": 10}))

    def test_missing_view_falls_back_to_the_builtin(self):
        """Rolling the code back past the migration must not break the evaluator."""
        profile = evaluator.load_profile(self.con)

        self.assertIs(profile, evaluator.DEFAULT_PROFILE)
        self.assertEqual(profile.tag, "default.builtin")

    def test_no_active_profile_falls_back_instead_of_selecting_everything(self):
        self._install_profile([("positivity", "gate_min", 7)], is_active=0)

        self.assertIs(evaluator.load_profile(self.con), evaluator.DEFAULT_PROFILE)

    def test_unknown_bound_kind_is_ignored(self):
        self._install_profile([
            ("positivity", "gate_min", 8),
            ("negativity", "gate_avg", 5),
        ])

        profile = evaluator.load_profile(self.con)

        self.assertEqual(profile.gates_min, {"positivity": 8})
        self.assertEqual(profile.gates_max, {})

    def test_profile_revision_travels_into_selector_version(self):
        cfg = Config(selector_name="test-evaluator")
        event_id = write_review(
            self.con, cfg, 5, full_scores(), "", "actual-model", "positive", "default.r3"
        )

        version = self.con.execute(
            "SELECT selector_version FROM exchange_review_events WHERE id = ?", (event_id,)
        ).fetchone()["selector_version"]
        self.assertEqual(version, f"{evaluator.EVALUATOR_VERSION}+actual-model+default.r3")


class RescoreTests(unittest.TestCase):
    """--backfill --rescore-all: re-apply the rule, write only what changed."""

    VIEWS_SQL = """
    CREATE VIEW exchange_latest_reviews AS
    SELECT * FROM exchange_review_events e
    WHERE e.id = (
        SELECT id FROM exchange_review_events x
        WHERE x.news_id = e.news_id AND x.selector_name = e.selector_name
        ORDER BY x.created_at DESC, x.id DESC LIMIT 1
    );
    CREATE VIEW exchange_latest_evaluation_scores AS
    SELECT r.news_id, r.selector_name, r.id AS review_event_id, r.created_at,
           s.characteristic_key, s.value
    FROM exchange_latest_reviews r
    JOIN exchange_evaluation_scores s ON s.review_event_id = r.id;
    """

    def setUp(self):
        self.path = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False).name
        self.con = evaluator.open_db(self.path)
        self.con.executescript(SCHEMA_SQL)
        self.con.executescript(self.VIEWS_SQL)
        self.con.executemany(
            "INSERT INTO exchange_evaluation_characteristics (key) VALUES (?)",
            [(key,) for key in AXIS_KEYS],
        )
        self.cfg = Config(db_path=self.path, selector_name="news-evaluator")

    def tearDown(self):
        self.con.close()

    def _score(self, news_id, decision, scores):
        write_review(self.con, self.cfg, news_id, scores, "", "m", decision, "default.r1")

    def test_only_changed_verdicts_get_a_correcting_event(self):
        passing = full_scores(0)
        passing.update({"positivity": 9, "uniqueness": 9})
        rejected = full_scores(0)
        # already labelled correctly under the current rule
        self._score(1, "positive", passing)
        self._score(2, "not_positive", rejected)
        # labelled positive, but the rule says otherwise now
        self._score(3, "positive", rejected)
        before = self.con.execute("SELECT COUNT(*) FROM exchange_review_events").fetchone()[0]

        rc = evaluator.run_backfill(self.cfg, evaluator.DEFAULT_PROFILE, dry_run=False, rescore_all=True)

        after = self.con.execute("SELECT COUNT(*) FROM exchange_review_events").fetchone()[0]
        self.assertEqual(rc, 0)
        self.assertEqual(after - before, 1)  # only news 3 was corrected
        latest = dict(self.con.execute(
            "SELECT news_id, decision FROM exchange_latest_reviews"
        ).fetchall())
        self.assertEqual(latest, {1: "positive", 2: "not_positive", 3: "not_positive"})

    def test_plain_backfill_only_touches_skipped(self):
        self._score(1, "skipped", full_scores(0))
        self._score(2, "positive", full_scores(0))

        evaluator.run_backfill(self.cfg, evaluator.DEFAULT_PROFILE, dry_run=False)

        latest = dict(self.con.execute(
            "SELECT news_id, decision FROM exchange_latest_reviews"
        ).fetchall())
        self.assertEqual(latest, {1: "not_positive", 2: "positive"})


if __name__ == "__main__":
    unittest.main()
