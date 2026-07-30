#!/usr/bin/env python3
"""News preparer: turns selected news into a ready-to-publish HTML page.

For every news item that a selector marked positive («Отобрано») and that is not
prepared yet, this:

1. re-fetches the original article and pulls out illustrations with captions
   (<figure>/<figcaption>, lazy-loaded <img>, og:image), respecting robots;
2. asks the model for a fresh, lively Russian retelling (not a dry translation);
3. stores the retelling as a markdown document (the canonical text the publisher
   renders from) plus the downloaded images;
4. records it in the evaluator's OWN database and marks it «Подготовлено».

Single-file, stdlib-only. Reuses the MCP router client from evaluator.py. The
crawler's exchange contract forbids writing anything but the two exchange tables,
so all prepared artifacts (the markdown retelling, images, labels) live in the
pipeline's own database and media directory, keyed by news_id.

Behavior: AGENTS/SPEC.md, section «Подготовка отобранных новостей».
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import evaluator
import runlog  # reuse the MCP router client, Config, JSON extraction

log = logging.getLogger("posinus-preparer")

PREPARER_VERSION = "0.1.0"
PREPARER_ROUTER_USER = "news-preparer"  # external_user_id at the model router
MAX_SOURCE_CHARS = 6000
MAX_RETELL_ATTEMPTS = 2
MAX_IMAGES = 4
MIN_IMAGE_BYTES = 3000
MAX_IMAGE_BYTES = 12_000_000
# Anything heavier than IMAGE_RECODE_BYTES is resized and re-encoded before it
# is stored: Telegram refuses photos over 10 MB, and a page of multi-megabyte
# originals is what a reader on a slow line waits ten seconds for. 1600 px is
# wider than any platform renders.
IMAGE_RECODE_BYTES = 400_000
IMAGE_MAX_WIDTH = 1600
FFMPEG = "ffmpeg"
FFMPEG_TIMEOUT = 120.0
FETCH_TIMEOUT = 30.0

SELECTED_SQL = """
SELECT DISTINCT n.news_id, n.primary_url, n.title, n.body_text, n.language
FROM exchange_news_for_selection AS n
JOIN exchange_latest_reviews AS r ON r.news_id = n.news_id
WHERE r.decision = 'positive'
ORDER BY n.first_seen_at DESC
"""

OWN_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS prepared_item (
    news_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    retold_title TEXT,
    retold_body_md TEXT,
    model_id TEXT,
    prepared_at TEXT,
    published_at TEXT,
    error TEXT,
    edited_at TEXT,       -- set when the operator fixed the retelling by hand
    edited_by TEXT,
    images_purged_at TEXT, -- set by retention.py when the pictures were deleted
    expired_at TEXT       -- set by publisher.py when the item waited too long
);
CREATE TABLE IF NOT EXISTS illustration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id INTEGER NOT NULL REFERENCES prepared_item(news_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    caption TEXT,
    source_url TEXT,
    downloaded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_illustration_news ON illustration(news_id);
CREATE TABLE IF NOT EXISTS ignored_image (
    url_key TEXT PRIMARY KEY,  -- URL without query/fragment, as _image_key builds it
    note TEXT,
    added_at TEXT
);
"""

CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif",
}


# --------------------------------------------------------------- config


@dataclass
class PreparerConfig:
    news_db: str = "/var/lib/posinus/posinus.sqlite3"
    own_db: str = "/var/lib/posinus/pipeline/evaluator.sqlite3"
    media_dir: str = "/var/lib/posinus/pipeline/media"
    user_agent: str = "PositiveNewsEvaluator/0.1 (+mailto:mail@wildcar.ru)"
    fetch_delay: float = 1.0
    max_images: int = MAX_IMAGES
    # A news item that ends up with zero pictures gets one generated from the
    # retelling. Empty image_provider switches the feature off; empty
    # image_model lets the router pick within the provider.
    image_provider: str = "codex-oauth"
    image_model: str = ""
    # Downloaded pictures are shown to a vision model that weeds out what the
    # URL blacklist and the size filters cannot see: source logos, banners,
    # badges. Same off-switch convention as the pair above.
    image_check_provider: str = "codex-oauth"
    image_check_model: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] = os.environ) -> "PreparerConfig":
        cfg = cls()
        cfg.news_db = env.get("NEWS_DB_PATH", cfg.news_db)
        cfg.own_db = env.get("EVALUATOR_DB_PATH", cfg.own_db)
        cfg.media_dir = env.get("MEDIA_DIR", cfg.media_dir)
        cfg.user_agent = env.get("PREPARER_USER_AGENT", cfg.user_agent)
        cfg.image_provider = env.get("IMAGE_PROVIDER", cfg.image_provider)
        cfg.image_model = env.get("IMAGE_MODEL", cfg.image_model)
        cfg.image_check_provider = env.get("IMAGE_CHECK_PROVIDER", cfg.image_check_provider)
        cfg.image_check_model = env.get("IMAGE_CHECK_MODEL", cfg.image_check_model)
        return cfg


# --------------------------------------------------------- article fetch


_ROBOTS: dict[str, urllib.robotparser.RobotFileParser] = {}


def allowed_by_robots(url: str, user_agent: str) -> bool:
    """Respect robots.txt; allow when it cannot be read (article was already
    collected by the crawler, which honored robots at that time)."""
    parts = urllib.parse.urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    parser = _ROBOTS.get(root)
    if parser is None:
        parser = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(f"{root}/robots.txt", headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=15) as resp:
                parser.parse(resp.read(1_000_000).decode("utf-8", errors="replace").splitlines())
        except Exception:
            parser = None  # unreadable -> allow
        _ROBOTS[root] = parser
    return parser.can_fetch(user_agent, url) if parser else True


