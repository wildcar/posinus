#!/usr/bin/env python3
"""Картина дня: draws the daily picture and posts it to the platforms.

Once a day (per slot: `day` now, an evening slot later) this builds an image
prompt from the current date — a chat model turns the slot's instruction, the
day-of-month style and the calendar into one ready prompt — generates the
picture through the router's `generate_image` tool and publishes it to the
publisher's platforms (telegram, Эгея site, VK; wildcar_org is a separate
owner's call). The picture file lands in DAYPIC_DIR under a stable name
`<YYYY-MM-DD>-<slot>.<ext>`, so external consumers (the owner's bot) can pick
it up by schedule.

The slots — prompt, system prompt, style list, caption, generation time, model
hints — are the operator's to edit on the crawler's «Картина дня» page and live
in the crawler DB (`exchange_daypic_slot`, read-only here, like the selection
profile). Results live in the pipeline's own DB: `daypic_item` one row per
(day, slot), `daypic_publication` one row per (item, platform), idempotent the
same way news publications are. The stop cock (`pause` in the request mailbox)
holds the whole run, generation included.

A failed generation retries on the next timer run, at most DAYPIC_MAX_ATTEMPTS
per day: generation costs money, and a broken day has to end rather than burn
budget until midnight. A dry run builds and prints the prompt (one cheap chat
call) but never spends an image call, sends nothing and leaves no row.

Single-file, stdlib-only. Reuses the router client from evaluator.py and the
platform adapters and config from publisher.py.

Behavior: AGENTS/SPEC.md, section «Картина дня (daypic.py)».
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import sqlite3
import sys
import time
import urllib.error
from dataclasses import dataclass, replace
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import evaluator
import preparer   # _sniff_image_ext, MIN_IMAGE_BYTES
import publisher  # platform adapters, PublisherConfig, PreparedNews, read_pause
import runlog

log = logging.getLogger("posinus-daypic")

DAYPIC_VERSION = "0.1.0"
DAYPIC_ROUTER_USER = "daypic"   # external_user_id at the model router
MAX_PROMPT_ATTEMPTS = 2
DB_LOCK_RETRIES = 4

# The slots as the operator configured them. A missing table or an empty result
# is not an error: «Картина дня» is simply not set up on this database.
SLOTS_SQL = """
SELECT slot, enabled, title, generate_at, prompt, system_prompt, styles,
       chat_provider, chat_model, image_provider, image_model, image_size
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
    title TEXT,
    style TEXT,
    prompt TEXT,                    -- the final image prompt that was used
    file_path TEXT,
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

# The prompt scaffolding around the operator's slot prompt. The router has no
# web search, so the date discipline matters: the model must build the day from
# its own knowledge of the calendar and not drift to a neighbouring date.
PROMPT_REQUEST = (
    "Сегодня {date}, {weekday}. Используй только эту дату и события и праздники именно "
    "этого дня; не придумывай другую дату и не смещайся на соседние дни. "
    "День месяца: {day}.{style_line}\n\n{task}"
)
STYLE_LINE = " Базовый стиль картинки: {style}."
RU_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

# When the chat model would not answer, the issue still goes out on this
# template: a plainer picture is better than a missing day.
FALLBACK_PROMPT = (
    "Создай вертикальную «картину дня» для даты {date}. {style_line}"
    "Покажи спокойную, безопасную атмосферу этого дня: сезон, городской или праздничный "
    "контекст с приоритетом российских праздников, дружелюбное настроение, красивый свет "
    "и композицию открытки. Избегай насилия, трагедий, политики, катастроф и шок-контента. "
    "Без крупных надписей; только внизу небольшая плашка в стиле картинки с указанием "
    "по-русски, какие сегодня праздники."
)

# Appended on the retry after the image provider refused the first prompt —
# usually a safety refusal over something the chat model pulled from the news.
SAFE_SUFFIX = (
    "Сделай изображение безопасным для широкой аудитории: без насилия, катастроф, оружия, "
    "трагедий, политики, реальных конфликтов и пугающих сцен. Если события дня чувствительные, "
    "передай только нейтральное настроение дня, сезон и праздники."
)


