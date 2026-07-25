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
so all prepared artifacts (pages, images, labels) live in the evaluator's own
database and media directory, keyed by news_id.

Behavior: AGENTS/SPEC.md, section «Подготовка отобранных новостей».
"""

from __future__ import annotations

import argparse
import gzip
import html
import logging
import os
import re
import sqlite3
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

import evaluator  # reuse the MCP router client, Config, JSON extraction

log = logging.getLogger("news-preparer")

PREPARER_VERSION = "0.1.0"
MAX_SOURCE_CHARS = 6000
MAX_RETELL_ATTEMPTS = 2
MAX_IMAGES = 4
MIN_IMAGE_BYTES = 3000
MAX_IMAGE_BYTES = 12_000_000
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
    error TEXT
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
"""

CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif",
}


# --------------------------------------------------------------- config


@dataclass
class PreparerConfig:
    news_db: str = "/var/lib/newscrawler/newscrawler.sqlite3"
    own_db: str = "/var/lib/news-evaluator/evaluator.sqlite3"
    media_dir: str = "/var/lib/news-evaluator/media"
    user_agent: str = "PositiveNewsEvaluator/0.1 (+mailto:mail@wildcar.ru)"
    fetch_delay: float = 1.0
    max_images: int = MAX_IMAGES

    @classmethod
    def from_env(cls, env: dict[str, str] = os.environ) -> "PreparerConfig":
        cfg = cls()
        cfg.news_db = env.get("NEWS_DB_PATH", cfg.news_db)
        cfg.own_db = env.get("EVALUATOR_DB_PATH", cfg.own_db)
        cfg.media_dir = env.get("MEDIA_DIR", cfg.media_dir)
        cfg.user_agent = env.get("PREPARER_USER_AGENT", cfg.user_agent)
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


def extract_illustrations(html_body: bytes, base_url: str, limit: int) -> list[dict[str, str]]:
    """Ordered, de-duplicated illustration candidates: og:image, then figures
    (with captions), then loose images (caption from alt)."""
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
    seen: set[str] = set()
    for candidate in candidates:
        absolute = urllib.parse.urljoin(base_url, candidate["src"])
        if absolute in seen or not absolute.startswith(("http://", "https://")):
            continue
        seen.add(absolute)
        result.append({"url": absolute, "caption": candidate["caption"]})
        if len(result) >= limit:
            break
    return result