def fetch(url: str, user_agent: str, timeout: float = FETCH_TIMEOUT) -> tuple[str, str, bytes]:
    """Return (final_url, content_type, body). Raises on HTTP or transport error."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, identity"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(MAX_IMAGE_BYTES + 1)
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return resp.geturl(), resp.headers.get("Content-Type", ""), body


# ------------------------------------------------------- image extraction


class _ArticleImageParser(HTMLParser):
    """Collect candidate illustrations: og:image, <figure> images with their
    <figcaption>, and lazy-loaded <img> tags with their alt text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_image: str | None = None
        self.figures: list[dict[str, str]] = []
        self.loose: list[dict[str, str]] = []
        self._figure_depth = 0
        self._current: dict[str, str] | None = None
        self._in_caption = False
        self._caption_parts: list[str] = []

    @staticmethod
    def _img_src(attrs: dict[str, str]) -> str | None:
        for key in ("src", "data-src", "data-original", "data-lazy-src"):
            value = attrs.get(key)
            if value and not value.startswith("data:"):
                return value
        return None

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        if tag == "meta":
            prop = (attrs.get("property") or attrs.get("name") or "").lower()
            if prop in ("og:image", "twitter:image") and attrs.get("content") and not self.og_image:
                self.og_image = attrs["content"]
        elif tag == "figure":
            self._figure_depth += 1
            self._current = {"src": "", "alt": "", "caption": ""}
        elif tag == "img":
            src = self._img_src(attrs)
            if not src:
                return
            if self._figure_depth and self._current is not None and not self._current["src"]:
                self._current["src"] = src
                self._current["alt"] = attrs.get("alt", "")
            else:
                self.loose.append({"src": src, "alt": attrs.get("alt", ""), "caption": ""})
        elif tag == "figcaption" and self._figure_depth:
            self._in_caption = True
            self._caption_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "figcaption" and self._in_caption:
            self._in_caption = False
            if self._current is not None:
                self._current["caption"] = " ".join(" ".join(self._caption_parts).split())
        elif tag == "figure" and self._figure_depth:
            self._figure_depth -= 1
            if self._current and self._current["src"]:
                self.figures.append(self._current)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._in_caption:
            self._caption_parts.append(data)


