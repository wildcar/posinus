#!/usr/bin/env python3
"""News publisher: posts prepared news to the platforms.

For every news item the preparer marked «Подготовлено» (`prepared_item.status =
'prepared'`), this posts it to each configured platform and, when all of them
succeed, marks it «Опубликовано». Runs fully automatically by a timer. No model
calls: the title, paragraphs and images are already prepared.

Pacing: at most one NEW item starts per `min_interval_minutes` (default 120), so
posts trickle into the public channel/site instead of flooding it. A platform
that keeps failing is retried up to `max_attempts` times and then given up on;
the item is finalized «Опубликовано» best-effort with whatever platforms
succeeded, so a broken platform (e.g. a bad VK token) never blocks the queue.

Platforms (each turns on only when its secrets are present in the config):

- telegram: sendPhoto + HTML caption to the channel (@posinus).
- site: wildcar.ru on the Эгея engine (login, new-note form, image upload,
  note-process, note-publish) with Neasden markup.
- vk: a community wall post (photo upload + wall.post from the group).

Idempotency: each (news_id, platform) send is recorded in the `publication`
table; a re-run skips platforms already 'ok' and retries only the failed ones.

Single-file, stdlib-only. Shares the preparer's own-DB schema and markdown; it
reads only the evaluator's own DB (the crawler DB is not touched here). Everything
the publisher needs — title, paragraphs, source, images — comes from the prepared
markdown and the illustration table.

Behavior: AGENTS/SPEC.md, section «Публикация (метка "Опубликовано")».
"""

from __future__ import annotations

import argparse
import gzip
import html
import http.cookiejar
import json
import logging
import mimetypes
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import preparer   # own-DB schema + migration, markdown builder

log = logging.getLogger("posinus-publisher")

PUBLISHER_VERSION = "0.1.0"
TG_CAPTION_LIMIT = 1024      # Telegram photo caption hard limit
TG_MESSAGE_LIMIT = 4096      # Telegram text message hard limit
DEFAULT_TG_CHAT = "-1003795927410"   # @posinus channel (from the proven hermes flow)
EGEYA_EMPTY_TAGS_HASH = "d41d8cd98f00b204e9800998ecf8427e"  # md5 of "" — Эгея default
HTTP_TIMEOUT = 90.0
DB_LOCK_RETRIES = 4          # same backoff as evaluator.write_review

_own_db_path: str | None = None   # set by open_own_db, read by _unrecorded_marker

PREPARED_SQL = """
SELECT news_id, retold_title, retold_body_md
FROM prepared_item
WHERE status = 'prepared'
ORDER BY prepared_at ASC, news_id ASC
"""

PUBLICATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS publication (
    news_id INTEGER NOT NULL REFERENCES prepared_item(news_id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,       -- 'ok' | 'error'
    url TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (news_id, platform)
);
CREATE INDEX IF NOT EXISTS idx_publication_news ON publication(news_id);
"""


class PublishError(RuntimeError):
    """A platform refused the post or the transport failed."""


# --------------------------------------------------------------- config


@dataclass
class PublisherConfig:
    own_db: str = "/var/lib/posinus/pipeline/evaluator.sqlite3"
    user_agent: str = "PositiveNewsEvaluator/0.1 (+mailto:mail@wildcar.ru)"
    # Telegram
    tg_token: str = ""
    tg_chat: str = DEFAULT_TG_CHAT
    tg_channel_username: str = "posinus"
    # Site (Эгея, wildcar.ru)
    site_base: str = "https://wildcar.ru"
    site_login: str = "wildcar"
    site_password: str = ""
    site_tags: str = "добрые новости"
    # VK community wall
    vk_token: str = ""
    vk_group_id: str = ""
    vk_api_version: str = "5.199"
    # Pacing: at most one NEW item per interval; give up on a failing platform
    # after this many attempts so it can't block the queue forever.
    min_interval_minutes: int = 120
    max_attempts: int = 8

    @classmethod
    def from_env(cls, env: dict[str, str] = os.environ) -> "PublisherConfig":
        cfg = cls()
        cfg.own_db = env.get("EVALUATOR_DB_PATH", cfg.own_db)
        cfg.user_agent = env.get("PUBLISHER_USER_AGENT", env.get("PREPARER_USER_AGENT", cfg.user_agent))
        cfg.tg_token = env.get("TELEGRAM_BOT_TOKEN", cfg.tg_token)
        cfg.tg_chat = env.get("TELEGRAM_CHAT_ID", cfg.tg_chat)
        cfg.tg_channel_username = env.get("TELEGRAM_CHANNEL_USERNAME", cfg.tg_channel_username)
        cfg.site_base = env.get("EGEYA_BASE_URL", cfg.site_base).rstrip("/")
        cfg.site_login = env.get("EGEYA_LOGIN", cfg.site_login)
        cfg.site_password = env.get("EGEYA_PASSWORD", cfg.site_password)
        cfg.site_tags = env.get("EGEYA_TAGS", cfg.site_tags)
        cfg.vk_token = env.get("VK_ACCESS_TOKEN", cfg.vk_token)
        cfg.vk_group_id = env.get("VK_GROUP_ID", cfg.vk_group_id)
        cfg.vk_api_version = env.get("VK_API_VERSION", cfg.vk_api_version)
        cfg.min_interval_minutes = int(env.get("PUB_MIN_INTERVAL_MINUTES", cfg.min_interval_minutes))
        cfg.max_attempts = int(env.get("PUB_MAX_ATTEMPTS", cfg.max_attempts))
        return cfg

    def enabled_platforms(self) -> list[str]:
        """A platform turns on only when its required secrets are set."""
        platforms: list[str] = []
        if self.tg_token:
            platforms.append("telegram")
        if self.site_password:
            platforms.append("site")
        if self.vk_token and self.vk_group_id:
            platforms.append("vk")
        return platforms


@dataclass
class PreparedNews:
    news_id: int
    title: str
    paragraphs: list[str]
    lead_image: str | None
    source_url: str
    source_name: str


# ---------------------------------------------------------- content builders


def source_name_from_url(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc
    return host[4:] if host.startswith("www.") else host


_SOURCE_LINE_RE = re.compile(r"^Источник:\s*\[([^\]]*)\]\(([^)]+)\)\s*$")


def parse_markdown(md: str) -> tuple[str, list[str], str, str]:
    """Parse the stored markdown doc into (title, paragraphs, source_url, source_name).

    The inverse of preparer.build_markdown: the first H1 is the title, the
    ``Источник: [name](url)`` line is the source, image lines are ignored, and
    everything else is body split into paragraphs on blank lines."""
    title = ""
    source_url = ""
    source_name = ""
    body_lines: list[str] = []
    for line in (md or "").splitlines():
        stripped = line.strip()
        if not title and stripped.startswith("# "):
            title = stripped[2:].strip()
            continue
        match = _SOURCE_LINE_RE.match(stripped)
        if match:
            source_name, source_url = match.group(1).strip(), match.group(2).strip()
            continue
        if stripped.startswith("!["):  # image ref line, if any
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if source_url and not source_name:
        source_name = source_name_from_url(source_url)
    return title, paragraphs, source_url, source_name


def build_tg_caption(
    title: str, paragraphs: list[str], source_url: str, source_name: str, limit: int
) -> str:
    """HTML caption: bold title, as many leading paragraphs as fit, source link."""

    def render(n: int) -> str:
        caption = f"<b>{html.escape(title)}</b>"
        text = "\n\n".join(paragraphs[:n])
        if text:
            caption += "\n\n" + html.escape(text)
        if source_url:
            caption += (
                '\n\n<a href="' + html.escape(source_url, quote=True) + '">'
                + "Источник: " + html.escape(source_name or source_name_from_url(source_url))
                + "</a>"
            )
        return caption

    n = min(len(paragraphs), 3)
    caption = render(n)
    while len(caption) > limit and n > 0:
        n -= 1
        caption = render(n)
    return caption[:limit]


def build_vk_message(title: str, paragraphs: list[str], source_url: str, source_name: str) -> str:
    """Plain-text wall post: title, full retelling, source link."""
    blocks = [title]
    if paragraphs:
        blocks.append("\n\n".join(paragraphs))
    if source_url:
        blocks.append(f"Источник: {source_url}")
    return "\n\n".join(b for b in blocks if b)


def build_site_text(
    image_filename: str, paragraphs: list[str], source_url: str, source_name: str
) -> str:
    """Neasden markup: image filename line, paragraphs, ((url name)) source line."""
    blocks: list[str] = []
    if image_filename:
        blocks.append(image_filename)
    blocks.append("\n\n".join(paragraphs))
    if source_url:
        blocks.append(f"Источник: (({source_url} {source_name or source_name_from_url(source_url)}))")
    return "\n\n".join(b for b in blocks if b)


# ------------------------------------------------------------- HTTP helpers


def guess_mime(path: str) -> str:
    return mimetypes.guess_type(str(path))[0] or "image/jpeg"


def encode_multipart(
    fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]
) -> tuple[str, bytes]:
    """Encode multipart/form-data. files maps name -> (filename, bytes, content_type)."""
    boundary = "----pubnews" + uuid.uuid4().hex
    marker = ("--" + boundary).encode()
    body = bytearray()
    for name, value in fields.items():
        body += marker + b"\r\n"
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        body += str(value).encode("utf-8") + b"\r\n"
    for name, (filename, content, content_type) in files.items():
        body += marker + b"\r\n"
        body += (
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        body += content + b"\r\n"
    body += marker + b"--\r\n"
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def _decode_body(body: bytes) -> bytes:
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def http_send(
    url: str, data: bytes | None = None, headers: dict[str, str] | None = None,
    method: str = "POST", timeout: float = HTTP_TIMEOUT,
) -> tuple[int, bytes]:
    """One request via the default opener (follows redirects). Returns (status, body).

    HTTPError is not raised: its body is returned, so callers can read a JSON
    error payload (Telegram/VK put the real error there with a 4xx status)."""
    request_headers = {"Accept-Encoding": "gzip, identity"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), _decode_body(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode_body(exc.read())


def _post_json_result(url: str, data: bytes, content_type: str, timeout: float) -> dict[str, Any]:
    status, body = http_send(url, data=data, headers={"Content-Type": content_type}, timeout=timeout)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise PublishError(f"non-JSON reply (status {status}): {body[:200]!r}") from exc


# -------------------------------------------------------- Эгея cookie session


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never auto-follow; let the caller read the Location header itself."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class _Response:
    def __init__(self, status: int, headers: Any, body: bytes, url: str) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def location(self) -> str:
        return self.headers.get("Location", "") if self.headers else ""


class Session:
    """A cookie-keeping HTTP session that does not auto-follow redirects.

    Enough of the ``requests.Session`` surface for the Эгея publish flow."""

    def __init__(self, user_agent: str, timeout: float = 60.0) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect()
        )
        self.user_agent = user_agent
        self.timeout = timeout

    def _do(self, method: str, url: str, data: bytes | None, headers: dict[str, str] | None) -> _Response:
        request_headers = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, identity"}
        if headers:
            request_headers.update(headers)
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            resp = self.opener.open(req, timeout=self.timeout)
            status, rheaders, body = resp.getcode(), resp.headers, resp.read()
        except urllib.error.HTTPError as exc:  # 3xx included (redirects are disabled)
            status, rheaders, body = exc.code, exc.headers, exc.read()
        return _Response(status, rheaders, _decode_body(body), url)

    def get(self, url: str, headers: dict[str, str] | None = None, max_redirects: int = 5) -> _Response:
        resp = self._do("GET", url, None, headers)
        seen = 0
        while resp.status in (301, 302, 303, 307, 308) and resp.location() and seen < max_redirects:
            url = urllib.parse.urljoin(url, resp.location())
            resp = self._do("GET", url, None, headers)
            resp.url = url
            seen += 1
        return resp

    def post_form(self, url: str, fields: dict[str, str], headers: dict[str, str] | None = None) -> _Response:
        merged = {"Content-Type": "application/x-www-form-urlencoded"}
        if headers:
            merged.update(headers)
        return self._do("POST", url, urllib.parse.urlencode(fields).encode("utf-8"), merged)

    def post_multipart(
        self, url: str, fields: dict[str, str], files: dict[str, tuple[str, bytes, str]],
        headers: dict[str, str] | None = None,
    ) -> _Response:
        content_type, body = encode_multipart(fields, files)
        merged = {"Content-Type": content_type}
        if headers:
            merged.update(headers)
        return self._do("POST", url, body, merged)


def input_val(page: str, field: str) -> str:
    """Read an <input> value by id, then by name, unescaping HTML entities."""
    m = re.search(r'id="' + re.escape(field) + r'"[\s\S]*?value="([^"]*)"', page)
    if not m:
        m = re.search(r'name="' + re.escape(field) + r'"[\s\S]*?value="([^"]*)"', page)
    return html.unescape(m.group(1)) if m else ""


def _abs_url(base: str, loc: str) -> str:
    if not loc:
        return ""
    if loc.startswith("http"):
        return loc
    if loc.startswith("/"):
        return base + loc
    return loc


# ----------------------------------------------------------- platform: telegram


def publish_telegram(cfg: PublisherConfig, item: PreparedNews, dry_run: bool) -> str:
    has_image = bool(item.lead_image)
    limit = TG_CAPTION_LIMIT if has_image else TG_MESSAGE_LIMIT
    caption = build_tg_caption(item.title, item.paragraphs, item.source_url, item.source_name, limit)
    if dry_run:
        log.info("news %s telegram [dry-run]: image=%s, %d chars", item.news_id, has_image, len(caption))
        return "(dry-run)"

    api = f"https://api.telegram.org/bot{cfg.tg_token}"
    if has_image:
        image = Path(item.lead_image).read_bytes()  # type: ignore[arg-type]
        content_type, body = encode_multipart(
            {"chat_id": cfg.tg_chat, "caption": caption, "parse_mode": "HTML"},
            {"photo": (Path(item.lead_image).name, image, guess_mime(item.lead_image))},  # type: ignore[arg-type]
        )
        payload = _post_json_result(api + "/sendPhoto", body, content_type, HTTP_TIMEOUT)
    else:
        fields = {"chat_id": cfg.tg_chat, "text": caption, "parse_mode": "HTML"}
        payload = _post_json_result(
            api + "/sendMessage", urllib.parse.urlencode(fields).encode("utf-8"),
            "application/x-www-form-urlencoded", HTTP_TIMEOUT,
        )
    if not payload.get("ok"):
        raise PublishError(f"telegram: {payload.get('description') or payload}")
    message_id = payload.get("result", {}).get("message_id")
    if cfg.tg_channel_username and message_id:
        return f"https://t.me/{cfg.tg_channel_username}/{message_id}"
    return f"tg:{cfg.tg_chat}:{message_id}"


# ----------------------------------------------------------------- platform: vk


def vk_call(cfg: PublisherConfig, method: str, params: dict[str, Any]) -> Any:
    query = dict(params)
    query["access_token"] = cfg.vk_token
    query["v"] = cfg.vk_api_version
    payload = _post_json_result(
        f"https://api.vk.ru/method/{method}",
        urllib.parse.urlencode(query).encode("utf-8"),
        "application/x-www-form-urlencoded",
        HTTP_TIMEOUT,
    )
    if "error" in payload:
        err = payload["error"]
        raise PublishError(f"vk {method}: {err.get('error_code')} {err.get('error_msg')}")
    return payload.get("response")


def vk_upload_photo(cfg: PublisherConfig, image_path: str) -> str:
    """Upload a wall photo and return its attachment string (photo{owner}_{id}).

    Needs a user token of a group admin: photos.getWallUploadServer refuses a
    community token with error 27."""
    server = vk_call(cfg, "photos.getWallUploadServer", {"group_id": cfg.vk_group_id})
    upload_url = server.get("upload_url")
    if not upload_url:
        raise PublishError("vk: no upload_url from getWallUploadServer")
    image = Path(image_path).read_bytes()
    content_type, body = encode_multipart(
        {}, {"photo": (Path(image_path).name, image, guess_mime(image_path))}
    )
    uploaded = _post_json_result(upload_url, body, content_type, HTTP_TIMEOUT)
    if not uploaded.get("photo") or uploaded.get("photo") == "[]":
        raise PublishError(f"vk: upload server returned no photo: {uploaded}")
    saved = vk_call(cfg, "photos.saveWallPhoto", {
        "group_id": cfg.vk_group_id,
        "server": uploaded["server"], "photo": uploaded["photo"], "hash": uploaded["hash"],
    })
    photo = saved[0]
    return f"photo{photo['owner_id']}_{photo['id']}"


def publish_vk(cfg: PublisherConfig, item: PreparedNews, dry_run: bool) -> str:
    message = build_vk_message(item.title, item.paragraphs, item.source_url, item.source_name)
    if dry_run:
        log.info("news %s vk [dry-run]: image=%s, %d chars", item.news_id, bool(item.lead_image), len(message))
        return "(dry-run)"
    attachment = vk_upload_photo(cfg, item.lead_image) if item.lead_image else ""
    response = vk_call(cfg, "wall.post", {
        "owner_id": f"-{cfg.vk_group_id}", "from_group": 1,
        "message": message, "attachments": attachment,
    })
    post_id = response.get("post_id")
    return f"https://vk.ru/wall-{cfg.vk_group_id}_{post_id}"


# --------------------------------------------------------------- platform: site


def publish_site(cfg: PublisherConfig, item: PreparedNews, dry_run: bool) -> str:
    """Post to wildcar.ru (Эгея): login, upload image, submit and publish the note."""
    if dry_run:
        text = build_site_text("<image>", item.paragraphs, item.source_url, item.source_name)
        log.info("news %s site [dry-run]: title='%s', %d chars", item.news_id, item.title, len(text))
        return "(dry-run)"

    base = cfg.site_base
    session = Session(cfg.user_agent)
    ref = {"Referer": base + "/new/"}

    page = session.get(base + "/new/", headers=ref)
    if "form-note" not in page.text:
        session.post_form(base + "/@actions/sign-in/", {"login": cfg.site_login, "password": cfg.site_password})
        page = session.get(base + "/new/", headers=ref)
    if "form-note" not in page.text:
        raise PublishError(f"site: cannot open new-note form (status {page.status})")

    token = input_val(page.text, "token")
    if not token:
        raise PublishError("site: no CSRF token on the new-note form")
    old_stamp = input_val(page.text, "old-stamp")
    old_hash = input_val(page.text, "old-tags-hash") or EGEYA_EMPTY_TAGS_HASH

    filename = ""
    if item.lead_image:
        image = Path(item.lead_image).read_bytes()
        upload = session.post_multipart(
            base + f"/@ajax/file-upload/?entity=note&entity-id=new&token={urllib.parse.quote(token)}",
            fields={"token": token},
            files={"file": (Path(item.lead_image).name, image, guess_mime(item.lead_image))},
            headers={"X-CSRF-Token": token, "Referer": base + "/new/"},
        )
        try:
            reply = upload.json()
        except json.JSONDecodeError:
            reply = {}
        if not (reply.get("success") or reply.get("ok")):
            raise PublishError(f"site: image upload failed (status {upload.status})")
        data = reply.get("data", {})
        filename = data.get("new-name") or data.get("name") or Path(item.lead_image).name

    text = build_site_text(filename, item.paragraphs, item.source_url, item.source_name)
    form = {
        "note-timestamp": "0", "note-id": "new", "formatter-id": "neasden",
        "is-note-published": "true", "old-tags-hash": old_hash, "old-stamp": old_stamp,
        "action": "write", "token": token, "browser-offset": "0",
        "title": item.title, "text": text, "tags": cfg.site_tags,
    }
    submit = session.post_form(
        base + "/@actions/note-process/", form,
        headers={"X-CSRF-Token": token, "Referer": base + "/new/"},
    )
    url = _abs_url(base, submit.location())

    if "/drafts/" in url:  # landed as a draft — publish it explicitly
        draft = session.get(url)
        draft_token = input_val(draft.text, "token") or token
        note_id = input_val(draft.text, "note-id")
        published = session.post_form(
            base + "/@actions/note-publish/",
            {"note-id": note_id, "token": draft_token, "action": "publish"},
            headers={"X-CSRF-Token": draft_token, "Referer": url},
        )
        if published.location():
            url = _abs_url(base, published.location())

    if not url:
        raise PublishError("site: no post URL after submit")
    final = session.get(url)
    if final.status != 200 or "Неопубликовано" in final.text:
        raise PublishError(f"site: post not visible (status {final.status})")
    return final.url


ADAPTERS: dict[str, Callable[[PublisherConfig, PreparedNews, bool], str]] = {
    "telegram": publish_telegram,
    "site": publish_site,
    "vk": publish_vk,
}


# ---------------------------------------------------------------- own storage


def open_own_db(path: str) -> sqlite3.Connection:
    global _own_db_path
    _own_db_path = path        # where _unrecorded_marker drops its file
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    # See preparer.open_own_db: WAL so a reader cannot block the write that
    # records a post we have already sent.
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(preparer.OWN_SCHEMA_SQL)   # prepared_item / illustration
    preparer.migrate_own_db(con)                 # add retold_body_md to older DBs
    con.executescript(PUBLICATION_SCHEMA_SQL)
    return con


def publication_status(con: sqlite3.Connection, news_id: int) -> dict[str, str]:
    return {row["platform"]: row["status"]
            for row in con.execute("SELECT platform, status FROM publication WHERE news_id = ?", (news_id,))}


def publication_state(con: sqlite3.Connection, news_id: int) -> dict[str, tuple[str, int]]:
    """Per-platform (status, attempts) for one news item."""
    return {row["platform"]: (row["status"], row["attempts"])
            for row in con.execute(
                "SELECT platform, status, attempts FROM publication WHERE news_id = ?", (news_id,))}


def last_success_at(con: sqlite3.Connection) -> str | None:
    """When anything was last posted successfully (drives the new-item throttle)."""
    row = con.execute("SELECT MAX(updated_at) AS t FROM publication WHERE status = 'ok'").fetchone()
    return row["t"] if row and row["t"] else None


def lead_image_path(con: sqlite3.Connection, news_id: int) -> str | None:
    row = con.execute(
        "SELECT file_path FROM illustration WHERE news_id = ? ORDER BY position ASC LIMIT 1",
        (news_id,),
    ).fetchone()
    if row and row["file_path"] and Path(row["file_path"]).exists():
        return row["file_path"]
    return None


def _unrecorded_marker(news_id: int, platform: str, status: str, url: str | None) -> None:
    """Leave a file next to the DB when a send could not be recorded.

    The post is already public at this point, so losing the row means the next
    run would send it again. The marker survives the crash and names the pair a
    human has to reconcile by hand.
    """
    if _own_db_path is None:
        return
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        marker = Path(_own_db_path).parent / f"unrecorded-{news_id}-{platform}-{stamp}.txt"
        marker.write_text(f"{status} {url or ''}\n", encoding="utf-8")
        log.error("wrote %s: this send is public but not in the database", marker)
    except OSError:
        log.exception("could not write the unrecorded-send marker for news %s / %s",
                      news_id, platform)


def record_publication(
    con: sqlite3.Connection, news_id: int, platform: str, status: str,
    url: str | None, error: str | None,
) -> None:
    """Record one send, retrying while the database is locked.

    This runs AFTER the post has gone out, so a lost write means a duplicate on
    the next run. Hence the retry, and hence the loud failure when it still does
    not go through.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for attempt in range(DB_LOCK_RETRIES):
        try:
            with con:
                con.execute(
                    "INSERT INTO publication (news_id, platform, status, url, error, attempts, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(news_id, platform) DO UPDATE SET status=excluded.status, url=excluded.url, "
                    "error=excluded.error, attempts=publication.attempts+1, updated_at=excluded.updated_at",
                    (news_id, platform, status, url, (error or "")[:1000] or None, now),
                )
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            if attempt == DB_LOCK_RETRIES - 1:
                if status == "ok":
                    _unrecorded_marker(news_id, platform, status, url)
                raise
            delay = 0.5 * 2**attempt
            log.warning("database is locked, retrying the %s record in %.1fs", platform, delay)
            time.sleep(delay)


def mark_published(con: sqlite3.Connection, news_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with con:
        con.execute(
            "UPDATE prepared_item SET status = 'published', published_at = ? WHERE news_id = ?",
            (now, news_id),
        )


def build_item(own: sqlite3.Connection, row: sqlite3.Row) -> PreparedNews:
    title, paragraphs, source_url, source_name = parse_markdown(row["retold_body_md"] or "")
    return PreparedNews(
        news_id=row["news_id"],
        title=title or (row["retold_title"] or ""),
        paragraphs=paragraphs,
        lead_image=lead_image_path(own, row["news_id"]),
        source_url=source_url,
        source_name=source_name,
    )


# ---------------------------------------------------------------- pipeline


def _pending_platforms(state: dict[str, tuple[str, int]], platforms: list[str], max_attempts: int) -> list[str]:
    """Platforms still worth trying: not yet ok and not out of attempts."""
    return [p for p in platforms
            if state.get(p, ("", 0))[0] != "ok" and state.get(p, ("", 0))[1] < max_attempts]


def _settled(state: dict[str, tuple[str, int]], platforms: list[str], max_attempts: int) -> bool:
    """True when every platform is either ok or has exhausted its attempts."""
    return all(state.get(p, ("", 0))[0] == "ok" or state.get(p, ("", 0))[1] >= max_attempts
               for p in platforms)


def run(cfg: PublisherConfig, limit: int, dry_run: bool, only: int | None) -> int:
    platforms = cfg.enabled_platforms()
    if not platforms:
        log.warning(
            "no platform configured; set TELEGRAM_BOT_TOKEN / EGEYA_PASSWORD / "
            "VK_ACCESS_TOKEN+VK_GROUP_ID to enable one. Nothing to do."
        )
        return 0

    own = open_own_db(cfg.own_db)
    try:
        prepared = own.execute(PREPARED_SQL).fetchall()
        now = datetime.now(timezone.utc)
        last_ok = last_success_at(own)
        throttled_new = False
        if last_ok:
            try:
                throttled_new = (now - datetime.fromisoformat(last_ok)) < timedelta(minutes=cfg.min_interval_minutes)
            except ValueError:
                throttled_new = False
        log.info("prepared %d, platforms [%s], last post %s, new items %s%s",
                 len(prepared), ", ".join(platforms), last_ok or "never",
                 "throttled" if throttled_new else "allowed", " (dry-run)" if dry_run else "")

        published, incomplete, new_posted = 0, 0, 0
        for row in prepared:
            news_id = row["news_id"]
            if only is not None and news_id != only:
                continue
            state = publication_state(own, news_id)
            appeared = any(state.get(p, ("", 0))[0] == "ok" for p in platforms)
            pending = _pending_platforms(state, platforms, cfg.max_attempts)

            if not pending:
                # Every platform is ok or out of attempts: finalize best-effort so a
                # persistently failing platform cannot block the rest of the queue.
                gave_up = [p for p in platforms if state.get(p, ("", 0))[0] != "ok"]
                if not dry_run:
                    mark_published(own, news_id)
                published += 1
                if gave_up:
                    log.warning("news %s: «Опубликовано» best-effort, gave up on %s after %d attempts",
                                news_id, ", ".join(gave_up), cfg.max_attempts)
                continue

            # A brand-new item (nothing posted yet) is rate-limited; an item already
            # public somewhere is finished regardless (it is the same news).
            if not appeared and only is None and (throttled_new or new_posted >= limit):
                continue

            item = build_item(own, row)
            for platform in pending:
                try:
                    url = ADAPTERS[platform](cfg, item, dry_run)
                except Exception as exc:  # one bad platform must not sink the batch
                    log.error("news %s -> %s failed: %s", news_id, platform, exc)
                    if not dry_run:
                        record_publication(own, news_id, platform, "error", None, str(exc))
                    continue
                log.info("news %s -> %s ok: %s", news_id, platform, url)
                if not dry_run:
                    record_publication(own, news_id, platform, "ok", url, None)

            if not appeared:
                new_posted += 1
                throttled_new = True  # at most one fresh appearance per run and window

            if dry_run:
                continue
            state_now = publication_state(own, news_id)
            if _settled(state_now, platforms, cfg.max_attempts):
                mark_published(own, news_id)
                published += 1
                gave_up = [p for p in platforms if state_now.get(p, ("", 0))[0] != "ok"]
                if gave_up:
                    log.warning("news %s: «Опубликовано» best-effort, gave up on %s after %d attempts",
                                news_id, ", ".join(gave_up), cfg.max_attempts)
                else:
                    log.info("news %s: all platforms ok -> «Опубликовано»", news_id)
            else:
                incomplete += 1

        log.info("finished: %d published, %d incomplete (will retry)%s",
                 published, incomplete, " (dry-run, nothing sent)" if dry_run else "")
        return 0
    finally:
        own.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish prepared news to the platforms.")
    parser.add_argument("--limit", type=int, default=1,
                        help="max NEW items to start per run (default 1); retries of already-public items are not limited")
    parser.add_argument("--news-id", type=int, default=None,
                        help="publish only this news id (ignores the rate limit)")
    parser.add_argument("--dry-run", action="store_true", help="build content and log, send nothing")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cfg = PublisherConfig.from_env()
    return run(cfg, limit=args.limit, dry_run=args.dry_run, only=args.news_id)


if __name__ == "__main__":
    sys.exit(main())
