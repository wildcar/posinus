#!/usr/bin/env python3
"""Картина дня: draws the daily picture and posts it to the platforms.

Once a day (per slot: `day` now, an evening slot later) this asks a chat model
for two things in one JSON reply — an image prompt built from the current date
and a random style, and a short Russian description of the day's holidays and
events — then draws the picture TWICE from that one prompt (vertical for
telegram and the owner's bot, horizontal for the sites and VK; the orientation
travels in the prompt as well as in `params.size`, see ORIENTATIONS) and publishes it
everywhere the news publisher posts, wildcar.org included (its own section,
`DAYPIC_WILDCAR_SECTION`). Every post carries the text «<title> · <дата>» plus
the description. The vertical file lands in DAYPIC_DIR under the stable name
`<YYYY-MM-DD>-<slot>.<ext>` (the horizontal one adds `-wide`), so external
consumers can pick it up by schedule.

The style is chosen at random from the slot's list, never repeating a style
this slot already used in the current calendar month; when no fresh style is
left, any goes — a repeat beats a missing day.

The slots — prompt, system prompt, style list, caption, generation time, model
hints, the two sizes — are the operator's to edit on the crawler's «Картина
дня» page and live in the crawler DB (`exchange_daypic_slot`, read-only here,
like the selection profile). Results live in the pipeline's own DB:
`daypic_item` one row per (day, slot), `daypic_publication` one row per
(item, platform), idempotent the same way news publications are. The stop cock
(`pause` in the request mailbox) holds the whole run, generation included.

A failed generation retries on the next timer run, at most DAYPIC_MAX_ATTEMPTS
per day: generation costs money, and a broken day has to end rather than burn
budget until midnight. The vertical picture is the gate; a failed horizontal
one only logs, and the platforms fall back to the vertical file. A dry run
builds and prints the prompt (one cheap chat call) but never spends an image
call, sends nothing and leaves no row.

Single-file, stdlib-only. Reuses the router client from evaluator.py and the
platform adapters and config from publisher.py.

Behavior: AGENTS/SPEC.md, section «Картина дня (daypic.py)».
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import random
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, replace
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import evaluator
import preparer   # _sniff_image_ext, MIN_IMAGE_BYTES
import publisher  # platform adapters, PublisherConfig, PreparedNews, read_pause
import runlog

log = logging.getLogger("posinus-daypic")

DAYPIC_VERSION = "0.2.0"
DAYPIC_ROUTER_USER = "daypic"   # external_user_id at the model router
MAX_PROMPT_ATTEMPTS = 2
DB_LOCK_RETRIES = 4

# The slots as the operator configured them. A missing table or an empty result
# is not an error: «Картина дня» is simply not set up on this database.
SLOTS_SQL = """
SELECT slot, enabled, title, generate_at, prompt, system_prompt, styles,
       chat_provider, chat_model, chat_reasoning_effort, chat_web_search,
       image_provider, image_model,
       image_size, image_size_wide
FROM exchange_daypic_slot
WHERE enabled = 1
ORDER BY slot
"""

OWN_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS daypic_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,              -- local date of the issue, YYYY-MM-DD
    slot TEXT NOT NULL,
    status TEXT NOT NULL,           -- 'error' | 'generated' | 'published'
    title TEXT,                     -- the composed post title: «Картина дня · 30 июля 2026»
    style TEXT,
    prompt TEXT,                    -- the final image prompt that was used
    caption TEXT,                   -- the day's description, goes under the picture
    file_path TEXT,                 -- vertical (telegram, the owner's bot)
    file_path_wide TEXT,            -- horizontal (sites and VK); may be NULL
    prompt_model_id TEXT,
    image_model_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,  -- generation attempts this day
    error TEXT,
    generated_at TEXT,
    published_at TEXT,
    file_purged_at TEXT,            -- set by retention.py
    UNIQUE (day, slot)
);
CREATE TABLE IF NOT EXISTS daypic_publication (
    item_id INTEGER NOT NULL REFERENCES daypic_item(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,           -- 'ok' | 'error'
    url TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (item_id, platform)
);
"""

# The prompt scaffolding around the operator's slot prompt. The slot can turn
# on the router's web search (chat_web_search; codex-oauth runs its native
# search tool), but the date discipline still matters either way: searched or
# remembered, the day must stay the passed date and not drift to a neighbouring
# one. The reply format is owned here, not by the slot text: the code is what
# parses it.
PROMPT_REQUEST = (
    "Сегодня {date}, {weekday}. Используй только эту дату и события и праздники именно "
    "этого дня; не придумывай другую дату и не смещайся на соседние дни."
    "{search_line}{style_line}\n\n"
    "{task}\n\n"
    "Картинка будет отрисована дважды, вертикально и горизонтально, поэтому ориентацию "
    "кадра в промпте не задавай. Верни один JSON-объект и больше ничего: "
    '{{"prompt": "<готовый промпт для модели генерации изображений>", '
    '"description": "<два-четыре предложения по-русски: какие сегодня праздники и '
    'события, нейтрально и дружелюбно>"}}'
)
STYLE_LINE = " Базовый стиль картинки: {style}."
# Only when the slot switched web search on: the model gets the tool from the
# router, and this is what tells it to actually spend it on today.
SEARCH_LINE = (
    " Найди в интернете праздники и события этой даты, приоритет российским, "
    "и опирайся на найденное."
)
RU_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

