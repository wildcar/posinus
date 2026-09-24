#!/usr/bin/env python3
"""News evaluator v0: scores crawler news on the fixed 20-axis set.

Single-file, stdlib-only. Reads unevaluated news from the crawler's SQLite
exchange contract, asks a chat model through model-router-mcp (Streamable HTTP
MCP), validates the reply, applies the selection profile, and appends a review
event (positive/not_positive) plus per-axis scores in one transaction.

--backfill re-verdicts news that were scored before the profile existed (their
latest event is 'skipped'): it recomputes the verdict from the stored scores and
writes a correcting event, without calling the model.

Contract: docs/contracts/database-contract.md (repository root)
Behavior: AGENTS/SPEC.md, sections "Сервис v0" and "Пороговая модель".
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import runlog

log = logging.getLogger("posinus-evaluator")

EVALUATOR_VERSION = "0.2.0"
AXIS_COUNT = 20
MAX_MODEL_ATTEMPTS = 3
MAX_BODY_CHARS = 8000
MAX_COMMENT_CHARS = 500
OPENROUTER = "openrouter"
FINAL_CHECK_MODES = ("chat", "decide", "shadow")
DB_LOCK_RETRIES = 4
MCP_PROTOCOL_VERSION = "2025-03-26"

QUEUE_SQL = """
SELECT n.news_id, n.title, n.body_text, n.language, n.published_at
FROM exchange_news_for_selection AS n
WHERE NOT EXISTS (
    SELECT 1
    FROM exchange_latest_reviews AS r
    WHERE r.news_id = n.news_id
      AND r.selector_name = :selector_name
)
ORDER BY n.first_seen_at
LIMIT :batch_size
"""

AXES_SQL = """
SELECT key, category, title, description, anchor_low, anchor_high
FROM exchange_evaluation_characteristics
ORDER BY position
"""

INSERT_EVENT_SQL = """
INSERT INTO exchange_review_events (
    news_id, decision, score, reason,
    selector_name, selector_version,
    idempotency_key, created_at
) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
RETURNING id
"""

INSERT_SCORE_SQL = """
INSERT INTO exchange_evaluation_scores (review_event_id, characteristic_key, value)
VALUES (?, ?, ?)
"""

# The closed list of rubrics, minus the placeholder: `unknown` is what we write
# ourselves when the answer is unusable, and offering it to the model would make
# it the easy way out of every hard call.
TOPICS_SQL = """
SELECT key, title, description
FROM exchange_topic
WHERE assignable = 1
ORDER BY position
"""

# One row per news item, so a re-evaluation corrects the rubric in place. Unlike
# a verdict there is nothing to keep a history of: the rubric describes the story,
# not a decision about it.
INSERT_TOPIC_SQL = """
INSERT INTO exchange_news_topic (news_id, topic_key, selector_name, selector_version, created_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(news_id) DO UPDATE SET
    topic_key = excluded.topic_key,
    selector_name = excluded.selector_name,
    selector_version = excluded.selector_version,
    created_at = excluded.created_at
"""

PLACEHOLDER_TOPIC = "unknown"

# News whose latest event by this selector is still 'skipped' (no verdict yet),
# together with the scores attached to that event. Feeds the backfill pass.
BACKFILL_SQL = """
SELECT s.news_id, r.decision, r.selector_version, s.characteristic_key, s.value
FROM exchange_latest_evaluation_scores AS s
JOIN exchange_latest_reviews AS r
  ON r.news_id = s.news_id AND r.selector_name = s.selector_name
WHERE s.selector_name = :selector_name
  AND r.decision = 'skipped'
ORDER BY s.news_id
"""

# Every news item this selector has scored, whatever verdict it carries now.
# Feeds --rescore-all, which re-applies the current thresholds to the whole
# corpus and only writes where the verdict actually changed.
RESCORE_SQL = """
SELECT s.news_id, r.decision, r.selector_version, s.characteristic_key, s.value
FROM exchange_latest_evaluation_scores AS s
JOIN exchange_latest_reviews AS r
  ON r.news_id = s.news_id AND r.selector_name = s.selector_name
WHERE s.selector_name = :selector_name
ORDER BY s.news_id
"""


# --------------------------------------------------------------- MCP client


class McpError(RuntimeError):
    pass


class EvaluationInvalid(ValueError):
    """The model reply failed JSON extraction or schema validation."""


def _post(url: str, token: str | None, payload: dict[str, Any], timeout: float) -> tuple[str, str]:
    body = json.dumps(payload).encode("utf-8")
    # urllib refuses to re-POST on redirects; follow 307/308 manually
    # (FastMCP mounted at /mcp redirects to /mcp/).
    for _ in range(3):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("MCP-Protocol-Version", MCP_PROTOCOL_VERSION)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8"), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            if exc.code in (307, 308) and exc.headers.get("Location"):
                url = urllib.parse.urljoin(url, exc.headers["Location"])
                continue
            raise
    raise McpError("too many redirects")


def _extract_rpc_response(raw: str, content_type: str, request_id: int) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if content_type.startswith("text/event-stream"):
        for line in raw.splitlines():
            if line.startswith("data:"):
                data = line[len("data:"):].strip()
                if data:
                    messages.append(json.loads(data))
    elif raw.strip():
        messages.append(json.loads(raw))
    for message in messages:
        if message.get("id") == request_id and ("result" in message or "error" in message):
            return message
    raise McpError(f"no JSON-RPC response with id={request_id} in server reply")


def call_tool(
    url: str,
    tool: str,
    arguments: dict[str, Any],
    token: str | None = None,
    timeout: float = 300.0,
) -> Any:
    """Call an MCP tool on a stateless Streamable HTTP server."""
    request_id = 1
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    raw, content_type = _post(url, token, payload, timeout)
    message = _extract_rpc_response(raw, content_type, request_id)
    if "error" in message:
        raise McpError(f"tool {tool}: {message['error'].get('message', message['error'])}")
    result = message["result"]
    if result.get("isError"):
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        raise McpError(f"tool {tool} failed: {' '.join(texts) or result}")
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
        return structured["result"]  # FastMCP wraps non-object returns
    if structured is not None:
        return structured
    texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    return "\n".join(t for t in texts if t)


@dataclass
class Config:
    """Everything comes from the environment; the model is NOT hard-coded.

    An empty model_id delegates model choice to the router (provider/tier
    hints); the model that actually answered is read from the reply and
    recorded in selector_version per event.
    """

    db_path: str = "/var/lib/posinus/posinus.sqlite3"
    # The trailing slash matters: FastMCP mounts at /mcp/ and answers /mcp with
    # a 307 WITHOUT reading the request body first — a megabyte-scale POST (an
    # image in `images_b64`) overflows the socket buffer and dies with an
    # instant ECONNRESET instead of the redirect (root-caused 2026-07-30).
    router_url: str = "http://127.0.0.1:8088/mcp/"
    router_token: str = ""
    provider: str = "deepseek"
    model_id: str = "deepseek-v4-pro"
    tier: str = ""
    selector_name: str = "news-evaluator"
    # Who the router bills and rate-limits. Kept separate from selector_name on
    # purpose: selector_name is the frozen contract string in exchange_review_events
    # (~6200 rows), while every process calling the router wants its own id so the
    # router's usage reports tell scoring and retelling apart. Empty falls back to
    # selector_name, which is what the evaluator has always sent.
    router_user: str = ""
    # The application identity sent with every router call (app_url/app_name).
    # One identity for the whole of posinus — external_user_id already tells the
    # processes apart. The router forwards it to providers that take one
    # (OpenRouter's HTTP-Referer/X-Title), and the URL outranks the name there.
    # An empty value drops the field from the request.
    app_url: str = "https://wildcar.org"
    app_name: str = "Positive news"
    # max_tokens has to cover the model's reasoning tokens, not just the JSON answer.
    # deepseek-v4-pro spends ~950 completion tokens on one full 20-axis evaluation, and
    # when it hits the cap before writing content the provider returns an empty body —
    # which surfaces here as "DeepSeek returned an empty response". The old 1000-token
    # budget was fine for deepseek-chat and failed on most news with v4-pro.
    params: dict[str, Any] = field(default_factory=lambda: {"temperature": 0.3, "max_tokens": 4000})
    # Selected news gets a second look from the model as a publishing editor
    # before the verdict is written; "inappropriate" overrides the thresholds.
    final_check: bool = True
    # Who gives that second look. `chat`: the scoring model answers a JSON
    # yes/no (the original path). `decide`: a decision model (TypeSafe Jev via
    # the router's `decide` tool) answers five noul questions with
    # probabilities, and the thresholds below turn them into a verdict.
    # `shadow`: `chat` decides, `decide` runs beside it, and both verdicts land
    # in `final_check_shadow` in the pipeline's own DB — the calibration week.
    final_check_mode: str = "chat"
    decide_provider: str = OPENROUTER
    decide_model: str = "~typesafe/jev-latest"
    # P(appropriate) at or above this reads as appropriate, and any red flag at
    # or above the flag threshold vetoes on its own. All are noul probabilities.
    # Advertising has its own, higher bar: in the 2026-09-24 replay it never
    # fired on a real veto yet reached 0.56 on an approved story.
    decide_threshold: float = 0.5
    decide_flag_threshold: float = 0.5
    decide_advertising_threshold: float = 0.6
    # The pipeline-owned DB (shared with the preparer), home of the shadow table.
    own_db_path: str = runlog.DEFAULT_DB

    @classmethod
    def from_env(cls, env: dict[str, str] = os.environ) -> "Config":
        cfg = cls()
        cfg.db_path = env.get("NEWS_DB_PATH", cfg.db_path)
        cfg.router_url = env.get("ROUTER_MCP_URL", cfg.router_url)
        cfg.router_token = env.get("ROUTER_AUTH_TOKEN", cfg.router_token)
        cfg.provider = env.get("EVALUATOR_PROVIDER", cfg.provider)
        cfg.model_id = env.get("EVALUATOR_MODEL", cfg.model_id)
        cfg.tier = env.get("EVALUATOR_TIER", cfg.tier)
        cfg.selector_name = env.get("SELECTOR_NAME", cfg.selector_name)
        cfg.router_user = env.get("ROUTER_USER_ID", cfg.router_user)
        cfg.app_url = env.get("ROUTER_APP_URL", cfg.app_url)
        cfg.app_name = env.get("ROUTER_APP_NAME", cfg.app_name)
        if value := env.get("EVALUATOR_MAX_TOKENS"):
            cfg.params["max_tokens"] = int(value)
        if value := env.get("EVALUATOR_TEMPERATURE"):
            cfg.params["temperature"] = float(value)
        cfg.final_check = env.get("EVALUATOR_FINAL_CHECK", "").strip().lower() not in (
            "off", "0", "no", "false"
        )
        mode = env.get("EVALUATOR_FINAL_CHECK_MODE", cfg.final_check_mode).strip().lower()
        if mode not in FINAL_CHECK_MODES:
            log.warning("EVALUATOR_FINAL_CHECK_MODE=%r is unknown, using 'chat'", mode)
            mode = "chat"
        cfg.final_check_mode = mode
        cfg.decide_provider = env.get("EVALUATOR_DECIDE_PROVIDER", cfg.decide_provider)
        cfg.decide_model = env.get("EVALUATOR_DECIDE_MODEL", cfg.decide_model)
        if value := env.get("EVALUATOR_DECIDE_THRESHOLD"):
            cfg.decide_threshold = float(value)
        if value := env.get("EVALUATOR_DECIDE_FLAG_THRESHOLD"):
            cfg.decide_flag_threshold = float(value)
        if value := env.get("EVALUATOR_DECIDE_ADVERTISING_THRESHOLD"):
            cfg.decide_advertising_threshold = float(value)
        cfg.own_db_path = env.get("EVALUATOR_DB_PATH", cfg.own_db_path)
        return cfg


def app_identity(cfg: Config) -> dict[str, str]:
    """The app_url/app_name pair for any router tool call; empty ones are omitted."""
    identity: dict[str, str] = {}
    if cfg.app_url:
        identity["app_url"] = cfg.app_url
    if cfg.app_name:
        identity["app_name"] = cfg.app_name
    return identity


# Provider spellings of two knobs the router passes through verbatim and
# reports as `ignored_params` when a provider does not know the name. The
# callers say what they want (an effort, a WxH frame) and this pair says it the
# way each provider's adapter reads it — codex-oauth takes `reasoning_effort`
# and `size`; OpenRouter takes `reasoning: {effort}` and, on its /images
# endpoint, `aspect_ratio` (the pixel size is the model's own choice there).


def reasoning_params(provider: str, effort: str) -> dict[str, Any]:
    """The router params that ask `provider` for this reasoning effort; {} for none."""
    if not effort:
        return {}
    if provider == OPENROUTER:
        return {"reasoning": {"effort": effort}}
    return {"reasoning_effort": effort}


def aspect_ratio(size: str) -> str:
    """`1024x1536` -> `2:3`; the size itself when it is not WxH."""
    width, sep, height = size.lower().partition("x")
    if not sep or not width.strip().isdigit() or not height.strip().isdigit():
        return size
    w, h = int(width), int(height)
    if w <= 0 or h <= 0:
        return size
    from math import gcd
    g = gcd(w, h)
    return f"{w // g}:{h // g}"


def image_params(provider: str, size: str) -> dict[str, Any]:
    """The router params that ask `provider` for a picture of this WxH frame.

    OpenRouter's images endpoint reads the frame as an aspect ratio and is asked
    for JPEG outright: the platforms take JPEG everywhere, and the reply travels
    inline as base64 in one MCP message, where a PNG at the model's native
    resolution (4 MB and up) does not fit. Other providers get the size as is.
    """
    if not size:
        return {}
    if provider == OPENROUTER:
        return {"aspect_ratio": aspect_ratio(size), "output_format": "jpeg"}
    return {"size": size}


def build_chat_arguments(cfg: Config, messages: list[dict[str, str]]) -> dict[str, Any]:
    """Router hints are optional: empty ones are omitted, the router decides."""
    arguments: dict[str, Any] = {
        "external_user_id": cfg.router_user or cfg.selector_name,
        "messages": messages,
        "params": cfg.params,
    }
    arguments.update(app_identity(cfg))
    if cfg.model_id:
        arguments["model_id"] = cfg.model_id
    if cfg.provider:
        arguments["provider"] = cfg.provider
    if cfg.tier:
        arguments["tier"] = cfg.tier
    return arguments


def chat(cfg: Config, messages: list[dict[str, str]]) -> dict[str, Any]:
    reply = call_tool(
        cfg.router_url,
        "chat",
        build_chat_arguments(cfg, messages),
        token=cfg.router_token or None,
    )
    if not isinstance(reply, dict) or not isinstance(reply.get("text"), str):
        raise McpError(f"unexpected chat reply shape: {type(reply).__name__}")
    return reply


# ---------------------------------------------------------- JSON validation


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Tolerates markdown fences, prose around the object, and trailing commas.
    """
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate_source = fenced.group(1) if fenced else text
    candidate = _first_balanced_object(candidate_source)
    if candidate is None and fenced:
        candidate = _first_balanced_object(text)
    if candidate is None:
        raise EvaluationInvalid("в ответе нет JSON-объекта")
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        try:
            payload = json.loads(re.sub(r",\s*([}\]])", r"\1", candidate))
        except json.JSONDecodeError as exc:
            raise EvaluationInvalid(f"JSON не разбирается: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvaluationInvalid("верхний уровень JSON не объект")
    return payload


def _first_balanced_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def _coerce_score(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"булево значение {value!r}")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"дробное значение {value!r}")
        number = int(value)
    elif isinstance(value, str):
        try:
            as_float = float(value.strip())
        except ValueError:
            raise ValueError(f"не число: {value!r}") from None
        if not as_float.is_integer():
            raise ValueError(f"дробное значение {value!r}")
        number = int(as_float)
    else:
        raise ValueError(f"не число: {value!r}")
    if not 0 <= number <= 10:
        raise ValueError(f"вне диапазона от 0 до 10: {number}")
    return number


def validate_evaluation(
    payload: dict[str, Any],
    expected_news_id: int,
    axis_keys: list[str],
    topic_keys: list[str] | None = None,
) -> tuple[dict[str, int], str, str, list[str]]:
    """Check a parsed model reply against the contract.

    Returns (scores, comment, topic, warnings); raises EvaluationInvalid when
    the reply cannot be trusted. Validation messages are in Russian because they
    are fed back to the model, whose instruction is Russian.

    A missing or unknown rubric is a warning, not a rejection: the scores are
    what the verdict is made of and what the model was paid for, while a rubric
    is one line in a report. Throwing a valid evaluation away over it would cost
    a second full answer. Such replies land on the placeholder rubric, and the
    run counters carry how often that happened.
    """
    warnings: list[str] = []

    echoed = payload.get("news_id")
    if echoed is not None:
        try:
            echoed_id = int(str(echoed).strip())
        except ValueError:
            raise EvaluationInvalid(f"news_id в ответе не число: {echoed!r}") from None
        if echoed_id != expected_news_id:
            raise EvaluationInvalid(
                f"news_id в ответе {echoed_id}, а оценивалась новость {expected_news_id}"
            )

    raw_scores = payload.get("scores")
    if not isinstance(raw_scores, dict):
        if raw_scores is None and all(key in payload for key in axis_keys):
            raw_scores = payload
            warnings.append("ключа scores нет, оси взяты с верхнего уровня ответа")
        else:
            raise EvaluationInvalid("в ответе нет словаря scores")

    scores: dict[str, int] = {}
    problems: list[str] = []
    for key in axis_keys:
        if key not in raw_scores:
            problems.append(f"нет оси {key}")
            continue
        try:
            scores[key] = _coerce_score(raw_scores[key])
        except ValueError as exc:
            problems.append(f"{key}: {exc}")
    if problems:
        raise EvaluationInvalid("; ".join(problems))

    known = set(axis_keys) | ({"news_id", "comment", "topic"} if raw_scores is payload else set())
    extra = sorted(set(raw_scores) - known)
    if extra:
        warnings.append("лишние ключи в scores игнорируются: " + ", ".join(extra))

    comment = payload.get("comment", "")
    if not isinstance(comment, str):
        warnings.append("comment не строка, заменён на пустой")
        comment = ""
    comment = " ".join(comment.split())[:MAX_COMMENT_CHARS]

    topic = PLACEHOLDER_TOPIC
    if topic_keys:
        raw_topic = payload.get("topic")
        candidate = raw_topic.strip().lower() if isinstance(raw_topic, str) else ""
        if candidate in topic_keys:
            topic = candidate
        elif not candidate:
            warnings.append("темы в ответе нет, поставлена заглушка")
        else:
            warnings.append(f"неизвестная тема {raw_topic!r}, поставлена заглушка")

    return scores, comment, topic, warnings


# -------------------------------------------------------- selection profile


@dataclass(frozen=True)
class SelectionProfile:
    """Per-axis thresholds that turn scores into a verdict.

    A news item is selected when every ``gates_min``/``gates_max`` bound holds
    and, if ``highlight_min`` is non-empty, at least one of its axes reaches its
    bound. A missing axis reads as 0. Bounds are inclusive.
    """

    name: str
    gates_min: dict[str, int]
    gates_max: dict[str, int]
    highlight_min: dict[str, int]
    # Revision of the row set this profile was read from; None for the built-in
    # fallback. It travels in selector_version, because the threshold table is
    # editable while the events it produced are not.
    revision: int | None = None

    @property
    def tag(self) -> str:
        """How this profile identifies itself inside selector_version."""
        return f"{self.name}.r{self.revision}" if self.revision is not None else f"{self.name}.builtin"

    def selects(self, scores: dict[str, int]) -> bool:
        if any(scores.get(axis, 0) < low for axis, low in self.gates_min.items()):
            return False
        if any(scores.get(axis, 0) > high for axis, high in self.gates_max.items()):
            return False
        if self.highlight_min:
            return any(scores.get(axis, 0) >= low for axis, low in self.highlight_min.items())
        return True

    def decide(self, scores: dict[str, int]) -> str:
        return "positive" if self.selects(scores) else "not_positive"


# Owner's rule (SPEC «Пороговая модель и метка "Отобрано"»): strict on purpose,
# few items pass. heroism is an upper gate here despite its lower_bound default.
DEFAULT_PROFILE = SelectionProfile(
    name="default",
    gates_min={"positivity": 8},
    gates_max={"heroism": 4, "clickbait": 4, "promo": 4},
    highlight_min={
        "pride_humanity": 9,
        "pride_russia": 9,
        "inspiration": 9,
        "beauty": 9,
        "interestingness": 9,
        "surprise": 9,
        "uniqueness": 9,
    },
)

ACTIVE_PROFILE_SQL = """
SELECT profile_name, profile_revision, characteristic_key, kind, value
FROM exchange_active_selection_profile
"""


def load_profile(con: sqlite3.Connection) -> SelectionProfile:
    """Read the thresholds in force from the crawler DB.

    One rule for two readers: the evaluator decides by it, the operator UI
    explains decisions by it. Falls back to the built-in DEFAULT_PROFILE when
    the view is missing (an older database, or the code rolled back past the
    migration) or holds nothing — a profile with no bounds would select
    everything, which is the one outcome nobody wants.
    """
    try:
        rows = con.execute(ACTIVE_PROFILE_SQL).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("no exchange_active_selection_profile view (%s); using the built-in profile", exc)
        return DEFAULT_PROFILE
    if not rows:
        log.warning("no active selection profile in the database; using the built-in profile")
        return DEFAULT_PROFILE

    gates_min: dict[str, int] = {}
    gates_max: dict[str, int] = {}
    highlight_min: dict[str, int] = {}
    buckets = {"gate_min": gates_min, "gate_max": gates_max, "highlight_min": highlight_min}
    for row in rows:
        bucket = buckets.get(row["kind"])
        if bucket is None:
            log.warning("unknown bound kind %r on axis %s, ignored", row["kind"], row["characteristic_key"])
            continue
        bucket[row["characteristic_key"]] = int(row["value"])
    return SelectionProfile(
        name=rows[0]["profile_name"],
        gates_min=gates_min,
        gates_max=gates_max,
        highlight_min=highlight_min,
        revision=int(rows[0]["profile_revision"]),
    )


# ------------------------------------------------------------------ prompt


def load_topics(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """The rubrics on offer, or nothing when the database predates them.

    Same graceful degradation as the selection profile: an older crawler database
    (or code rolled back past the migration) must not stop the evaluator. Without
    the table the prompt says nothing about rubrics and no topic row is written.
    """
    try:
        return con.execute(TOPICS_SQL).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("no exchange_topic table (%s); news will be evaluated without a rubric", exc)
        return []


def build_system_prompt(axes: list[sqlite3.Row], topics: list[sqlite3.Row] | None = None) -> str:
    lines = [
        "Ты оценщик новостей. Оцени новость по 20 характеристикам, "
        "каждую целым числом от 0 до 10.",
        "",
        "Правила шкалы.",
        "- 0 ставь, когда признак отсутствует или к новости неприменим; "
        "это не штраф. 10 ставь, когда признак выражен максимально.",
        "- Оси независимы: оценка по одной не влияет на другие, "
        "в сумму они не складываются.",
        "- negativity не зеркало positivity: у новости о спасении "
        "людей из пожара позитивность может быть 8, а негативность 5.",
        "- Оценивай только заголовок и текст. Изображений у тебя нет.",
        "",
        "Характеристики.",
    ]
    for axis in axes:
        lines.append(
            f"- {axis['key']} ({axis['title']}). {axis['description']}"
            f" 0: {axis['anchor_low']}. 10: {axis['anchor_high']}."
        )
    topic_field = ""
    if topics:
        lines += ["", "Рубрики. Выбери ровно одну, ту, о чём новость в первую очередь."]
        for topic in topics:
            lines.append(f"- {topic['key']} ({topic['title']}). {topic['description']}")
        lines.append(
            "Если новость подходит под несколько рубрик, бери ту, без которой "
            "новости бы не было. Ключ бери ровно из списка, ничего своего."
        )
        topic_field = '"topic": "<ключ одной рубрики из списка>", '

    lines += [
        "",
        "Формат ответа.",
        "Верни один JSON-объект и больше ничего: ни пояснений, ни markdown-разметки.",
        'Схема: {"news_id": <номер новости из задания>, '
        + topic_field
        + '"scores": {<все 20 ключей осей с целыми значениями>}, '
        '"comment": "<одно предложение по-русски: главное впечатление от новости>"}',
        "В scores обязаны быть все 20 ключей из списка выше.",
    ]
    return "\n".join(lines)


def _news_block(news: sqlite3.Row) -> str:
    body = (news["body_text"] or "").strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n(текст обрезан)"
    return f"Заголовок: {(news['title'] or '').strip()}\nТекст:\n{body}"


def build_user_message(news: sqlite3.Row) -> str:
    return f"Оцени новость news_id: {news['news_id']}\n" + _news_block(news)


RETRY_MESSAGE = (
    "Твой ответ не прошёл проверку: {error}. "
    "Пришли исправленный JSON той же схемы и больше ничего."
)


# ------------------------------------------------------------- final check


# Appended to selector_version when the final check rejects a news item the
# thresholds had selected. A veto is a judgement about the content, not about
# the thresholds, so the rescore pass must not undo it — it keys on this tag.
VETO_TAG = "veto"

FINAL_CHECK_SYSTEM = (
    "Ты выпускающий редактор ленты позитивных новостей. Читатель приходит "
    "сюда за радостью, вдохновением и добрыми историями.\n"
    "Реши, уместна ли новость в такой ленте. Суди не качество текста, "
    "а уместность самой новости.\n"
    "Неуместное: некролог; новость, где главное событие — смерть, гибель или "
    "тяжёлая болезнь; катастрофа, война, преступление или конфликт без "
    "счастливой развязки; политическая агитация; реклама.\n"
    "Уместное: история преодоления со счастливым концом; спасение; добрые "
    "дела; открытия и достижения; красота и забавные случаи.\n"
    "Сомневаешься — бракуй: одна неуместная публикация обходится каналу "
    "дороже, чем одна пропущенная хорошая.\n"
    "Формат ответа. Верни один JSON-объект и больше ничего: "
    '{"appropriate": true или false, "reason": "<одно предложение по-русски: почему>"}'
)

_FINAL_CHECK_TRUE = {"true", "yes", "да"}
_FINAL_CHECK_FALSE = {"false", "no", "нет"}


def build_final_check_message(news: sqlite3.Row) -> str:
    return "Уместна ли эта новость в ленте позитивных новостей?\n" + _news_block(news)


def validate_final_check(payload: dict[str, Any]) -> tuple[bool, str]:
    """Check a parsed final-check reply: a boolean verdict plus a one-line reason."""
    verdict = payload.get("appropriate")
    if isinstance(verdict, str):
        lowered = verdict.strip().lower()
        if lowered in _FINAL_CHECK_TRUE:
            verdict = True
        elif lowered in _FINAL_CHECK_FALSE:
            verdict = False
    if not isinstance(verdict, bool):
        raise EvaluationInvalid("в ответе нет поля appropriate со значением true или false")
    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        reason = ""
    reason = " ".join(reason.split())[:MAX_COMMENT_CHARS]
    return verdict, reason


def final_check(cfg: Config, news: sqlite3.Row) -> tuple[bool, str, dict[str, Any]]:
    """Ask the model whether a selected news item belongs on the channel.

    Same retry contract as evaluate_news: the validation error goes back to the
    model as feedback, and running out of attempts raises EvaluationInvalid.
    """
    messages = [
        {"role": "system", "content": FINAL_CHECK_SYSTEM},
        {"role": "user", "content": build_final_check_message(news)},
    ]
    last_error = "модель не отвечала"
    for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
        reply = chat(cfg, messages)
        text = reply["text"]
        try:
            verdict, reason = validate_final_check(extract_json_object(text))
        except EvaluationInvalid as exc:
            last_error = str(exc)
            log.warning(
                "news %s: final check attempt %d/%d rejected: %s",
                news["news_id"], attempt, MAX_MODEL_ATTEMPTS, last_error,
            )
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": RETRY_MESSAGE.format(error=last_error)})
            continue
        return verdict, reason, reply
    raise EvaluationInvalid(last_error)


# ------------------------------------------------ final check: decision model


# The same editorial question as FINAL_CHECK_SYSTEM, asked of a decision model
# (TypeSafe Jev through the router's `decide` tool). Not a chat: each question
# comes back as a probability, and the verdict is computed here, in code. One
# umbrella question plus one noul per exclusion rule of the chat prompt, so a
# veto can name the rule that fired instead of a free-text sentence the model
# would have written. Instructions are English — the model's best language;
# the news itself travels as it is — and every question spells out its
# boundary cases, because the model answers the question as written.
FINAL_CHECK_QUESTIONS: dict[str, dict[str, Any]] = {
    "appropriate": {
        "type": "noul",
        "instructions": (
            "Is this news story appropriate for a feed of positive news, where "
            "readers come for joy, inspiration and kind stories? Judge the story, "
            "not the writing quality."
        ),
        "criteria": {
            "true": (
                "A story of overcoming with a happy ending; a rescue that "
                "succeeded; kind deeds; discoveries and achievements; beauty; "
                "funny or heart-warming incidents."
            ),
            "false": (
                "An obituary; a story whose main event is a death, a fatal accident "
                "or a grave illness; a disaster, war, crime or conflict without a "
                "happy resolution; political campaigning; advertising or a press "
                "release promoting a product or company."
            ),
        },
    },
    "death_central": {
        "type": "noul",
        "instructions": (
            "Is this story, at its heart, about a death or a grave illness? Answer yes when: "
            "the main event is a death, a fatal accident or a life-threatening illness of a "
            "person or animal; or the piece is an obituary, tribute, memorial or look back at "
            "the life and legacy of someone who has died, even when the tone is warm, the "
            "death is only hinted at (\"left this world\", \"her legacy\", \"rest in peace\") and "
            "most of the text is about their life and good deeds. Answer no when a death or "
            "illness is only background to a story whose main event is a success, and when "
            "a rescue or recovery succeeded and everyone is well."
        ),
    },
    "unresolved_harm": {
        "type": "noul",
        "instructions": (
            "Is the main subject of this story a specific person, animal or community "
            "that is still in serious danger or suffering at the end of the story? Harm "
            "includes a disaster, war, crime, violence, conflict, and a life-threatening "
            "illness or injury. Answer yes when, for example, a patient is still waiting "
            "for a transplant, a cure or a surgery; people are still trapped, displaced or "
            "under attack; the story is built around a catastrophe or a war whose damage "
            "is still being felt. A hopeful or upbeat tone does not make it resolved. "
            "Answer no when the rescue, treatment or recovery is completed within the "
            "story; when past harm is only background to a present success; and when a "
            "broad problem (climate change, an endangered species, a disease in general, "
            "poverty, bullying) is the backdrop to a discovery, a project or an "
            "achievement that works against it."
        ),
    },
    "political": {
        "type": "noul",
        "instructions": (
            "Does this story take a side in politics? Answer yes for political campaigning; "
            "praising or attacking politicians, parties, governments or ideologies; an "
            "opinion column or editorial arguing a political or diplomatic position; "
            "agitating on a polarizing issue such as abortion, immigration, gender, guns "
            "or elections. In a digest of several stories, answer yes if any item does "
            "this. Answer no for a government programme or a court ruling reported "
            "neutrally, and for stories about civic life, public debate, community "
            "self-organisation or charity that do not take a political side."
        ),
    },
    "advertising": {
        "type": "noul",
        "instructions": (
            "Is the main purpose of this text to sell or promote something: a press "
            "release, sponsored content, a product review written to sell, or promotion "
            "of a product, service, brand or company? A news story that merely names a "
            "company, shop, book, film, award, charity, event or a person's business counts "
            "as no, and so does a scientific or engineering achievement reported as news."
        ),
    },
}

# Bumped whenever a question above is reworded; shadow rows carry it, so a
# calibration report compares only the verdicts of one wording. v2 (2026-09-24):
# the flags rewritten after replaying 14 past chat vetoes and 106 approved stories.
FINAL_CHECK_QUESTIONS_VERSION = "v2"

# Russian names for the red flags in a veto reason (operator-facing, event `reason`).
FINAL_CHECK_FLAG_TITLES = {
    "death_central": "смерть или тяжёлая болезнь в центре события",
    "unresolved_harm": "беда без счастливой развязки",
    "political": "политическая агитация",
    "advertising": "реклама",
}


def build_decide_state(news: sqlite3.Row) -> dict[str, str]:
    """The story as structured state: the model judges the object as a whole."""
    body = (news["body_text"] or "").strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS]
    return {"title": (news["title"] or "").strip(), "text": body}


def decide(cfg: Config, questions: dict[str, Any], state: Any) -> dict[str, Any]:
    """Call the router's `decide` tool; the reply carries `answers`, cost, usage."""
    arguments: dict[str, Any] = {
        "external_user_id": cfg.router_user or cfg.selector_name,
        "questions": questions,
        "state": state,
        **app_identity(cfg),
    }
    if cfg.decide_model:
        arguments["model_id"] = cfg.decide_model
    if cfg.decide_provider:
        arguments["provider"] = cfg.decide_provider
    started = time.monotonic()
    reply = call_tool(cfg.router_url, "decide", arguments, cfg.router_token)
    if not isinstance(reply, dict):
        raise McpError(f"decide: unexpected reply {reply!r}")
    reply["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return reply


def _noul(answers: dict[str, Any], name: str) -> float:
    answer = answers.get(name)
    value = answer.get("noul") if isinstance(answer, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationInvalid(f"в ответе нет вероятности noul для вопроса {name}")
    if not 0.0 <= float(value) <= 1.0:
        raise EvaluationInvalid(f"вероятность {name} вне диапазона 0..1: {value}")
    return float(value)


def flag_threshold(cfg: Config, name: str) -> float:
    if name == "advertising":
        return cfg.decide_advertising_threshold
    return cfg.decide_flag_threshold


def judge_decide_answers(
    cfg: Config, answers: Any
) -> tuple[bool, str, dict[str, float]]:
    """Turn the five probabilities into a verdict and a Russian reason.

    Appropriate when P(appropriate) reaches `decide_threshold` and no red flag
    reaches its threshold: `decide_advertising_threshold` for advertising,
    `decide_flag_threshold` for the rest. The flags are judged independently — the
    model promises no arithmetic between a question and its negation — so a
    confident flag vetoes even when the umbrella question says yes.
    """
    if not isinstance(answers, dict):
        raise EvaluationInvalid("в ответе нет объекта answers")
    probs = {name: _noul(answers, name) for name in FINAL_CHECK_QUESTIONS}
    fired = [
        name for name in FINAL_CHECK_FLAG_TITLES
        if probs[name] >= flag_threshold(cfg, name)
    ]
    appropriate = probs["appropriate"] >= cfg.decide_threshold and not fired
    parts = [f"уместность {probs['appropriate']:.2f}"]
    parts += [f"{FINAL_CHECK_FLAG_TITLES[name]} {probs[name]:.2f}" for name in fired]
    reason = ("модель решений: " if appropriate else "модель решений забраковала: ") + ", ".join(parts)
    return appropriate, reason, probs


def final_check_decide(cfg: Config, news: sqlite3.Row) -> tuple[bool, str, dict[str, Any]]:
    """The final check through the decision model. Same return shape as final_check.

    No retry loop: the answer is probabilities, not prose, so there is nothing
    the model could «fix» on a second attempt. A malformed reply is
    EvaluationInvalid, a router failure McpError, as with the chat path.
    """
    reply = decide(cfg, FINAL_CHECK_QUESTIONS, build_decide_state(news))
    appropriate, reason, probs = judge_decide_answers(cfg, reply.get("answers"))
    reply["probabilities"] = probs
    return appropriate, reason, reply


# --------------------------------------------------- final check: shadow log


SHADOW_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS final_check_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    chat_model TEXT NOT NULL DEFAULT '',
    chat_appropriate INTEGER,              -- 1/0; the verdict that was written
    chat_reason TEXT NOT NULL DEFAULT '',
    chat_cost_usd REAL,
    decide_model TEXT NOT NULL DEFAULT '',
    decide_appropriate INTEGER,            -- 1/0; NULL when the call failed
    decide_reason TEXT NOT NULL DEFAULT '',
    probabilities TEXT NOT NULL DEFAULT '{}',
    decide_cost_usd REAL,
    decide_ms INTEGER,
    error TEXT NOT NULL DEFAULT '',
    questions TEXT NOT NULL DEFAULT 'v1'  -- FINAL_CHECK_QUESTIONS_VERSION
);
CREATE INDEX IF NOT EXISTS idx_final_check_shadow_news ON final_check_shadow(news_id);
"""


def open_shadow_db(path: str) -> sqlite3.Connection | None:
    """The shadow table lives in the pipeline-owned DB, beside the run log."""
    con = runlog.open_runlog(path)
    if con is None:
        return None
    try:
        con.executescript(SHADOW_SCHEMA_SQL)
        # Rows from before the column existed were asked the v1 questions.
        columns = {row[1] for row in con.execute("PRAGMA table_info(final_check_shadow)")}
        if "questions" not in columns:
            con.execute(
                "ALTER TABLE final_check_shadow ADD COLUMN questions TEXT NOT NULL DEFAULT 'v1'"
            )
        con.commit()
    except sqlite3.Error as exc:
        log.warning("cannot create final_check_shadow at %s: %s", path, exc)
        con.close()
        return None
    return con


def shadow_final_check(
    cfg: Config, con: sqlite3.Connection | None, news: sqlite3.Row,
    chat_appropriate: bool, chat_reason: str, chat_reply: dict[str, Any],
) -> bool | None:
    """Run the decision model beside the chat verdict and record both.

    Returns the decision model's verdict, or None when it failed. Nothing here
    may break the run: the chat verdict is the one being written, this is the
    measurement.
    """
    decide_appropriate: bool | None = None
    reason, probs, reply, error = "", {}, {}, ""
    try:
        decide_appropriate, reason, reply = final_check_decide(cfg, news)
        probs = reply.get("probabilities") or {}
    except (EvaluationInvalid, McpError, urllib.error.URLError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.warning("news %s: shadow decide failed: %s", news["news_id"], error)
    if con is not None:
        try:
            with con:
                con.execute(
                    "INSERT INTO final_check_shadow (news_id, created_at, title, chat_model, "
                    "chat_appropriate, chat_reason, chat_cost_usd, decide_model, "
                    "decide_appropriate, decide_reason, probabilities, decide_cost_usd, "
                    "decide_ms, error, questions) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        news["news_id"], datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        (news["title"] or "")[:200], chat_reply.get("model_id") or cfg.model_id,
                        int(chat_appropriate), chat_reason, chat_reply.get("cost_usd"),
                        reply.get("served_model_id") or reply.get("model_id") or cfg.decide_model,
                        None if decide_appropriate is None else int(decide_appropriate),
                        reason, json.dumps(probs, ensure_ascii=False), reply.get("cost_usd"),
                        reply.get("elapsed_ms"), error[:500], FINAL_CHECK_QUESTIONS_VERSION,
                    ),
                )
        except sqlite3.Error as exc:
            log.warning("news %s: cannot record the shadow verdict: %s", news["news_id"], exc)
    if decide_appropriate is not None:
        log.info(
            "news %s: shadow decide %s chat (%s vs %s): %s",
            news["news_id"], "agrees with" if decide_appropriate == chat_appropriate else "DISAGREES with",
            decide_appropriate, chat_appropriate, reason,
        )
    return decide_appropriate


def shadow_report(path: str, out=sys.stdout, questions: str = FINAL_CHECK_QUESTIONS_VERSION) -> int:
    """Print how the two final checks compared so far: totals, then every disagreement.

    Only the rows asked the given question wording count: a verdict under old
    questions says nothing about the thresholds for the new ones.
    """
    con = open_shadow_db(path)
    if con is None:
        print(f"cannot open {path}", file=sys.stderr)
        return 1
    try:
        total, failed, agree, disagree = con.execute(
            "SELECT COUNT(*), SUM(decide_appropriate IS NULL), "
            "SUM(decide_appropriate = chat_appropriate), "
            "SUM(decide_appropriate IS NOT NULL AND decide_appropriate <> chat_appropriate) "
            "FROM final_check_shadow WHERE questions = ?", (questions,)
        ).fetchone()
        costs = con.execute(
            "SELECT COALESCE(SUM(chat_cost_usd), 0), COALESCE(SUM(decide_cost_usd), 0), "
            "COALESCE(AVG(decide_ms), 0) FROM final_check_shadow "
            "WHERE decide_appropriate IS NOT NULL AND questions = ?", (questions,)
        ).fetchone()
        print(f"questions {questions}; shadow rows: {total}, decide failed: {failed or 0}, "
              f"agree: {agree or 0}, disagree: {disagree or 0}", file=out)
        print(f"cost: chat ${costs[0]:.4f}, decide ${costs[1]:.4f}; decide avg {costs[2]:.0f} ms", file=out)
        rows = con.execute(
            "SELECT news_id, created_at, title, chat_appropriate, chat_reason, "
            "decide_appropriate, decide_reason, probabilities, error FROM final_check_shadow "
            "WHERE questions = ? "
            "AND (decide_appropriate IS NULL OR decide_appropriate <> chat_appropriate) "
            "ORDER BY id", (questions,)
        ).fetchall()
        for row in rows:
            print(json.dumps({key: row[key] for key in row.keys()}, ensure_ascii=False), file=out)
        return 0
    finally:
        con.close()


# ---------------------------------------------------------------- pipeline


def open_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def fetch_axes(con: sqlite3.Connection) -> list[sqlite3.Row]:
    axes = con.execute(AXES_SQL).fetchall()
    if len(axes) != AXIS_COUNT:
        raise RuntimeError(
            f"exchange_evaluation_characteristics has {len(axes)} axes, expected {AXIS_COUNT}"
        )
    return axes


def evaluate_news(
    cfg: Config,
    news: sqlite3.Row,
    system_prompt: str,
    axis_keys: list[str],
    topic_keys: list[str] | None = None,
) -> tuple[dict[str, int], str, str, dict[str, Any]]:
    """Ask the model, validate; retry with the validation error as feedback."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": build_user_message(news)},
    ]
    last_error = "модель не отвечала"
    for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
        reply = chat(cfg, messages)
        text = reply["text"]
        try:
            payload = extract_json_object(text)
            scores, comment, topic, warnings = validate_evaluation(
                payload, news["news_id"], axis_keys, topic_keys
            )
        except EvaluationInvalid as exc:
            last_error = str(exc)
            log.warning(
                "news %s: attempt %d/%d rejected: %s",
                news["news_id"], attempt, MAX_MODEL_ATTEMPTS, last_error,
            )
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": RETRY_MESSAGE.format(error=last_error)})
            continue
        for warning in warnings:
            log.info("news %s: %s", news["news_id"], warning)
        return scores, comment, topic, reply
    raise EvaluationInvalid(last_error)