class DaypicError(RuntimeError):
    """The picture could not be produced; the day retries on the next run."""


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
    image_size: str = "1024x1536"   # vertical, the ort_bot 9:16 heritage
    site_tags: str = "картина дня"

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
        cfg.site_tags = env.get("DAYPIC_SITE_TAGS", cfg.site_tags)
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
    image_provider: str = ""
    image_model: str = ""
    image_size: str = ""


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
            image_provider=(row["image_provider"] or "").strip(),
            image_model=(row["image_model"] or "").strip(),
            image_size=(row["image_size"] or "").strip(),
        ))
    return slots


# ------------------------------------------------------------ prompt stage


def pick_style(styles: tuple[str, ...], day_of_month: int) -> str:
    """The style for the day: the day-of-month entry, clamped to the list.

    The 31st runs into the last entry when the list is shorter — a repeat once
    a month is better than a special case."""
    if not styles:
        return ""
    return styles[min(max(day_of_month, 1), len(styles)) - 1]


def build_prompt_request(slot: Slot, now_local: datetime) -> str:
    style = pick_style(slot.styles, now_local.day)
    return PROMPT_REQUEST.format(
        date=now_local.strftime("%Y-%m-%d"),
        weekday=RU_WEEKDAYS[now_local.weekday()],
        day=now_local.day,
        style_line=STYLE_LINE.format(style=style) if style else "",
        task=slot.prompt,
    )


def fallback_prompt(slot: Slot, now_local: datetime) -> str:
    style = pick_style(slot.styles, now_local.day)
    return FALLBACK_PROMPT.format(
        date=now_local.strftime("%Y-%m-%d"),
        style_line=f"Стиль: {style}. " if style else "",
    )


def build_prompt(router_cfg: "evaluator.Config", slot: Slot, now_local: datetime) -> tuple[str, str]:
    """The image prompt for today, via the chat model; (prompt, model_id).

    Two attempts (the second asks for a shorter, simpler prompt), then the
    built-in fallback template: the issue is worth more than the prose.
    """
    cfg = replace(
        router_cfg,
        provider=slot.chat_provider or router_cfg.provider,
        model_id=slot.chat_model or router_cfg.model_id,
    )
    request = build_prompt_request(slot, now_local)
    messages = [
        {"role": "system", "content": slot.system_prompt},
        {"role": "user", "content": request},
    ]
    for attempt in range(1, MAX_PROMPT_ATTEMPTS + 1):
        try:
            reply = evaluator.chat(cfg, messages)
        except (evaluator.McpError, urllib.error.URLError, OSError) as exc:
            log.warning("slot %s: prompt attempt %d/%d failed: %s",
                        slot.slot, attempt, MAX_PROMPT_ATTEMPTS, exc)
            messages = [
                {"role": "system", "content": slot.system_prompt},
                {"role": "user", "content": f"{request}\n\nСделай промпт короче и проще, но сохрани суть."},
            ]
            continue
        prompt = (reply.get("text") or "").strip()
        if prompt:
            return prompt, reply.get("model_id") or cfg.model_id
        log.warning("slot %s: prompt attempt %d/%d returned empty text", slot.slot, attempt, MAX_PROMPT_ATTEMPTS)
    log.warning("slot %s: falling back to the built-in prompt template", slot.slot)
    return fallback_prompt(slot, now_local), "fallback-template"


# ------------------------------------------------------------- image stage