def _image_key(url: str) -> str:
    """De-duplication key: the URL without query and fragment. og:image is
    usually the lead figure again, served with different sizing parameters
    (`?w=1200` vs `?w=800`), which used to make the first two saved pictures
    the same photograph."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def ignored_image_keys(con: sqlite3.Connection) -> frozenset[str]:
    """URL keys of images that must never be published, from `ignored_image`.

    Fed by `--ignore-image`; the typical entry is a source site's own logo,
    which the article parser keeps mistaking for an illustration."""
    return frozenset(row[0] for row in con.execute("SELECT url_key FROM ignored_image"))


def extract_illustrations(
    html_body: bytes, base_url: str, limit: int, ignored: frozenset[str] = frozenset()
) -> list[dict[str, str]]:
    """Ordered, de-duplicated illustration candidates: og:image, then figures
    (with captions), then loose images (caption from alt).

    Duplicates are folded by `_image_key`; when the kept copy has no caption
    and a duplicate does (og:image first, the captioned figure later), the
    caption is adopted so de-duplication never loses it.

    `ignored` (same keys) drops a candidate before anything else: it takes no
    slot in the limit and its caption never reaches the translation call."""
    parser = _ArticleImageParser()
    parser.feed(html_body.decode("utf-8", errors="replace"))
    candidates: list[dict[str, str]] = []
    if parser.og_image:
        candidates.append({"src": parser.og_image, "caption": ""})
    for figure in parser.figures:
        candidates.append({"src": figure["src"], "caption": figure["caption"] or figure["alt"]})
    for loose in parser.loose:
        candidates.append({"src": loose["src"], "caption": loose["alt"]})
    result: list[dict[str, str]] = []
    kept: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        absolute = urllib.parse.urljoin(base_url, candidate["src"])
        if not absolute.startswith(("http://", "https://")):
            continue
        key = _image_key(absolute)
        if key in ignored:
            continue
        if key in kept:
            if candidate["caption"] and not kept[key]["caption"]:
                kept[key]["caption"] = candidate["caption"]
            continue
        if len(result) >= limit:
            continue  # keep scanning: a duplicate may still donate its caption
        entry = {"url": absolute, "caption": candidate["caption"]}
        kept[key] = entry
        result.append(entry)
    return result


def shrink_image(path: Path) -> Path:
    """Re-encode a heavy picture as a JPEG no wider than IMAGE_MAX_WIDTH and
    return the (possibly renamed) path.

    The pipeline is stdlib-only, so this shells out to ffmpeg, which the host
    carries. Light files and GIFs (animation would be lost) pass through
    untouched. Lenient on failure: when ffmpeg is missing, errors out or does
    not actually make the file smaller, the original stays — a heavy picture
    beats a missing one, and Telegram's 10 MB refusal is survivable."""
    if path.suffix == ".gif" or path.stat().st_size <= IMAGE_RECODE_BYTES:
        return path
    tmp = path.with_name(path.stem + ".shrink.jpg")
    cmd = [
        FFMPEG, "-y", "-nostdin", "-loglevel", "error", "-i", str(path),
        "-frames:v", "1", "-map_metadata", "-1",
        "-vf", f"scale=min(iw\\,{IMAGE_MAX_WIDTH}):-2",
        "-q:v", "3", str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=FFMPEG_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", None) or b""
        log.warning("image %s: ffmpeg re-encode failed, keeping the original: %s %s",
                    path, exc, stderr.decode(errors="replace").strip())
        tmp.unlink(missing_ok=True)
        return path
    if not tmp.exists() or not (MIN_IMAGE_BYTES <= tmp.stat().st_size < path.stat().st_size):
        log.warning("image %s: re-encode produced nothing smaller, keeping the original", path)
        tmp.unlink(missing_ok=True)
        return path
    target = path.with_suffix(".jpg")
    os.replace(tmp, target)
    if target != path:
        path.unlink()
    log.info("image %s: re-encoded to %s (%d bytes)", path.name, target.name, target.stat().st_size)
    return target


def download_illustrations(
    cfg: PreparerConfig, news_id: int, candidates: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Download image bytes into media_dir/<news_id>/; skip icons and oversized files."""
    target_dir = Path(cfg.media_dir) / str(news_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, str]] = []
    seen_digests: set[str] = set()
    for candidate in candidates:
        url = candidate["url"]
        try:
            if not allowed_by_robots(url, cfg.user_agent):
                log.info("news %s: robots forbids image %s", news_id, url)
                continue
            time.sleep(cfg.fetch_delay)
            _, content_type, body = fetch(url, cfg.user_agent)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("news %s: image download failed %s: %s", news_id, url, exc)
            continue
        media_type = content_type.split(";")[0].strip().lower()
        if not media_type.startswith("image/") or not (MIN_IMAGE_BYTES <= len(body) <= MAX_IMAGE_BYTES):
            continue
        # Distinct URLs can still serve byte-identical files (CDN aliases);
        # a second copy of the same picture is never worth publishing.
        digest = hashlib.sha256(body).hexdigest()
        if digest in seen_digests:
            log.info("news %s: image %s duplicates an already saved one, skipped", news_id, url)
            continue
        seen_digests.add(digest)
        position = len(saved) + 1
        filename = f"{position}{CONTENT_TYPE_EXT.get(media_type, '.img')}"
        path = target_dir / filename
        path.write_bytes(body)
        path = shrink_image(path)
        saved.append({"path": str(path), "caption": candidate["caption"], "source_url": url})
    return saved


# ------------------------------------------------- illustration vision check


IMAGE_CHECK_PROMPT = (
    "На изображении - кандидат в иллюстрации к новости «{title}».\n"
    "Реши, годится ли оно как иллюстрация: нужна фотография или содержательная "
    "картинка по теме. Не годится служебная и оформительская графика: логотип, "
    "баннер, плашка, кнопка, значок, реклама, водяной знак, заглушка, шапка "
    "сайта, картинка из одного текста.\n"
    "Верни один JSON-объект и больше ничего: "
    '{{"verdict": "keep" | "drop", "reason": "<коротко почему>"}}'
)
# Bigger files are not sent to the model and stay unchecked: junk graphics are
# small, a file this heavy is almost certainly a real photograph, and a
# multi-megabyte data URI is what a provider chokes on. (The resets that once
# looked like a 4 MB transport cap were the /mcp 307 redirect issued without
# reading the request body — gone since router_url carries the trailing slash.)
IMAGE_CHECK_MAX_BYTES = 8_000_000

EXT_MIME = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
            ".gif": "image/gif", ".avif": "image/avif"}


def review_illustrations(
    cfg: PreparerConfig, router_cfg: "evaluator.Config", news_id: int,
    title: str, images: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Show every downloaded picture to a vision model and drop the junk.

    The `ignored_image` blacklist catches a source's logo only the second time,
    after an operator has seen it published; this catches it the first time, by
    looking. One `chat` call per picture (files differ in MIME type, and the
    tool takes one `image_mime` per call). Lenient like every image extra here:
    a router failure or an unusable reply keeps the picture — the check can only
    make things better than before it existed. Only an explicit "drop" verdict
    removes a picture, together with its file."""
    kept: list[dict[str, str]] = []
    for image in images:
        path = Path(image["path"])
        mime = EXT_MIME.get(path.suffix)
        try:
            data = path.read_bytes()
        except OSError as exc:
            log.warning("news %s: cannot read %s for the vision check: %s", news_id, path, exc)
            kept.append(image)
            continue
        if mime is None or len(data) > IMAGE_CHECK_MAX_BYTES:
            kept.append(image)
            continue
        arguments: dict[str, Any] = {
            "external_user_id": router_cfg.router_user or router_cfg.selector_name,
            "text": IMAGE_CHECK_PROMPT.format(title=title),
            "images_b64": [base64.b64encode(data).decode()],
            "image_mime": mime,
            "params": {"reasoning_effort": "low"},
        }
        if cfg.image_check_provider:
            arguments["provider"] = cfg.image_check_provider
        if cfg.image_check_model:
            arguments["model_id"] = cfg.image_check_model
        try:
            reply = evaluator.call_tool(router_cfg.router_url, "chat", arguments,
                                        token=router_cfg.router_token or None)
            text = reply.get("text") if isinstance(reply, dict) else None
            payload = evaluator.extract_json_object(text or "")
        except (evaluator.McpError, evaluator.EvaluationInvalid,
                urllib.error.URLError, OSError) as exc:
            log.warning("news %s: vision check failed for %s, keeping it: %s",
                        news_id, image["source_url"], exc)
            kept.append(image)
            continue
        if payload.get("verdict") == "drop":
            log.info("news %s: vision check dropped %s: %s",
                     news_id, image["source_url"], payload.get("reason", ""))
            path.unlink(missing_ok=True)
            continue
        kept.append(image)
    return kept


def review_prepared_images(cfg: PreparerConfig, router_cfg: "evaluator.Config",
                           counters: dict | None = None) -> int:
    """Vision-check the pictures of items prepared before the check existed.

    Sweeps illustrations of prepared, not yet published, not operator-edited
    items — the same protection rules as --ignore-image: what went out went
    out, and a human's choice of pictures is not re-reviewed by a machine.
    Generated pictures are skipped too: the pipeline made them itself, from a
    prompt that already forbids text and logos. Dropped pictures lose their
    row and their file.

    Afterwards, any queued item without a single picture — emptied by this
    sweep, or prepared back when generation was off or failing — gets one
    generated from its stored retelling, the same bonus a zero-picture item
    gets at preparation time."""
    con = open_own_db(cfg.own_db)
    try:
        rows = con.execute(
            "SELECT i.id, i.news_id, i.file_path, i.caption, i.source_url, p.retold_title "
            "FROM illustration i JOIN prepared_item p ON p.news_id = i.news_id "
            "WHERE p.status = 'prepared' AND p.edited_at IS NULL "
            "AND i.source_url NOT LIKE 'generated://%' ORDER BY i.news_id, i.position"
        ).fetchall()
        by_news: dict[int, list[dict[str, str]]] = {}
        titles: dict[int, str] = {}
        for row in rows:
            path = Path(row["file_path"])
            if not path.exists():  # the media tree may have moved; same fallback as the publisher
                path = Path(cfg.media_dir) / str(row["news_id"]) / path.name
            by_news.setdefault(row["news_id"], []).append(
                {"id": str(row["id"]), "path": str(path),
                 "caption": row["caption"] or "", "source_url": row["source_url"] or ""})
            titles[row["news_id"]] = row["retold_title"] or ""
        checked, dropped = 0, 0
        for news_id, images in by_news.items():
            kept = review_illustrations(cfg, router_cfg, news_id, titles[news_id], images)
            checked += len(images)
            kept_ids = {image["id"] for image in kept}
            for image in images:
                if image["id"] not in kept_ids:
                    con.execute("DELETE FROM illustration WHERE id = ?", (int(image["id"]),))
                    dropped += 1
            con.commit()
        generated = 0
        if cfg.image_provider:
            empties = con.execute(
                "SELECT p.news_id, p.retold_title, p.retold_body_md FROM prepared_item p "
                "LEFT JOIN illustration i ON i.news_id = p.news_id "
                "WHERE p.status = 'prepared' AND p.edited_at IS NULL "
                "AND p.retold_body_md IS NOT NULL GROUP BY p.news_id HAVING count(i.id) = 0"
            ).fetchall()
            for row in empties:
                paragraphs = [block for block in (row["retold_body_md"] or "").split("\n\n")
                              if block.strip() and not block.startswith(("# ", "Источник:"))]
                entry = generate_illustration(cfg, router_cfg, row["news_id"],
                                              row["retold_title"] or "", paragraphs)
                if entry is not None:
                    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    con.execute(
                        "INSERT INTO illustration (news_id, position, file_path, caption, "
                        "source_url, downloaded_at) VALUES (?, 1, ?, ?, ?, ?)",
                        (row["news_id"], entry["path"], entry["caption"], entry["source_url"], now))
                    generated += 1
            con.commit()
        log.info("review finished: %d pictures of %d prepared items checked, %d dropped, "
                 "%d generated for pictureless items", checked, len(by_news), dropped, generated)
        if counters is not None:
            counters.update(checked=checked, dropped=dropped, generated=generated)
        return 0
    finally:
        con.close()


# ---------------------------------------------------- generated illustration


IMAGE_PROMPT = (
    "Фотореалистичная горизонтальная иллюстрация к новости. Без текста, надписей, "
    "логотипов и водяных знаков на изображении.\n"
    "Новость: {title}\n{lead}"
)
IMAGE_PROMPT_LEAD_CHARS = 600

_IMAGE_MAGIC = ((b"\x89PNG", ".png"), (b"\xff\xd8", ".jpg"), (b"RIFF", ".webp"), (b"GIF8", ".gif"))


def _sniff_image_ext(data: bytes) -> str:
    for magic, ext in _IMAGE_MAGIC:
        if data.startswith(magic):
            return ext
    return ".img"


def build_image_prompt(title: str, paragraphs: list[str]) -> str:
    lead = " ".join(paragraphs[:2])[:IMAGE_PROMPT_LEAD_CHARS]
    return IMAGE_PROMPT.format(title=title, lead=lead)


def generate_illustration(
    cfg: PreparerConfig, router_cfg: "evaluator.Config", news_id: int,
    title: str, paragraphs: list[str],
) -> dict[str, str] | None:
    """One generated picture for a news item that ended up with none.

    Calls the router's `generate_image` tool (provider `image_provider`, the
    same `news-preparer` identity as the retelling). Lenient like the caption
    translation: any failure logs and returns None, and the item publishes
    without a picture as it always did — the picture is a bonus, not a gate.
    The entry's source_url scheme `generated://` marks the provenance."""
    arguments: dict[str, Any] = {
        "external_user_id": router_cfg.router_user or router_cfg.selector_name,
        "prompt": build_image_prompt(title, paragraphs),
    }
    if cfg.image_provider:
        arguments["provider"] = cfg.image_provider
    if cfg.image_model:
        arguments["model_id"] = cfg.image_model
    try:
        reply = evaluator.call_tool(router_cfg.router_url, "generate_image", arguments,
                                    token=router_cfg.router_token or None)
    except (evaluator.McpError, urllib.error.URLError, OSError) as exc:
        log.warning("news %s: image generation failed: %s", news_id, exc)
        return None
    blobs = reply.get("image_b64") if isinstance(reply, dict) else None
    if not isinstance(blobs, list) or not blobs:
        log.warning("news %s: image generation returned no image", news_id)
        return None
    try:
        data = base64.b64decode(blobs[0])
    except (TypeError, ValueError) as exc:
        log.warning("news %s: generated image is not decodable base64: %s", news_id, exc)
        return None
    if len(data) < MIN_IMAGE_BYTES:
        log.warning("news %s: generated image is implausibly small (%d bytes), dropped", news_id, len(data))
        return None
    target_dir = Path(cfg.media_dir) / str(news_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"1{_sniff_image_ext(data)}"
    path.write_bytes(data)
    path = shrink_image(path)
    model_id = reply.get("model_id") or cfg.image_model or cfg.image_provider
    log.info("news %s: illustration generated via %s (%d bytes)", news_id, model_id, len(data))
    return {"path": str(path), "caption": "", "source_url": f"generated://{model_id}"}


# ------------------------------------------------------------- retelling


RETELL_SYSTEM = (
    "Ты редактор ленты добрых новостей. Перескажи новость на русском живо и по-человечески, "
    "чтобы читать было интересно.\n"
    "Правила.\n"
    "- Пересказывай факты из текста, ничего не выдумывай. Если новость на другом языке, "
    "перескажи её по-русски.\n"
    "- Убирай канцелярит, штампы и сухость. Пиши короткими и длинными предложениями вперемешку.\n"
    "- Не используй обороты «не только... но и», «не просто... а». "
    "Не используй знаки сравнения и математические знаки в тексте.\n"
    "- Заголовок короткий и цепляющий, без кликбейта. Тело от двух до четырёх абзацев.\n"
    "- Если в сообщении есть блок «Подписи к иллюстрациям», переведи каждую подпись на русский "
    "в том же порядке: коротко, без точки в конце, без выдумок.\n"
    "Формат ответа. Верни один JSON-объект и больше ничего: "
    '{"title": "<заголовок>", "body": ["<абзац>", "<абзац>", ...]}. '
    'Если были подписи, добавь поле "captions": ["<подпись>", ...] - ровно столько же, '
    "сколько было в блоке, в том же порядке."
)


def build_retell_user_message(title: str, body: str, language: str, captions: list[str] | None = None) -> str:
    source = (body or "").strip()
    if len(source) > MAX_SOURCE_CHARS:
        source = source[:MAX_SOURCE_CHARS] + "\n(текст обрезан)"
    lang = f"Язык оригинала: {language}.\n" if language else ""
    message = f"{lang}Заголовок оригинала: {(title or '').strip()}\nТекст оригинала:\n{source}"
    if captions:
        listed = "\n".join(f"{i + 1}. {caption}" for i, caption in enumerate(captions))
        message += f"\n\nПодписи к иллюстрациям:\n{listed}"
    return message


def parse_retelling(payload: dict[str, Any]) -> tuple[str, list[str]]:
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise evaluator.EvaluationInvalid("нет заголовка title")
    body = payload.get("body")
    if isinstance(body, list):
        paragraphs = [str(part).strip() for part in body if str(part).strip()]
    elif isinstance(body, str):
        paragraphs = [p.strip() for p in body.replace("\r", "").split("\n\n") if p.strip()]
        paragraphs = paragraphs or [line.strip() for line in body.split("\n") if line.strip()]
    else:
        raise evaluator.EvaluationInvalid("нет тела body")
    if not paragraphs:
        raise evaluator.EvaluationInvalid("пустое тело body")
    return " ".join(title.split()), paragraphs


def parse_captions(payload: dict[str, Any], expected: int) -> list[str] | None:
    """The translated captions, or None when the reply is unusable.

    Lenient on purpose, like the rubric: a bad captions array must not fail a
    retelling that is otherwise fine, let alone cost a second paid call — the
    original captions are simply kept."""
    if expected <= 0:
        return None
    captions = payload.get("captions")
    if not isinstance(captions, list) or len(captions) != expected \
            or not all(isinstance(c, str) for c in captions):
        return None
    return [" ".join(c.split()) for c in captions]


def retell(
    router_cfg: "evaluator.Config", news: sqlite3.Row, captions: list[str] | None = None
) -> tuple[str, list[str], list[str] | None, str]:
    """Ask the model for a Russian retelling; one retry on invalid JSON.

    `captions` are the original (usually English) illustration captions; they
    ride along in the same call and come back translated as the third element,
    or None when the model did not cooperate."""
    messages = [
        {"role": "system", "content": RETELL_SYSTEM},
        {"role": "user", "content": build_retell_user_message(
            news["title"], news["body_text"], news["language"], captions)},
    ]
    last_error = "модель не отвечала"
    for attempt in range(1, MAX_RETELL_ATTEMPTS + 1):
        reply = evaluator.chat(router_cfg, messages)
        text = reply["text"]
        try:
            payload = evaluator.extract_json_object(text)
            title, paragraphs = parse_retelling(payload)
        except evaluator.EvaluationInvalid as exc:
            last_error = str(exc)
            log.warning("news %s: retelling attempt %d/%d rejected: %s", news["news_id"], attempt, MAX_RETELL_ATTEMPTS, last_error)
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": f"Ответ не прошёл проверку: {last_error}. Пришли исправленный JSON той же схемы."})
            continue
        captions_ru = parse_captions(payload, len(captions or []))
        if captions and captions_ru is None:
            log.warning("news %s: captions missing or mismatched in the reply, keeping the originals", news["news_id"])
        return title, paragraphs, captions_ru, reply.get("model_id") or router_cfg.model_id
    raise evaluator.EvaluationInvalid(last_error)


# ------------------------------------------------------------ markdown


def source_name_from_url(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc
    return host[4:] if host.startswith("www.") else host


def build_markdown(title: str, paragraphs: list[str], source_url: str, source_name: str) -> str:
    """Serialize the retelling as a self-contained markdown document.

    H1 title, blank-line-separated paragraphs, a source link. Images are NOT
    embedded here: they live in the `illustration` table with their files. This
    markdown is the canonical stored form; every platform renders from it, so
    there is no HTML round-trip and the text stays hand-editable."""
    parts = [f"# {title}"]
    parts.extend(paragraphs)
    if source_url:
        parts.append(f"Источник: [{source_name or source_name_from_url(source_url)}]({source_url})")
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------- storage


def _html_paragraphs(body: str) -> list[str]:
    """Paragraphs from a legacy HTML body (each was one <p>escaped-text</p>)."""
    return [text for text in (html.unescape(p).strip()
            for p in re.findall(r"<p>(.*?)</p>", body or "", re.DOTALL)) if text]


def _html_source_url(body: str) -> str:
    # build_page stored the href HTML-escaped (& -> &amp;), so unescape it back.
    match = re.search(r'<footer>Источник:\s*<a href="([^"]+)"', body or "")
    return html.unescape(match.group(1)) if match else ""


def migrate_own_db(con: sqlite3.Connection) -> None:
    """Bring an older own DB forward: add retold_body_md and backfill it from the
    HTML that used to be stored (paragraphs + source), so nothing is re-run.

    Also adds the operator-edit columns. They matter beyond bookkeeping: once a
    human has fixed a retelling, regenerating it would silently throw the fix
    away on the first failure and re-queue, so `edited_at` is what stops that.
    """
    # Every service adds it, not just retention.py: the operator UI reads this
    # column, and a database that has not seen a retention run yet must not make
    # the card say «нет связи с базой конвейера».
    columns = {row["name"] for row in con.execute("PRAGMA table_info(prepared_item)")}
    if columns and "images_purged_at" not in columns:
        con.execute("ALTER TABLE prepared_item ADD COLUMN images_purged_at TEXT")
        con.commit()
    columns = {row["name"] for row in con.execute("PRAGMA table_info(prepared_item)")}
    if columns and "expired_at" not in columns:
        con.execute("ALTER TABLE prepared_item ADD COLUMN expired_at TEXT")
        con.commit()
    columns = {row["name"] for row in con.execute("PRAGMA table_info(prepared_item)")}
    if columns and "edited_at" not in columns:
        con.execute("ALTER TABLE prepared_item ADD COLUMN edited_at TEXT")
        con.execute("ALTER TABLE prepared_item ADD COLUMN edited_by TEXT")
        con.commit()
    columns = {row["name"] for row in con.execute("PRAGMA table_info(prepared_item)")}
    if not columns or "retold_body_md" in columns:
        return
    con.execute("ALTER TABLE prepared_item ADD COLUMN retold_body_md TEXT")
    if "retold_body_html" in columns:
        for row in con.execute(
            "SELECT news_id, retold_title, retold_body_html FROM prepared_item "
            "WHERE retold_body_html IS NOT NULL"
        ).fetchall():
            source_url = _html_source_url(row["retold_body_html"])
            markdown = build_markdown(
                row["retold_title"] or "", _html_paragraphs(row["retold_body_html"]),
                source_url, source_name_from_url(source_url) if source_url else "",
            )
            con.execute("UPDATE prepared_item SET retold_body_md = ? WHERE news_id = ?",
                        (markdown, row["news_id"]))
    con.commit()


def open_own_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    # WAL keeps a reader (the operator UI) from blocking our commits; it is stored
    # in the file header, so setting it here is enough for every client.
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(OWN_SCHEMA_SQL)
    migrate_own_db(con)
    return con


def prepared_ids(con: sqlite3.Connection) -> set[int]:
    """News the preparer must not touch again.

    Everything the pipeline has already finished with — prepared, published,
    taken off the queue — plus anything a human has edited by hand.

    Only `status = 'prepared'` was checked here from the first version, and that
    was wrong in a way nothing noticed for three days: a published item is not
    «prepared», so it came back into the queue, was retold again at the price of
    a model call, and went back to `prepared`. On 2026-07-26 that was 215
    preparations in a day over 129 news items, and ten published posts had their
    publication date rewritten to today. The platforms were saved only by the
    `publication` rows: every one of them already said `ok`, so nothing was
    posted twice.

    The edited half matters for its own reason: a retelling the operator fixed
    would be silently regenerated after any later failure, and the fix would
    vanish without anyone noticing until the wrong text was public.

    `error` is the one status left retryable — that is what it is for.
    """
    return {
        row[0]
        for row in con.execute(
            "SELECT news_id FROM prepared_item WHERE status <> 'error' OR edited_at IS NOT NULL"
        )
    }


def save_prepared(
    con: sqlite3.Connection, news_id: int, title: str, body_md: str,
    model_id: str, images: list[dict[str, str]],
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with con:
        con.execute("DELETE FROM illustration WHERE news_id = ?", (news_id,))
        con.execute(
            "INSERT INTO prepared_item (news_id, status, retold_title, retold_body_md, model_id, prepared_at) "
            "VALUES (?, 'prepared', ?, ?, ?, ?) "
            "ON CONFLICT(news_id) DO UPDATE SET status='prepared', retold_title=excluded.retold_title, "
            "retold_body_md=excluded.retold_body_md, model_id=excluded.model_id, "
            "prepared_at=excluded.prepared_at, error=NULL",
            (news_id, title, body_md, model_id, now),
        )
        con.executemany(
            "INSERT INTO illustration (news_id, position, file_path, caption, source_url, downloaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(news_id, i + 1, img["path"], img["caption"], img["source_url"], now) for i, img in enumerate(images)],
        )


def record_error(con: sqlite3.Connection, news_id: int, message: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with con:
        con.execute(
            "INSERT INTO prepared_item (news_id, status, prepared_at, error) VALUES (?, 'error', ?, ?) "
            "ON CONFLICT(news_id) DO UPDATE SET status='error', prepared_at=excluded.prepared_at, error=excluded.error",
            (news_id, now, message[:1000]),
        )


# ---------------------------------------------------------------- pipeline


def prepare_one(cfg: PreparerConfig, router_cfg: "evaluator.Config", news: sqlite3.Row, dry_run: bool,
                ignored: frozenset[str] = frozenset()) -> dict[str, Any]:
    # The article is fetched BEFORE the model call: the illustration captions
    # go into the same retelling request and come back translated, so the
    # pictures are not signed in English under a Russian text — and it costs
    # no second call.
    candidates: list[dict[str, str]] = []
    if news["primary_url"]:
        try:
            if allowed_by_robots(news["primary_url"], cfg.user_agent):
                time.sleep(cfg.fetch_delay)
                final_url, _, body = fetch(news["primary_url"], cfg.user_agent)
                candidates = extract_illustrations(body, final_url, cfg.max_images, ignored)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("news %s: article fetch failed: %s", news["news_id"], exc)

    captions_in = [c["caption"] for c in candidates if c["caption"].strip()]
    title, paragraphs, captions_ru, model_id = retell(router_cfg, news, captions_in)
    if captions_ru is not None:
        translated = iter(captions_ru)
        for candidate in candidates:
            if candidate["caption"].strip():
                candidate["caption"] = next(translated)

    generated = False
    dropped = 0
    if not dry_run:
        images = download_illustrations(cfg, news["news_id"], candidates)
        if images and cfg.image_check_provider:
            reviewed = review_illustrations(cfg, router_cfg, news["news_id"], title, images)
            dropped = len(images) - len(reviewed)
            images = reviewed
        if not images and cfg.image_provider:
            entry = generate_illustration(cfg, router_cfg, news["news_id"], title, paragraphs)
            if entry is not None:
                images = [entry]
                generated = True
    else:
        images = [{"path": f"(dry-run) {c['url']}", "caption": c["caption"], "source_url": c["url"]} for c in candidates]
        if not candidates and cfg.image_provider:  # a dry run must not spend an image call
            log.info("news %s [dry-run]: no pictures, one would be generated via %s",
                     news["news_id"], cfg.image_provider)
    source_url = news["primary_url"] or ""
    body_md = build_markdown(title, paragraphs, source_url, source_name_from_url(source_url) if source_url else "")
    return {"title": title, "paragraphs": paragraphs, "model_id": model_id, "images": images,
            "body_md": body_md, "generated": generated, "images_dropped": dropped}


def add_ignored_image(cfg: PreparerConfig, url: str, note: str, counters: dict | None = None) -> int:
    """Register an image as never-to-be-published and pull it out of the queue.

    The entry matches by URL-without-query, the same key the candidate
    de-duplication uses. Illustration rows of not-yet-published items are
    deleted together with their files; published items keep theirs (what went
    out went out), and operator-edited items are only reported — a human
    already decided what those pictures should be.
    """
    if not url.startswith(("http://", "https://")):
        log.error("--ignore-image needs an http(s) URL, got %r", url)
        return 2
    key = _image_key(url)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = open_own_db(cfg.own_db)
    purged = 0
    try:
        with con:
            con.execute(
                "INSERT INTO ignored_image (url_key, note, added_at) VALUES (?, ?, ?) "
                "ON CONFLICT(url_key) DO UPDATE SET note = excluded.note",
                (key, note, now),
            )
            rows = con.execute(
                "SELECT i.id, i.news_id, i.file_path, i.source_url, p.status, p.edited_at "
                "FROM illustration i JOIN prepared_item p ON p.news_id = i.news_id"
            ).fetchall()
            for row in rows:
                if _image_key(row["source_url"] or "") != key:
                    continue
                if row["status"] != "prepared" or row["edited_at"]:
                    log.info("news %s (%s%s): keeping the already-public/edited copy",
                             row["news_id"], row["status"], ", edited" if row["edited_at"] else "")
                    continue
                con.execute("DELETE FROM illustration WHERE id = ?", (row["id"],))
                path = Path(row["file_path"])
                if not path.exists():  # the media tree may have moved; same fallback as the publisher
                    path = Path(cfg.media_dir) / str(row["news_id"]) / path.name
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("news %s: cannot delete %s: %s", row["news_id"], path, exc)
                purged += 1
                log.info("news %s: illustration %s removed from the queue copy", row["news_id"], row["source_url"])
    finally:
        con.close()
    log.info("ignored %s (%d queued cop%s purged)", key, purged, "y" if purged == 1 else "ies")
    if counters is not None:
        counters.update(purged=purged)
    return 0


def run(cfg: PreparerConfig, router_cfg: "evaluator.Config", limit: int, dry_run: bool, only: int | None,
        counters: dict | None = None) -> int:
    news_con = evaluator.open_db(cfg.news_db)
    own_con = open_own_db(cfg.own_db)
    try:
        selected = news_con.execute(SELECTED_SQL).fetchall()
        done = prepared_ids(own_con)
        ignored = ignored_image_keys(own_con)
        if only is not None:
            queue = [n for n in selected if n["news_id"] == only]
        else:
            queue = [n for n in selected if n["news_id"] not in done][:limit]
        log.info("selected %d, prepared %d, queue %d (limit %d)", len(selected), len(done), len(queue), limit)

        prepared, failed, generated, dropped = 0, 0, 0, 0
        for news in queue:
            try:
                result = prepare_one(cfg, router_cfg, news, dry_run, ignored)
            except (evaluator.EvaluationInvalid, evaluator.McpError, urllib.error.URLError) as exc:
                failed += 1
                log.error("news %s: preparation failed: %s", news["news_id"], exc)
                if not dry_run:
                    record_error(own_con, news["news_id"], str(exc))
                continue
            if dry_run:
                log.info("news %s [dry-run]: '%s', %d paragraphs, %d images",
                         news["news_id"], result["title"], len(result["paragraphs"]), len(result["images"]))
                print(result["body_md"])
            else:
                save_prepared(own_con, news["news_id"], result["title"], result["body_md"],
                              result["model_id"], result["images"])
                log.info("news %s: prepared '%s' (%d images)",
                         news["news_id"], result["title"], len(result["images"]))
            prepared += 1
            generated += 1 if result.get("generated") else 0
            dropped += result.get("images_dropped", 0)
        log.info("finished: %d prepared (%d with a generated picture, %d pictures dropped by the vision check), %d failed",
                 prepared, generated, dropped, failed)
        if counters is not None:
            counters.update(queue=len(selected), prepared=prepared, failed=failed,
                            images_generated=generated, images_dropped=dropped)
        return 0 if failed == 0 else 1
    finally:
        news_con.close()
        own_con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare selected news into markdown retellings.")
    parser.add_argument("--limit", type=int, default=5, help="batch size (default 5)")
    parser.add_argument("--news-id", type=int, default=None, help="prepare only this news id")
    parser.add_argument("--dry-run", action="store_true", help="fetch, retell and render, but write nothing")
    parser.add_argument("--ignore-image", metavar="URL", default=None,
                        help="blacklist this image (by URL without query), purge it from "
                             "unpublished prepared items, and exit")
    parser.add_argument("--note", default="", help="short reason stored with --ignore-image")
    parser.add_argument("--review-images", action="store_true",
                        help="vision-check the pictures of prepared, unpublished, unedited "
                             "items and drop the junk, then exit")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cfg = PreparerConfig.from_env()
    if args.ignore_image:  # an operator action: no router, no batch
        with runlog.record("ignore-image", cfg.own_db, {"url": args.ignore_image}) as counters:
            return add_ignored_image(cfg, args.ignore_image, args.note, counters)
    router_cfg = evaluator.Config.from_env()
    # A higher temperature than scoring: the retelling should read as prose. The token
    # budget has to cover the model's reasoning as well as the answer — see the note on
    # EVALUATOR_MAX_TOKENS in evaluator.Config.
    router_cfg.params = {
        "temperature": float(os.environ.get("PREPARER_TEMPERATURE", "0.7")),
        "max_tokens": int(os.environ.get("PREPARER_MAX_TOKENS", "4000")),
    }
    # Retelling is not scoring: the router must see a second caller, or the whole
    # pipeline's spend lands on one id and `news-evaluator` reads as the source of
    # tokens it never spent. selector_name stays what it is - it is a DB contract.
    router_cfg.router_user = os.environ.get("PREPARER_ROUTER_USER_ID", PREPARER_ROUTER_USER)
    if not router_cfg.router_token:
        log.error("ROUTER_AUTH_TOKEN is not set")
        return 2
    if args.review_images:  # an operator action, like --ignore-image, but with the model
        if not cfg.image_check_provider:
            log.error("IMAGE_CHECK_PROVIDER is empty: the vision check is switched off")
            return 2
        with runlog.record("review-images", cfg.own_db,
                           {"image_check_provider": cfg.image_check_provider}) as counters:
            return review_prepared_images(cfg, router_cfg, counters)
    if args.dry_run:  # a dry run is not something the machine did; it leaves no row
        return run(cfg, router_cfg, limit=args.limit, dry_run=True, only=args.news_id)
    settings = {
        "model": router_cfg.model_id,
        "provider": router_cfg.provider,
        "batch": args.limit,
        "media_dir": cfg.media_dir,
        "image_provider": cfg.image_provider,
        "image_check_provider": cfg.image_check_provider,
    }
    with runlog.record("preparer", cfg.own_db, settings) as counters:
        return run(cfg, router_cfg, limit=args.limit, dry_run=False, only=args.news_id, counters=counters)


if __name__ == "__main__":
    sys.exit(main())