def write_review(
    con: sqlite3.Connection,
    cfg: Config,
    news_id: int,
    scores: dict[str, int],
    comment: str,
    model_id: str,
    decision: str,
    profile_tag: str = "",
    topic: str = "",
) -> int:
    """Insert the review event and all axis scores in one transaction.

    model_id is the model that actually produced the scores (from the router
    reply), so selector_version stays truthful when the configured model changes.
    decision is the verdict from the selection profile (positive/not_positive)
    or 'skipped' when no verdict is reached. profile_tag names the thresholds
    that produced it («default.r3»): the threshold table is editable, the events
    are not, so without it an old decision cannot be explained later.
    """
    selector_version = f"{EVALUATOR_VERSION}+{model_id or 'router-choice'}"
    if profile_tag:
        selector_version = f"{selector_version}+{profile_tag}"
    idempotency_key = f"{news_id}:{selector_version}:{uuid.uuid4().hex[:12]}"
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for attempt in range(DB_LOCK_RETRIES):
        try:
            with con:
                cur = con.execute(
                    INSERT_EVENT_SQL,
                    (news_id, decision, comment, cfg.selector_name, selector_version,
                     idempotency_key, created_at),
                )
                event_id = cur.fetchone()[0]
                con.executemany(
                    INSERT_SCORE_SQL,
                    [(event_id, key, value) for key, value in scores.items()],
                )
                # Same transaction as the event on purpose: a scored news item
                # without a rubric would be invisible to «Состав ленты» and there
                # is no second pass that would come back for it.
                if topic:
                    con.execute(
                        INSERT_TOPIC_SQL,
                        (news_id, topic, cfg.selector_name, selector_version, created_at),
                    )
            return event_id
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == DB_LOCK_RETRIES - 1:
                raise
            delay = 0.5 * 2**attempt
            log.warning("database is locked, retrying in %.1fs", delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def run(cfg: Config, profile: SelectionProfile, limit: int, dry_run: bool,
        counters: dict | None = None) -> int:
    con = open_db(cfg.db_path)
    try:
        axes = fetch_axes(con)
        axis_keys = [axis["key"] for axis in axes]
        topics = load_topics(con)
        topic_keys = [topic["key"] for topic in topics]
        system_prompt = build_system_prompt(axes, topics)
        queue = con.execute(
            QUEUE_SQL, {"selector_name": cfg.selector_name, "batch_size": limit}
        ).fetchall()
        log.info("queue: %d news to evaluate (limit %d, profile %s)", len(queue), limit, profile.name)

        done, failed, selected, vetoed, total_cost, no_topic = 0, 0, 0, 0, 0.0, 0
        shadow_agree, shadow_disagree, shadow_failed = 0, 0, 0
        shadow_con = None
        if cfg.final_check and cfg.final_check_mode == "shadow" and not dry_run:
            shadow_con = open_shadow_db(cfg.own_db_path)
        check = final_check_decide if cfg.final_check_mode == "decide" else final_check
        for news in queue:
            title = (news["title"] or "")[:60]
            try:
                scores, comment, topic, reply = evaluate_news(
                    cfg, news, system_prompt, axis_keys, topic_keys
                )
            except EvaluationInvalid as exc:
                failed += 1
                log.error("news %s: giving up, stays in queue: %s", news["news_id"], exc)
                continue
            except (McpError, urllib.error.URLError) as exc:
                failed += 1
                log.error("news %s: router/model error: %s", news["news_id"], exc)
                continue
            decision = profile.decide(scores)
            total_cost += reply.get("cost_usd") or 0.0
            # The thresholds see 20 numbers; the final check sees the story. A
            # warm obituary passes the numbers, so a selected item needs both.
            veto_reason = ""
            if decision == "positive" and cfg.final_check:
                try:
                    appropriate, check_reason, check_reply = check(cfg, news)
                except EvaluationInvalid as exc:
                    failed += 1
                    log.error("news %s: final check failed, stays in queue: %s", news["news_id"], exc)
                    continue
                except (McpError, urllib.error.URLError) as exc:
                    failed += 1
                    log.error("news %s: final check router/model error: %s", news["news_id"], exc)
                    continue
                total_cost += check_reply.get("cost_usd") or 0.0
                if not appropriate:
                    decision = "not_positive"
                    veto_reason = check_reason or "модель сочла новость неуместной для канала"
                    vetoed += 1
                    log.info("news %s: final check vetoed: %s", news["news_id"], veto_reason)
                if cfg.final_check_mode == "shadow" and not dry_run:
                    shadow_verdict = shadow_final_check(
                        cfg, shadow_con, news, appropriate, check_reason, check_reply
                    )
                    if shadow_verdict is None:
                        shadow_failed += 1
                    elif shadow_verdict == appropriate:
                        shadow_agree += 1
                    else:
                        shadow_disagree += 1
            selected += decision == "positive"
            no_topic += bool(topic_keys) and topic == PLACEHOLDER_TOPIC
            if dry_run:
                log.info("news %s [dry-run] %s -> %s (%s)", news["news_id"], title, decision, topic)
                printed = {"news_id": news["news_id"], "decision": decision, "topic": topic,
                           "scores": scores, "comment": comment}
                if veto_reason:
                    printed["final_check"] = veto_reason
                print(json.dumps(printed, ensure_ascii=False))
            else:
                model_used = reply.get("model_id") or cfg.model_id
                event_id = write_review(
                    con, cfg, news["news_id"], scores,
                    f"Финальный контроль: {veto_reason}" if veto_reason else comment,
                    model_used, decision,
                    f"{profile.tag}+{VETO_TAG}" if veto_reason else profile.tag,
                    topic if topic_keys else "",
                )
                log.info("news %s: event %d %s: %s", news["news_id"], event_id, decision, title)
            done += 1
        log.info(
            "finished: %d evaluated (%d selected, %d vetoed), %d failed, model cost $%.4f (%s/%s)",
            done, selected, vetoed, failed, total_cost, cfg.provider, cfg.model_id,
        )
        if counters is not None:
            counters.update(queue=len(queue), evaluated=done, selected=selected,
                            vetoed=vetoed, failed=failed, cost_usd=round(total_cost, 4),
                            without_topic=no_topic)
            if shadow_con is not None:
                counters.update(shadow_agree=shadow_agree, shadow_disagree=shadow_disagree,
                                shadow_failed=shadow_failed)
        return 0 if failed == 0 else 1
    finally:
        con.close()
        if shadow_con is not None:
            shadow_con.close()


def run_backfill(cfg: Config, profile: SelectionProfile, dry_run: bool, rescore_all: bool = False,
                 counters: dict | None = None) -> int:
    """Re-verdict already-scored news from the stored scores, without the model.

    Two uses of one pass. By default it picks up news whose latest event is still
    'skipped' (scored before the profile existed). With rescore_all it replays the
    current thresholds over everything this selector has scored and writes only
    where the verdict changed — that is the «пересчитать уже оценённые» button
    after the operator edits the profile, and re-recording an unchanged verdict
    would only bloat the event log.

    Either way a change is a new event with the full score set and a new
    idempotency key, as the exchange contract requires.
    """
    con = open_db(cfg.db_path)
    try:
        by_news: dict[int, dict[str, int]] = {}
        current: dict[int, str] = {}
        versions: dict[int, str] = {}
        query = RESCORE_SQL if rescore_all else BACKFILL_SQL
        for row in con.execute(query, {"selector_name": cfg.selector_name}):
            by_news.setdefault(row["news_id"], {})[row["characteristic_key"]] = row["value"]
            current[row["news_id"]] = row["decision"]
            versions[row["news_id"]] = row["selector_version"] or ""
        log.info(
            "%s: %d news to re-verdict (profile %s)",
            "rescore" if rescore_all else "backfill", len(by_news), profile.tag,
        )

        processed, selected, incomplete, unchanged, vetoed = 0, 0, 0, 0, 0
        for news_id, scores in by_news.items():
            if len(scores) != AXIS_COUNT:
                incomplete += 1
                log.warning("news %s: %d/%d scores, skipping", news_id, len(scores), AXIS_COUNT)
                continue
            decision = profile.decide(scores)
            # A veto is the final check's call on the content itself; replaying
            # thresholds knows nothing about it and must not resurrect the item.
            if (decision == "positive" and current.get(news_id) == "not_positive"
                    and versions.get(news_id, "").endswith(f"+{VETO_TAG}")):
                vetoed += 1
                continue
            selected += decision == "positive"
            if rescore_all and decision == current.get(news_id):
                unchanged += 1
                continue
            if dry_run:
                log.info("news %s [dry-run] %s -> %s", news_id, current.get(news_id), decision)
            else:
                event_id = write_review(
                    con, cfg, news_id, scores, "", f"backfill:{profile.name}", decision, profile.tag
                )
                log.debug("news %s: event %d %s", news_id, event_id, decision)
            processed += 1
        log.info(
            "finished: %d corrected, %d selected by the profile, %d unchanged, "
            "%d vetoed kept, %d incomplete%s",
            processed, selected, unchanged, vetoed, incomplete,
            " (dry-run, nothing written)" if dry_run else "",
        )
        if counters is not None:
            counters.update(reviewed=len(by_news), corrected=processed, selected=selected,
                            unchanged=unchanged, vetoed_kept=vetoed, incomplete=incomplete)
        return 0
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score crawler news on the 20-axis set.")
    parser.add_argument("--limit", type=int, default=3, help="batch size (default 3)")
    parser.add_argument("--dry-run", action="store_true", help="evaluate and print, do not write")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="re-verdict already-scored 'skipped' news from stored scores; no model calls",
    )
    parser.add_argument(
        "--rescore-all",
        action="store_true",
        help="with --backfill: re-apply the profile to every scored news item, "
             "writing a correction only where the verdict changed",
    )
    parser.add_argument(
        "--builtin-profile",
        action="store_true",
        help="ignore the thresholds stored in the crawler DB and use the built-in ones",
    )
    parser.add_argument(
        "--shadow-report",
        action="store_true",
        help="print how the chat and decision-model final checks compared (shadow mode); no model calls",
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cfg = Config.from_env()
    if args.shadow_report:
        return shadow_report(cfg.own_db_path)
    if args.builtin_profile:
        profile = DEFAULT_PROFILE
    else:
        con = open_db(cfg.db_path)
        try:
            profile = load_profile(con)
        finally:
            con.close()
    log.info("selection profile %s", profile.tag)
    if not args.backfill and not cfg.router_token:
        log.error("ROUTER_AUTH_TOKEN is not set")
        return 2

    # A dry run is not something the machine did; it leaves no row.
    if args.dry_run:
        if args.backfill:
            return run_backfill(cfg, profile, dry_run=True, rescore_all=args.rescore_all)
        return run(cfg, profile, limit=args.limit, dry_run=True)

    service = "evaluator-backfill" if args.backfill else "evaluator"
    settings = {"profile": profile.tag, "selector": cfg.selector_name}
    if args.backfill:
        settings["scope"] = "all scored" if args.rescore_all else "skipped only"
    else:
        settings.update(model=cfg.model_id, provider=cfg.provider, batch=args.limit,
                        final_check=cfg.final_check)
        if cfg.final_check and cfg.final_check_mode != "chat":
            settings.update(final_check_mode=cfg.final_check_mode, decide_model=cfg.decide_model)
    with runlog.record(service, runlog.DEFAULT_DB, settings) as counters:
        if args.backfill:
            return run_backfill(cfg, profile, dry_run=False, rescore_all=args.rescore_all, counters=counters)
        return run(cfg, profile, limit=args.limit, dry_run=False, counters=counters)


if __name__ == "__main__":
    sys.exit(main())