# When the chat model would not answer, the issue still goes out on this
# template: a plainer picture is better than a missing day.
FALLBACK_PROMPT = (
    "Создай «картину дня» для даты {date}. {style_line}"
    "Покажи спокойную, безопасную атмосферу этого дня: сезон, городской или праздничный "
    "контекст с приоритетом российских праздников, дружелюбное настроение, красивый свет "
    "и композицию открытки. Избегай насилия, трагедий, политики, катастроф и шок-контента. "
    "Без крупных надписей; только внизу небольшая плашка в стиле картинки с указанием "
    "по-русски, какие сегодня праздники. Спрячь в кадре небольшой весёлый визуальный "
    "сюрприз — деталь, которую зрителю будет приятно поискать и найти."
)

# Appended on the retry after the image provider refused the first prompt —
# usually a safety refusal over something the chat model pulled from the news.
SAFE_SUFFIX = (
    "Сделай изображение безопасным для широкой аудитории: без насилия, катастроф, оружия, "
    "трагедий, политики, реальных конфликтов и пугающих сцен. Если события дня чувствительные, "
    "передай только нейтральное настроение дня, сезон и праздники."
)

RETRY_MESSAGE = (
    "Твой ответ не прошёл проверку: {error}. "
    "Пришли один JSON-объект той же схемы и больше ничего."
)


class DaypicError(RuntimeError):
    """The picture could not be produced; the day retries on the next run."""


RUN_REQUEST = "run-daypic"


def consume_run_request(requests_dir: str) -> bool:
    """The operator's «Прогнать сейчас», as a file in the mailbox.

    Its presence lifts the generate_at gate for this pass: a human pressing the
    button means «сейчас», not «в свой час». Consumed before anything else —
    including the pause check — because the .path unit retriggers the service
    for as long as the file exists, and a paused run that left it in place
    would loop until the pause lifted."""
    path = Path(requests_dir) / RUN_REQUEST
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("cannot remove the run request %s: %s", path, exc)
        return False
    return True


# --------------------------------------------------------------- config


@dataclass
class DaypicConfig:
    news_db: str = "/var/lib/posinus/posinus.sqlite3"
    own_db: str = "/var/lib/posinus/pipeline/evaluator.sqlite3"
    daypic_dir: str = "/var/lib/posinus/pipeline/daypic"
    tz: str = "Europe/Moscow"
    max_attempts: int = 4
    # Router hints when the slot leaves its own fields empty.
    image_provider: str = "codex-oauth"
    image_model: str = ""
    image_size: str = "1024x1536"        # vertical: telegram and the pickup file
    image_size_wide: str = "1536x1024"   # horizontal: the sites and VK
    site_tags: str = "картина дня"
    wildcar_section: str = "kartina"     # the daily-picture section of wildcar.org

    @classmethod
    def from_env(cls, env: dict[str, str] = os.environ) -> "DaypicConfig":
        cfg = cls()
        cfg.news_db = env.get("NEWS_DB_PATH", cfg.news_db)
        cfg.own_db = env.get("EVALUATOR_DB_PATH", cfg.own_db)
        cfg.daypic_dir = env.get("DAYPIC_DIR", cfg.daypic_dir)
        cfg.tz = env.get("DAYPIC_TZ", cfg.tz).strip() or "UTC"
        if value := env.get("DAYPIC_MAX_ATTEMPTS"):
            cfg.max_attempts = int(value)
        cfg.image_provider = env.get("DAYPIC_IMAGE_PROVIDER", env.get("IMAGE_PROVIDER", cfg.image_provider))
        cfg.image_model = env.get("DAYPIC_IMAGE_MODEL", cfg.image_model)
        cfg.image_size = env.get("DAYPIC_IMAGE_SIZE", cfg.image_size)
        cfg.image_size_wide = env.get("DAYPIC_IMAGE_SIZE_WIDE", cfg.image_size_wide)
        cfg.site_tags = env.get("DAYPIC_SITE_TAGS", cfg.site_tags)
        cfg.wildcar_section = env.get("DAYPIC_WILDCAR_SECTION", cfg.wildcar_section).strip("/")
        return cfg