def download_illustrations(
    cfg: PreparerConfig, news_id: int, candidates: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Download image bytes into media_dir/<news_id>/; skip icons and oversized files."""
    target_dir = Path(cfg.media_dir) / str(news_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, str]] = []
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
        position = len(saved) + 1
        filename = f"{position}{CONTENT_TYPE_EXT.get(media_type, '.img')}"
        path = target_dir / filename
        path.write_bytes(body)
        saved.append({"path": str(path), "caption": candidate["caption"], "source_url": url})
    return saved


# ------------------------------------------------------------- retelling


RETELL_SYSTEM = (
    "Ты редактор ленты добрых новостей. Перескажи новость на русском живо и по-человечески, "
    "чтобы читать было интересно.\n"
    "Правила.\n"
    "- Пересказывай факты из текста, ничего не выдумывай. Если новость на другом языке, "
    "перескажи её по-русски.\n"
    "- Убирай канцелярит, штампы и сухость. Пиши короткими и длинными предложениями вперемешку.\n"
    "- Не используй обороты «не только... но и», «не просто... а». Не используй длинное тире, "
    "ставь обычный дефис. Не используй знаки сравнения и математические знаки в тексте.\n"
    "- Заголовок короткий и цепляющий, без кликбейта. Тело от двух до четырёх абзацев.\n"
    "Формат ответа. Верни один JSON-объект и больше ничего: "
    '{"title": "<заголовок>", "body": ["<абзац>", "<абзац>", ...]}'
)


def build_retell_user_message(title: str, body: str, language: str) -> str:
    source = (body or "").strip()
    if len(source) > MAX_SOURCE_CHARS:
        source = source[:MAX_SOURCE_CHARS] + "\n(текст обрезан)"
    lang = f"Язык оригинала: {language}.\n" if language else ""
    return f"{lang}Заголовок оригинала: {(title or '').strip()}\nТекст оригинала:\n{source}"


def normalize_ru(text: str) -> str:
    """Enforce the humanizer-ru hard rule the model keeps ignoring: no long dashes.

    Replaces em/en/figure dashes with a plain hyphen. Deterministic, so the output
    complies regardless of the model. Left narrow: quotes and names stay untouched.
    """
    return text.replace("—", "-").replace("–", "-").replace("‒", "-")


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
    return normalize_ru(" ".join(title.split())), [normalize_ru(p) for p in paragraphs]


def retell(router_cfg: "evaluator.Config", news: sqlite3.Row) -> tuple[str, list[str], str]:
    """Ask the model for a Russian retelling; one retry on invalid JSON."""
    messages = [
        {"role": "system", "content": RETELL_SYSTEM},
        {"role": "user", "content": build_retell_user_message(news["title"], news["body_text"], news["language"])},
    ]
    last_error = "модель не отвечала"
    for attempt in range(1, MAX_RETELL_ATTEMPTS + 1):
        reply = evaluator.chat(router_cfg, messages)
        text = reply["text"]
        try:
            title, paragraphs = parse_retelling(evaluator.extract_json_object(text))
        except evaluator.EvaluationInvalid as exc:
            last_error = str(exc)
            log.warning("news %s: retelling attempt %d/%d rejected: %s", news["news_id"], attempt, MAX_RETELL_ATTEMPTS, last_error)
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": f"Ответ не прошёл проверку: {last_error}. Пришли исправленный JSON той же схемы."})
            continue
        return title, paragraphs, reply.get("model_id") or router_cfg.model_id
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
    HTML that used to be stored (paragraphs + source), so nothing is re-run."""
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
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(OWN_SCHEMA_SQL)
    migrate_own_db(con)
    return con


def prepared_ids(con: sqlite3.Connection) -> set[int]:
    return {row[0] for row in con.execute("SELECT news_id FROM prepared_item WHERE status = 'prepared'")}


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


def prepare_one(cfg: PreparerConfig, router_cfg: "evaluator.Config", news: sqlite3.Row, dry_run: bool) -> dict[str, Any]:
    title, paragraphs, model_id = retell(router_cfg, news)
    images: list[dict[str, str]] = []
    if news["primary_url"]:
        try:
            if allowed_by_robots(news["primary_url"], cfg.user_agent):
                time.sleep(cfg.fetch_delay)
                final_url, _, body = fetch(news["primary_url"], cfg.user_agent)
                candidates = extract_illustrations(body, final_url, cfg.max_images)
                if not dry_run:
                    images = download_illustrations(cfg, news["news_id"], candidates)
                else:
                    images = [{"path": f"(dry-run) {c['url']}", "caption": c["caption"], "source_url": c["url"]} for c in candidates]
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("news %s: article fetch failed: %s", news["news_id"], exc)
    source_url = news["primary_url"] or ""
    body_md = build_markdown(title, paragraphs, source_url, source_name_from_url(source_url) if source_url else "")
    return {"title": title, "paragraphs": paragraphs, "model_id": model_id, "images": images, "body_md": body_md}


def run(cfg: PreparerConfig, router_cfg: "evaluator.Config", limit: int, dry_run: bool, only: int | None) -> int:
    news_con = evaluator.open_db(cfg.news_db)
    own_con = open_own_db(cfg.own_db)
    try:
        selected = news_con.execute(SELECTED_SQL).fetchall()
        done = prepared_ids(own_con)
        if only is not None:
            queue = [n for n in selected if n["news_id"] == only]
        else:
            queue = [n for n in selected if n["news_id"] not in done][:limit]
        log.info("selected %d, prepared %d, queue %d (limit %d)", len(selected), len(done), len(queue), limit)

        prepared, failed = 0, 0
        for news in queue:
            try:
                result = prepare_one(cfg, router_cfg, news, dry_run)
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
        log.info("finished: %d prepared, %d failed", prepared, failed)
        return 0 if failed == 0 else 1
    finally:
        news_con.close()
        own_con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare selected news into HTML pages.")
    parser.add_argument("--limit", type=int, default=5, help="batch size (default 5)")
    parser.add_argument("--news-id", type=int, default=None, help="prepare only this news id")
    parser.add_argument("--dry-run", action="store_true", help="fetch, retell and render, but write nothing")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cfg = PreparerConfig.from_env()
    router_cfg = evaluator.Config.from_env()
    router_cfg.params = {"temperature": 0.7, "max_tokens": 1500}
    if not router_cfg.router_token:
        log.error("ROUTER_AUTH_TOKEN is not set")
        return 2
    return run(cfg, router_cfg, limit=args.limit, dry_run=args.dry_run, only=args.news_id)


if __name__ == "__main__":
    sys.exit(main())