def generate_picture(
    cfg: DaypicConfig, router_cfg: "evaluator.Config", slot: Slot,
    prompt: str, day: str,
) -> tuple[str, str]:
    """Generate and save today's picture; (file path, image model id).

    One retry with the safety suffix: image providers refuse prompts that carry
    too much of the day's news, and a toned-down picture beats a missing one.
    """
    provider = slot.image_provider or cfg.image_provider
    model = slot.image_model or cfg.image_model
    size = slot.image_size or cfg.image_size
    reply = None
    for attempt, text in enumerate((prompt, f"{prompt}\n\n{SAFE_SUFFIX}"), start=1):
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
            log.warning("slot %s: image attempt %d/2 failed: %s", slot.slot, attempt, exc)
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
    target_dir = Path(cfg.daypic_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{day}-{slot.slot}{preparer._sniff_image_ext(data)}"
    path.write_bytes(data)
    model_id = reply.get("model_id") or model or provider
    log.info("slot %s: picture generated via %s (%d bytes) -> %s", slot.slot, model_id, len(data), path)
    return str(path), model_id


# ---------------------------------------------------------------- storage


def open_own_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    # Same WAL settings as the rest of the pipeline DB: the operator UI reads
    # this database and must never block the write after a post went out.
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA foreign_keys = ON")
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
    con: sqlite3.Connection, day: str, slot: Slot, style: str, prompt: str,
    file_path: str, prompt_model: str, image_model: str,
) -> None:
    def action() -> None:
        con.execute(
            "INSERT INTO daypic_item (day, slot, status, title, style, prompt, file_path, "
            "prompt_model_id, image_model_id, attempts, generated_at) "
            "VALUES (?, ?, 'generated', ?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(day, slot) DO UPDATE SET status='generated', title=excluded.title, "
            "style=excluded.style, prompt=excluded.prompt, file_path=excluded.file_path, "
            "prompt_model_id=excluded.prompt_model_id, image_model_id=excluded.image_model_id, "
            "attempts=daypic_item.attempts+1, generated_at=excluded.generated_at, error=NULL",
            (day, slot.slot, slot.title, style, prompt, file_path, prompt_model, image_model, _now_iso()),
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
    pub_cfg: "publisher.PublisherConfig", con: sqlite3.Connection,
    item_row: sqlite3.Row, platforms: list[str],
) -> bool:
    """Send one issue to every platform still pending; True when it settled.

    Same shape as the news publisher: a platform already 'ok' is skipped, a
    failing one retries up to max_attempts, and the issue finalizes best-effort
    with whatever platforms succeeded, so a broken platform cannot hold the
    picture of every following day."""
    item_id = item_row["id"]
    file_path = item_row["file_path"] or ""
    if not file_path or not Path(file_path).exists():
        log.error("daypic %s/%s: picture file %s is missing, nothing to publish",
                  item_row["day"], item_row["slot"], file_path)
        return False
    state = publication_state(con, item_id)
    pending = [p for p in platforms
               if state.get(p, ("", 0))[0] != "ok" and state.get(p, ("", 0))[1] < pub_cfg.max_attempts]
    item = publisher.PreparedNews(
        news_id=item_id,
        title=item_row["title"] or "Картина дня",
        paragraphs=[],
        lead_image=file_path,
        source_url="",
        source_name="",
        images=[(file_path, "")],
    )
    for platform in pending:
        try:
            url = publisher.ADAPTERS[platform](pub_cfg, item, False)
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
    platforms = [p for p in pub_cfg.enabled_platforms() if p != "wildcar_org"]

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
                style = pick_style(slot.styles, now_local.day)
                prompt, prompt_model = build_prompt(router_cfg, slot, now_local)
                if dry_run:
                    log.info("slot %s [dry-run]: style '%s', prompt via %s; no image call is spent",
                             slot.slot, style, prompt_model)
                    print(prompt)
                    continue
                try:
                    file_path, image_model = generate_picture(cfg, router_cfg, slot, prompt, day)
                except DaypicError as exc:
                    failed += 1
                    log.error("slot %s: %s", slot.slot, exc)
                    record_failure(own, day, slot, str(exc))
                    continue
                record_generated(own, day, slot, style, prompt, file_path, prompt_model, image_model)
                generated += 1
                row = get_item(own, day, slot.slot)

            if dry_run:
                continue
            if not platforms:
                log.warning("slot %s: no platform configured; the picture is saved but goes nowhere",
                            slot.slot)
                continue
            if publish_item(pub_cfg, own, row, platforms):
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
        "image_size": cfg.image_size,
        "max_attempts": cfg.max_attempts,
    }
    with runlog.record("daypic", cfg.own_db, settings) as counters:
        return run(cfg, router_cfg, dry_run=False, only_slot=args.slot,
                   ignore_time=args.ignore_time, counters=counters)


if __name__ == "__main__":
    sys.exit(main())
