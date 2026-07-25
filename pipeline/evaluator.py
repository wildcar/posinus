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

# News whose latest event by this selector is still 'skipped' (no verdict yet),
# together with the scores attached to that event. Feeds the backfill pass.
BACKFILL_SQL = """
SELECT s.news_id, r.decision, s.characteristic_key, s.value
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
SELECT s.news_id, r.decision, s.characteristic_key, s.value
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
    router_url: str = "http://127.0.0.1:8088/mcp"
    router_token: str = ""
    provider: str = "deepseek"
    model_id: str = "deepseek-v4-pro"
    tier: str = ""
    selector_name: str = "news-evaluator"
    # max_tokens has to cover the model's reasoning tokens, not just the JSON answer.
    # deepseek-v4-pro spends ~950 completion tokens on one full 20-axis evaluation, and
    # when it hits the cap before writing content the provider returns an empty body —
    # which surfaces here as "DeepSeek returned an empty response". The old 1000-token
    # budget was fine for deepseek-chat and failed on most news with v4-pro.
    params: dict[str, Any] = field(default_factory=lambda: {"temperature": 0.3, "max_tokens": 4000})

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
        if value := env.get("EVALUATOR_MAX_TOKENS"):
            cfg.params["max_tokens"] = int(value)
        if value := env.get("EVALUATOR_TEMPERATURE"):
            cfg.params["temperature"] = float(value)
        return cfg


def build_chat_arguments(cfg: Config, messages: list[dict[str, str]]) -> dict[str, Any]:
    """Router hints are optional: empty ones are omitted, the router decides."""
    arguments: dict[str, Any] = {
        "external_user_id": cfg.selector_name,
        "messages": messages,
        "params": cfg.params,
    }
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
) -> tuple[dict[str, int], str, list[str]]:
    """Check a parsed model reply against the contract.

    Returns (scores, comment, warnings); raises EvaluationInvalid when the
    reply cannot be trusted. Validation messages are in Russian because they
    are fed back to the model, whose instruction is Russian.
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

    known = set(axis_keys) | ({"news_id", "comment"} if raw_scores is payload else set())
    extra = sorted(set(raw_scores) - known)
    if extra:
        warnings.append("лишние ключи в scores игнорируются: " + ", ".join(extra))

    comment = payload.get("comment", "")
    if not isinstance(comment, str):
        warnings.append("comment не строка, заменён на пустой")
        comment = ""
    comment = " ".join(comment.split())[:MAX_COMMENT_CHARS]

    return scores, comment, warnings


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


def build_system_prompt(axes: list[sqlite3.Row]) -> str:
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
    lines += [
        "",
        "Формат ответа.",
        "Верни один JSON-объект и больше ничего: ни пояснений, ни markdown-разметки.",
        'Схема: {"news_id": <номер новости из задания>, '
        '"scores": {<все 20 ключей осей с целыми значениями>}, '
        '"comment": "<одно предложение по-русски: главное впечатление от новости>"}',
        "В scores обязаны быть все 20 ключей из списка выше.",
    ]
    return "\n".join(lines)


def build_user_message(news: sqlite3.Row) -> str:
    body = (news["body_text"] or "").strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n(текст обрезан)"
    return (
        f"Оцени новость news_id: {news['news_id']}\n"
        f"Заголовок: {(news['title'] or '').strip()}\n"
        f"Текст:\n{body}"
    )


RETRY_MESSAGE = (
    "Твой ответ не прошёл проверку: {error}. "
    "Пришли исправленный JSON той же схемы и больше ничего."
)


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
) -> tuple[dict[str, int], str, dict[str, Any]]:
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
            scores, comment, warnings = validate_evaluation(payload, news["news_id"], axis_keys)
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
        return scores, comment, reply
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
        system_prompt = build_system_prompt(axes)
        queue = con.execute(
            QUEUE_SQL, {"selector_name": cfg.selector_name, "batch_size": limit}
        ).fetchall()
        log.info("queue: %d news to evaluate (limit %d, profile %s)", len(queue), limit, profile.name)

        done, failed, selected, total_cost = 0, 0, 0, 0.0
        for news in queue:
            title = (news["title"] or "")[:60]
            try:
                scores, comment, reply = evaluate_news(cfg, news, system_prompt, axis_keys)
            except EvaluationInvalid as exc:
                failed += 1
                log.error("news %s: giving up, stays in queue: %s", news["news_id"], exc)
                continue
            except (McpError, urllib.error.URLError) as exc:
                failed += 1
                log.error("news %s: router/model error: %s", news["news_id"], exc)
                continue
            decision = profile.decide(scores)
            selected += decision == "positive"
            total_cost += reply.get("cost_usd") or 0.0
            if dry_run:
                log.info("news %s [dry-run] %s -> %s", news["news_id"], title, decision)
                print(json.dumps(
                    {"news_id": news["news_id"], "decision": decision,
                     "scores": scores, "comment": comment},
                    ensure_ascii=False,
                ))
            else:
                model_used = reply.get("model_id") or cfg.model_id
                event_id = write_review(
                    con, cfg, news["news_id"], scores, comment, model_used, decision, profile.tag
                )
                log.info("news %s: event %d %s: %s", news["news_id"], event_id, decision, title)
            done += 1
        log.info(
            "finished: %d evaluated (%d selected), %d failed, model cost $%.4f (%s/%s)",
            done, selected, failed, total_cost, cfg.provider, cfg.model_id,
        )
        if counters is not None:
            counters.update(queue=len(queue), evaluated=done, selected=selected,
                            failed=failed, cost_usd=round(total_cost, 4))
        return 0 if failed == 0 else 1
    finally:
        con.close()


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
        query = RESCORE_SQL if rescore_all else BACKFILL_SQL
        for row in con.execute(query, {"selector_name": cfg.selector_name}):
            by_news.setdefault(row["news_id"], {})[row["characteristic_key"]] = row["value"]
            current[row["news_id"]] = row["decision"]
        log.info(
            "%s: %d news to re-verdict (profile %s)",
            "rescore" if rescore_all else "backfill", len(by_news), profile.tag,
        )

        processed, selected, incomplete, unchanged = 0, 0, 0, 0
        for news_id, scores in by_news.items():
            if len(scores) != AXIS_COUNT:
                incomplete += 1
                log.warning("news %s: %d/%d scores, skipping", news_id, len(scores), AXIS_COUNT)
                continue
            decision = profile.decide(scores)
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
            "finished: %d corrected, %d selected by the profile, %d unchanged, %d incomplete%s",
            processed, selected, unchanged, incomplete,
            " (dry-run, nothing written)" if dry_run else "",
        )
        if counters is not None:
            counters.update(reviewed=len(by_news), corrected=processed, selected=selected,
                            unchanged=unchanged, incomplete=incomplete)
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
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cfg = Config.from_env()
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
        settings.update(model=cfg.model_id, provider=cfg.provider, batch=args.limit)
    with runlog.record(service, runlog.DEFAULT_DB, settings) as counters:
        if args.backfill:
            return run_backfill(cfg, profile, dry_run=False, rescore_all=args.rescore_all, counters=counters)
        return run(cfg, profile, limit=args.limit, dry_run=False, counters=counters)


if __name__ == "__main__":
    sys.exit(main())