@dataclass(frozen=True)
class Slot:
    slot: str
    title: str
    generate_at: str
    prompt: str
    system_prompt: str
    styles: tuple[str, ...]
    chat_provider: str = ""
    chat_model: str = ""
    chat_reasoning_effort: str = ""
    chat_web_search: bool = False
    image_provider: str = ""
    image_model: str = ""
    image_size: str = ""
    image_size_wide: str = ""


def load_slots(news_db: str) -> list[Slot]:
    """The enabled slots from the crawler DB, or nothing.

    Same graceful degradation as the selection profile: an older crawler
    database must not fail the service — «Картина дня» is simply off there.
    """
    try:
        con = sqlite3.connect(f"file:{news_db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        log.warning("cannot open the crawler DB for daypic slots: %s", exc)
        return []
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(SLOTS_SQL).fetchall()
    except sqlite3.Error as exc:
        log.warning("no exchange_daypic_slot table (%s); картина дня is not set up", exc)
        return []
    finally:
        con.close()
    slots = []
    for row in rows:
        slots.append(Slot(
            slot=row["slot"],
            title=(row["title"] or "Картина дня").strip(),
            generate_at=(row["generate_at"] or "").strip(),
            prompt=(row["prompt"] or "").strip(),
            system_prompt=(row["system_prompt"] or "").strip(),
            styles=tuple(line.strip() for line in (row["styles"] or "").splitlines() if line.strip()),
            chat_provider=(row["chat_provider"] or "").strip(),
            chat_model=(row["chat_model"] or "").strip(),
            chat_reasoning_effort=(row["chat_reasoning_effort"] or "").strip(),
            chat_web_search=bool(row["chat_web_search"]),
            image_provider=(row["image_provider"] or "").strip(),
            image_model=(row["image_model"] or "").strip(),
            image_size=(row["image_size"] or "").strip(),
            image_size_wide=(row["image_size_wide"] or "").strip(),
        ))
    return slots


# ------------------------------------------------------------ prompt stage


def pick_style(styles: tuple[str, ...], used: set[str]) -> str:
    """A random style the slot has not used this month; any style when all are.

    Random rather than day-of-month by the owner's call: the calendar index made
    the whole month predictable. Non-repetition within the month is the part
    that matters — a feed where two mornings look alike reads as a machine."""
    if not styles:
        return ""
    fresh = [style for style in styles if style not in used]
    return random.choice(fresh or list(styles))


def used_styles(con: sqlite3.Connection, slot: str, day: str) -> set[str]:
    """Styles this slot already spent in the month of `day` (YYYY-MM-DD)."""
    return {
        row[0]
        for row in con.execute(
            "SELECT DISTINCT style FROM daypic_item "
            "WHERE slot = ? AND day LIKE ? AND style IS NOT NULL AND style <> ''",
            (slot, f"{day[:7]}-%"),
        )
    }


def compose_title(slot_title: str, now_local: datetime) -> str:
    """The post title: «Картина дня · 30 июля 2026»."""
    return f"{slot_title} · {publisher._format_date_ru(now_local)}"


def build_prompt_request(slot: Slot, now_local: datetime, style: str) -> str:
    return PROMPT_REQUEST.format(
        date=now_local.strftime("%Y-%m-%d"),
        weekday=RU_WEEKDAYS[now_local.weekday()],
        search_line=SEARCH_LINE if slot.chat_web_search else "",
        style_line=STYLE_LINE.format(style=style) if style else "",
        task=slot.prompt,
    )


def fallback_prompt(slot: Slot, now_local: datetime, style: str) -> str:
    return FALLBACK_PROMPT.format(
        date=now_local.strftime("%Y-%m-%d"),
        style_line=f"Стиль: {style}. " if style else "",
    )


def parse_prompt_reply(text: str) -> tuple[str, str]:
    """(prompt, description) out of the model's JSON reply.

    Raises EvaluationInvalid when there is no usable prompt; a missing or
    unusable description is not worth a second paid call — the picture is the
    product, the caption text merely dresses it."""
    payload = evaluator.extract_json_object(text)
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise evaluator.EvaluationInvalid("в ответе нет непустого поля prompt")
    description = payload.get("description")
    if not isinstance(description, str):
        description = ""
    return " ".join(prompt.split()), " ".join(description.split())


def build_prompt(
    router_cfg: "evaluator.Config", slot: Slot, now_local: datetime, style: str,
) -> tuple[str, str, str]:
    """Today's image prompt and day description; (prompt, description, model_id).

    Two attempts with the validation error fed back, then a last resort: a
    non-JSON but non-empty reply is used raw as the prompt (the model wrote a
    prompt, just not the envelope), and only after that the built-in template —
    the issue is worth more than the prose.
    """
    # Reasoning effort and web search ride the params next to temperature and
    # max_tokens; the router drops what a provider does not understand (it
    # reports them as ignored_params), so a slot pointed at a plainer provider
    # keeps working.
    params = dict(router_cfg.params)
    if slot.chat_reasoning_effort:
        params["reasoning_effort"] = slot.chat_reasoning_effort
    if slot.chat_web_search:
        params["web_search"] = True
    cfg = replace(
        router_cfg,
        provider=slot.chat_provider or router_cfg.provider,
        model_id=slot.chat_model or router_cfg.model_id,
        params=params,
    )
    messages = [
        {"role": "system", "content": slot.system_prompt},
        {"role": "user", "content": build_prompt_request(slot, now_local, style)},
    ]
    last_text = ""
    for attempt in range(1, MAX_PROMPT_ATTEMPTS + 1):
        try:
            reply = evaluator.chat(cfg, messages)
        except (evaluator.McpError, urllib.error.URLError, OSError) as exc:
            log.warning("slot %s: prompt attempt %d/%d failed: %s",
                        slot.slot, attempt, MAX_PROMPT_ATTEMPTS, exc)
            continue
        text = (reply.get("text") or "").strip()
        last_text = text or last_text
        try:
            prompt, description = parse_prompt_reply(text)
        except evaluator.EvaluationInvalid as exc:
            log.warning("slot %s: prompt attempt %d/%d rejected: %s",
                        slot.slot, attempt, MAX_PROMPT_ATTEMPTS, exc)
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": RETRY_MESSAGE.format(error=exc)})
            continue
        return prompt, description, reply.get("model_id") or cfg.model_id
    if last_text:
        log.warning("slot %s: no valid JSON after %d attempts, using the raw reply as the prompt",
                    slot.slot, MAX_PROMPT_ATTEMPTS)
        return last_text, "", cfg.model_id
    log.warning("slot %s: falling back to the built-in prompt template", slot.slot)
    return fallback_prompt(slot, now_local, style), "", "fallback-template"


# ------------------------------------------------------------- image stage


# The orientation has to be in the PROMPT, not only in `params.size`. The
# codex-oauth backend drops the requested size: on 2026-07-30 the router asked
# `1024x1536` and got 1536x1024 back, twice (router request_logs 8618, 8570) —
# it is the model that picks the canvas when it emits the image_generation call,
# and it reads the prompt. Saying it in words produced a real 1024x1536 frame,
# so both channels are used now: the size for providers that honour it, the
# sentence for the one that does not.
ORIENTATIONS = {
    "vertical": "Вертикальный портретный кадр, ориентация 2:3: высота больше ширины.",
    "horizontal": "Горизонтальный кадр, ориентация 3:2: ширина больше высоты.",
}


def _png_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a PNG, or None when these are not PNG bytes.

    Enough to tell portrait from landscape without a decoder: the provider
    returns PNG, and the check exists to catch the day the orientation silently
    flips again."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
        return None
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


def generate_picture(
    cfg: DaypicConfig, router_cfg: "evaluator.Config", slot: Slot,
    prompt: str, day: str, size: str, orientation: str, suffix: str = "",
) -> tuple[str, str]:
    """Generate and save one rendition; (file path, image model id).

    One retry with the safety suffix: image providers refuse prompts that carry
    too much of the day's news, and a toned-down picture beats a missing one.
    """
    provider = slot.image_provider or cfg.image_provider
    model = slot.image_model or cfg.image_model
    framed = f"{ORIENTATIONS[orientation]}\n\n{prompt}" if orientation in ORIENTATIONS else prompt
    reply = None
    for attempt, text in enumerate((framed, f"{framed}\n\n{SAFE_SUFFIX}"), start=1):
        arguments: dict = {
            "external_user_id": router_cfg.router_user or DAYPIC_ROUTER_USER,
            "prompt": text,
        }
        if provider:
            arguments["provider"] = provider
        if model:
            arguments["model_id"] = model
        if size:
            arguments["params"] = {"size": size}
        try:
            reply = evaluator.call_tool(router_cfg.router_url, "generate_image", arguments,
                                        token=router_cfg.router_token or None)
            break
        except (evaluator.McpError, urllib.error.URLError, OSError) as exc:
            log.warning("slot %s: image attempt %d/2 (%s) failed: %s", slot.slot, attempt, size, exc)
            if attempt == 2:
                raise DaypicError(f"генерация картинки не удалась: {exc}") from exc
    blobs = reply.get("image_b64") if isinstance(reply, dict) else None
    if not isinstance(blobs, list) or not blobs:
        raise DaypicError("модель не вернула изображение")
    try:
        data = base64.b64decode(blobs[0])
    except (TypeError, ValueError) as exc:
        raise DaypicError(f"изображение не декодируется из base64: {exc}") from exc
    if len(data) < preparer.MIN_IMAGE_BYTES:
        raise DaypicError(f"изображение неправдоподобно маленькое ({len(data)} байт)")
    # Warn rather than fail: a wrongly-framed picture still beats no issue, and
    # the log line is what tells the operator the backend changed its mind.
    measured = _png_size(data)
    if measured is not None:
        got = "vertical" if measured[1] > measured[0] else "horizontal"
        if orientation in ORIENTATIONS and got != orientation:
            log.warning("slot %s: asked for a %s frame, got %dx%d (%s)",
                        slot.slot, orientation, measured[0], measured[1], got)
    target_dir = Path(cfg.daypic_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{day}-{slot.slot}{suffix}{preparer._sniff_image_ext(data)}"
    path.write_bytes(data)
    path = preparer.shrink_image(path)
    model_id = reply.get("model_id") or model or provider
    log.info("slot %s: %s %s picture generated via %s (%d bytes) -> %s",
             slot.slot, orientation, size or "default-size", model_id, len(data), path)
    return str(path), model_id


def generate_pictures(
    cfg: DaypicConfig, router_cfg: "evaluator.Config", slot: Slot, prompt: str, day: str,
) -> tuple[str, str | None, str]:
    """Both renditions from one prompt; (vertical path, wide path or None, model).

    The vertical picture is the gate — it feeds telegram and the pickup file,
    and without it there is no issue. The horizontal one is best-effort: on
    failure the platforms take the vertical file and the day is not held up.
    """
    vertical, model_id = generate_picture(
        cfg, router_cfg, slot, prompt, day,
        slot.image_size or cfg.image_size, "vertical")
    try:
        wide, _ = generate_picture(
            cfg, router_cfg, slot, prompt, day,
            slot.image_size_wide or cfg.image_size_wide, "horizontal", suffix="-wide")
    except DaypicError as exc:
        log.warning("slot %s: horizontal rendition failed, platforms will take the vertical one: %s",
                    slot.slot, exc)
        wide = None
    return vertical, wide, model_id


# ---------------------------------------------------------------- storage


def migrate_own_db(con: sqlite3.Connection) -> None:
    """Bring a first-deploy daypic schema forward: the two-orientation issue
    added `file_path_wide` and `caption`. Empty tables in the wild, but ALTER
    keeps even a non-empty one."""
    columns = {row["name"] for row in con.execute("PRAGMA table_info(daypic_item)")}
    if columns and "file_path_wide" not in columns:
        con.execute("ALTER TABLE daypic_item ADD COLUMN file_path_wide TEXT")
        con.commit()
    columns = {row["name"] for row in con.execute("PRAGMA table_info(daypic_item)")}
    if columns and "caption" not in columns:
        con.execute("ALTER TABLE daypic_item ADD COLUMN caption TEXT")
        con.commit()


def open_own_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    # Same WAL settings as the rest of the pipeline DB: the operator UI reads
    # this database and must never block the write after a post went out.
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA foreign_keys = ON")
    migrate_own_db(con)
    con.executescript(OWN_SCHEMA_SQL)
    return con


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _with_lock_retries(con: sqlite3.Connection, action) -> None:
    for attempt in range(DB_LOCK_RETRIES):
        try:
            with con:
                action()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == DB_LOCK_RETRIES - 1:
                raise
            delay = 0.5 * 2**attempt
            log.warning("database is locked, retrying in %.1fs", delay)
            time.sleep(delay)


def get_item(con: sqlite3.Connection, day: str, slot: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM daypic_item WHERE day = ? AND slot = ?", (day, slot)
    ).fetchone()


def record_generated(
    con: sqlite3.Connection, day: str, slot: Slot, title: str, style: str,
    prompt: str, caption: str, file_path: str, file_path_wide: str | None,
    prompt_model: str, image_model: str,
) -> None:
    def action() -> None:
        con.execute(
            "INSERT INTO daypic_item (day, slot, status, title, style, prompt, caption, "
            "file_path, file_path_wide, prompt_model_id, image_model_id, attempts, generated_at) "
            "VALUES (?, ?, 'generated', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(day, slot) DO UPDATE SET status='generated', title=excluded.title, "
            "style=excluded.style, prompt=excluded.prompt, caption=excluded.caption, "
            "file_path=excluded.file_path, file_path_wide=excluded.file_path_wide, "
            "prompt_model_id=excluded.prompt_model_id, image_model_id=excluded.image_model_id, "
            "attempts=daypic_item.attempts+1, generated_at=excluded.generated_at, error=NULL",
            (day, slot.slot, title, style, prompt, caption, file_path, file_path_wide,
             prompt_model, image_model, _now_iso()),
        )
    _with_lock_retries(con, action)


def record_failure(con: sqlite3.Connection, day: str, slot: Slot, error: str) -> None:
    def action() -> None:
        con.execute(
            "INSERT INTO daypic_item (day, slot, status, title, attempts, error) "
            "VALUES (?, ?, 'error', ?, 1, ?) "
            "ON CONFLICT(day, slot) DO UPDATE SET status='error', "
            "attempts=daypic_item.attempts+1, error=excluded.error",
            (day, slot.slot, slot.title, error[:1000]),
        )
    _with_lock_retries(con, action)


def record_publication(
    con: sqlite3.Connection, item_id: int, platform: str, status: str,
    url: str | None, error: str | None,
) -> None:
    def action() -> None:
        con.execute(
            "INSERT INTO daypic_publication (item_id, platform, status, url, error, attempts, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(item_id, platform) DO UPDATE SET status=excluded.status, url=excluded.url, "
            "error=excluded.error, attempts=daypic_publication.attempts+1, updated_at=excluded.updated_at",
            (item_id, platform, status, url, (error or "")[:1000] or None, _now_iso()),
        )
    _with_lock_retries(con, action)


def publication_state(con: sqlite3.Connection, item_id: int) -> dict[str, tuple[str, int]]:
    return {row["platform"]: (row["status"], row["attempts"])
            for row in con.execute(
                "SELECT platform, status, attempts FROM daypic_publication WHERE item_id = ?",
                (item_id,))}


def finalize(con: sqlite3.Connection, item_id: int) -> None:
    def action() -> None:
        con.execute(
            "UPDATE daypic_item SET status = 'published', "
            "published_at = COALESCE(published_at, ?) WHERE id = ?",
            (_now_iso(), item_id),
        )
    _with_lock_retries(con, action)


# ------------------------------------------------- platform: wildcar.org


DAYPIC_NAV_YML = """\
# Generated by the posinus daily-picture service (daypic.py) — do not edit here.
title: Картина дня
nav:
  - index.md
"""


def wildcar_slug(day: str, slot: str) -> str:
    """The page directory: the plain date for the main slot, date-slot for others."""
    return day if slot == "day" else f"{day}-{slot}"


def wildcar_section_url(cfg: DaypicConfig, pub_cfg: "publisher.PublisherConfig") -> str:
    return f"{pub_cfg.wildcar_base}/{cfg.wildcar_section}/"


def build_wildcar_page(title: str, image_name: str, caption: str, tags: list[str] | None = None) -> str:
    parts = [f"# {title}"]
    if image_name:
        parts.append(f"![]({urllib.parse.quote(image_name)})")
    if caption:
        parts.append(caption)
    # The same tags Эгея gets (DAYPIC_SITE_TAGS, «картина дня»), as front
    # matter for the Material tags plugin.
    return publisher.build_front_matter(tags or []) + "\n\n".join(parts) + "\n"


def build_wildcar_index(entries: list[tuple[str, str, str]]) -> str:
    """The section index; entries are (slug, day, title), newest first."""
    lines = [
        "# Картина дня",
        "",
        "Каждый день машина рисует картину по праздникам и событиям этого дня "
        "и публикует её здесь и на площадках проекта "
        "[posinus](https://github.com/wildcar/posinus).",
        "",
    ]
    for slug, day, title in entries:
        year, month, dom = day.split("-")
        lines.append(f"- {dom}.{month}.{year} — [{title}]({slug}/index.md)")
    return "\n".join(lines) + "\n"


def _wildcar_published_issues(con: sqlite3.Connection, exclude_id: int) -> list[tuple[str, str, str]]:
    """Issues already on wildcar.org, newest first, for the regenerated index."""
    return [
        (wildcar_slug(row["day"], row["slot"]), row["day"], row["title"] or "Картина дня")
        for row in con.execute(
            "SELECT i.day, i.slot, i.title FROM daypic_publication p "
            "JOIN daypic_item i ON i.id = p.item_id "
            "WHERE p.platform = 'wildcar_org' AND p.status = 'ok' AND i.id <> ? "
            "ORDER BY i.day DESC, i.slot ASC", (exclude_id,))
    ]


def publish_wildcar_org(
    cfg: DaypicConfig, pub_cfg: "publisher.PublisherConfig", con: sqlite3.Connection,
    row: sqlite3.Row, item: "publisher.PreparedNews",
) -> str:
    """Write the issue into the site's daily-picture section and wait for it.

    Same mechanics as the news platform: files into the content directory, the
    rebuild marker for the site-build unit, then wait until the page answers.
    Everything here is regenerated on a retry, so a half-finished run heals."""
    slug = wildcar_slug(row["day"], row["slot"])
    page_url = wildcar_section_url(cfg, pub_cfg) + f"{slug}/"

    section = Path(pub_cfg.wildcar_content_dir) / cfg.wildcar_section
    page_dir = section / slug
    page_dir.mkdir(parents=True, exist_ok=True)
    image_name = ""
    if item.lead_image:
        image_name = Path(item.lead_image).name
        shutil.copyfile(item.lead_image, page_dir / image_name)
    (page_dir / "index.md").write_text(
        build_wildcar_page(item.title, image_name, "\n\n".join(item.paragraphs),
                           publisher.split_tags(cfg.site_tags)),
        encoding="utf-8",
    )
    entries = [(slug, row["day"], item.title)] + _wildcar_published_issues(con, exclude_id=row["id"])
    (section / ".nav.yml").write_text(DAYPIC_NAV_YML, encoding="utf-8")
    (section / "index.md").write_text(build_wildcar_index(entries), encoding="utf-8")

    Path(pub_cfg.requests_dir, publisher.WILDCAR_REBUILD_MARKER).touch()
    deadline = time.monotonic() + max(pub_cfg.wildcar_wait_seconds, 0)
    while True:
        try:
            status, _ = publisher.http_send(page_url, method="GET", timeout=30)
        except OSError:
            status = 0
        if status == 200:
            return page_url
        if time.monotonic() >= deadline:
            raise publisher.PublishError(
                f"wildcar_org: {page_url} is not live after {pub_cfg.wildcar_wait_seconds}s "
                f"(status {status}); is posinus-wildcar-org-build.path running?")
        time.sleep(3)


# ---------------------------------------------------------------- pipeline


def slot_due(slot: Slot, now_local: datetime) -> bool:
    """Has the slot's local generation time passed today?

    An unparsable time reads as «due all day»: the safe failure here is a
    picture that comes early, not a slot that never fires."""
    raw = slot.generate_at
    try:
        hour, _, minute = raw.partition(":")
        moment = dt_time(int(hour), int(minute or 0))
    except ValueError:
        if raw:
            log.warning("slot %s: cannot parse generate_at %r, treating the slot as due", slot.slot, raw)
        return True
    return now_local.time() >= moment


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown time zone %r, falling back to UTC", name)
        return ZoneInfo("UTC")


def publish_item(
    cfg: DaypicConfig, pub_cfg: "publisher.PublisherConfig", con: sqlite3.Connection,
    item_row: sqlite3.Row, platforms: list[str],
) -> bool:
    """Send one issue to every platform still pending; True when it settled.

    Telegram takes the vertical picture, everything else the horizontal one
    (falling back to the vertical when it is missing). Same shape as the news
    publisher: a platform already 'ok' is skipped, a failing one retries up to
    max_attempts, and the issue finalizes best-effort with whatever platforms
    succeeded, so a broken platform cannot hold the picture of every following
    day."""
    item_id = item_row["id"]
    vertical = item_row["file_path"] or ""
    if not vertical or not Path(vertical).exists():
        log.error("daypic %s/%s: picture file %s is missing, nothing to publish",
                  item_row["day"], item_row["slot"], vertical)
        return False
    wide = item_row["file_path_wide"] or ""
    if not wide or not Path(wide).exists():
        wide = vertical
    title = item_row["title"] or "Картина дня"
    paragraphs = [item_row["caption"]] if item_row["caption"] else []

    def item_for(platform: str) -> "publisher.PreparedNews":
        image = vertical if platform == "telegram" else wide
        return publisher.PreparedNews(
            news_id=item_id, title=title, paragraphs=list(paragraphs),
            lead_image=image, source_url="", source_name="", images=[(image, "")],
        )

    state = publication_state(con, item_id)
    pending = [p for p in platforms
               if state.get(p, ("", 0))[0] != "ok" and state.get(p, ("", 0))[1] < pub_cfg.max_attempts]
    for platform in pending:
        try:
            if platform == "wildcar_org":
                url = publish_wildcar_org(cfg, pub_cfg, con, item_row, item_for(platform))
            else:
                url = publisher.ADAPTERS[platform](pub_cfg, item_for(platform), False)
        except Exception as exc:  # one bad platform must not sink the rest
            log.error("daypic %s/%s -> %s failed: %s", item_row["day"], item_row["slot"], platform, exc)
            record_publication(con, item_id, platform, "error", None, str(exc))
            continue
        log.info("daypic %s/%s -> %s ok: %s", item_row["day"], item_row["slot"], platform, url)
        record_publication(con, item_id, platform, "ok", url, None)

    state = publication_state(con, item_id)
    settled = all(state.get(p, ("", 0))[0] == "ok" or state.get(p, ("", 0))[1] >= pub_cfg.max_attempts
                  for p in platforms)
    if settled:
        finalize(con, item_id)
        gave_up = [p for p in platforms if state.get(p, ("", 0))[0] != "ok"]
        if gave_up:
            log.warning("daypic %s/%s: published best-effort, gave up on %s",
                        item_row["day"], item_row["slot"], ", ".join(gave_up))
    return settled


def run(cfg: DaypicConfig, router_cfg: "evaluator.Config", dry_run: bool,
        only_slot: str | None = None, ignore_time: bool = False,
        counters: dict | None = None, now: datetime | None = None) -> int:
    now_utc = now or datetime.now(timezone.utc)
    pub_cfg = publisher.PublisherConfig.from_env()
    pub_cfg.site_tags = cfg.site_tags
    platforms = pub_cfg.enabled_platforms()

    if not dry_run and consume_run_request(pub_cfg.requests_dir):
        ignore_time = True
        log.info("manual run request consumed: the generate_at gate is lifted for this pass")

    pause = publisher.read_pause(pub_cfg.requests_dir, now_utc)
    if pause is not None and not dry_run:
        # The stop cock holds generation too: a paused channel that keeps
        # spending image calls is not paused.
        if counters is not None:
            counters.update(paused=True, reason=pause.reason)
        log.warning("publication paused%s; картина дня waits with everything else",
                    f" until {pause.until.isoformat()}" if pause.until else "")
        return 0

    now_local = now_utc.astimezone(_zone(cfg.tz))
    day = now_local.date().isoformat()
    slots = load_slots(cfg.news_db)
    log.info("day %s, %d slot(s) enabled, platforms [%s]%s",
             day, len(slots), ", ".join(platforms), " (dry-run)" if dry_run else "")

    generated, published, failed, skipped = 0, 0, 0, 0
    own = open_own_db(cfg.own_db)
    try:
        for slot in slots:
            if only_slot is not None and slot.slot != only_slot:
                continue
            if not ignore_time and not slot_due(slot, now_local):
                skipped += 1
                log.debug("slot %s: not due before %s", slot.slot, slot.generate_at)
                continue
            row = get_item(own, day, slot.slot)
            if row is not None and row["status"] == "published":
                continue

            has_picture = bool(row and row["file_path"] and Path(row["file_path"]).exists())
            if not has_picture:
                if row is not None and row["attempts"] >= cfg.max_attempts:
                    log.warning("slot %s: %d failed attempts today, giving the day up",
                                slot.slot, row["attempts"])
                    continue
                style = pick_style(slot.styles, used_styles(own, slot.slot, day))
                prompt, description, prompt_model = build_prompt(router_cfg, slot, now_local, style)
                if dry_run:
                    log.info("slot %s [dry-run]: style '%s', prompt via %s; no image call is spent",
                             slot.slot, style, prompt_model)
                    print(prompt)
                    if description:
                        print(f"--- описание ---\n{description}")
                    continue
                try:
                    vertical, wide, image_model = generate_pictures(cfg, router_cfg, slot, prompt, day)
                except DaypicError as exc:
                    failed += 1
                    log.error("slot %s: %s", slot.slot, exc)
                    record_failure(own, day, slot, str(exc))
                    continue
                record_generated(own, day, slot, compose_title(slot.title, now_local), style,
                                 prompt, description, vertical, wide, prompt_model, image_model)
                generated += 1
                row = get_item(own, day, slot.slot)

            if dry_run:
                continue
            if not platforms:
                log.warning("slot %s: no platform configured; the picture is saved but goes nowhere",
                            slot.slot)
                continue
            if publish_item(cfg, pub_cfg, own, row, platforms):
                published += 1
        if counters is not None:
            counters.update(slots=len(slots), generated=generated, published=published,
                            failed=failed, not_due=skipped)
        log.info("finished: %d generated, %d published, %d failed%s",
                 generated, published, failed, " (dry-run, nothing sent)" if dry_run else "")
        return 0 if failed == 0 else 1
    finally:
        own.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate and publish the daily picture.")
    parser.add_argument("--slot", default=None, help="run only this slot")
    parser.add_argument("--ignore-time", action="store_true",
                        help="skip the generate_at gate (manual check)")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and print the prompt; no image call, nothing sent, no rows")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cfg = DaypicConfig.from_env()
    router_cfg = evaluator.Config.from_env()
    # Its own spender id at the router, like the preparer: otherwise the daily
    # picture's tokens land on the evaluator in the usage report.
    router_cfg.router_user = os.environ.get("DAYPIC_ROUTER_USER_ID", DAYPIC_ROUTER_USER)
    router_cfg.params = {
        "temperature": float(os.environ.get("DAYPIC_TEMPERATURE", "0.7")),
        "max_tokens": int(os.environ.get("DAYPIC_MAX_TOKENS", "4000")),
    }
    if not router_cfg.router_token:
        log.error("ROUTER_AUTH_TOKEN is not set")
        return 2
    if args.dry_run:  # a dry run is not something the machine did; it leaves no row
        return run(cfg, router_cfg, dry_run=True, only_slot=args.slot, ignore_time=args.ignore_time)
    settings = {
        "tz": cfg.tz,
        "image_provider": cfg.image_provider,
        "image_sizes": f"{cfg.image_size}+{cfg.image_size_wide}",
        "max_attempts": cfg.max_attempts,
    }
    with runlog.record("daypic", cfg.own_db, settings) as counters:
        return run(cfg, router_cfg, dry_run=False, only_slot=args.slot,
                   ignore_time=args.ignore_time, counters=counters)


if __name__ == "__main__":
    sys.exit(main())
