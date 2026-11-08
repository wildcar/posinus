#!/usr/bin/env python3
"""Take a published news item down from every platform.

The operator presses «Снять новость» on the «Эфир → Вышло» tab. The web may not
write this database, so it drops `takedown-<news_id>.json` into the request
mailbox and a systemd path unit runs this script within a second:

```json
{"news_id": 29926, "operator": "wildcar", "reason": "решение редактора"}
```

For every platform the item went out to (`publication.status = 'ok'`) the post
is deleted where it lives:

- wildcar_org — the page directory goes, the section index and the Dzen feed
  are rebuilt from the remaining `ok` rows, the site is rebuilt via its marker;
- telegram    — deleteMessage in the channel;
- vk          — wall.delete on the community wall;
- site        — wildcar.ru (Эгея): sign in, read the note id off the page,
                /@actions/note-delete/, then check the page is gone.

A platform that went through is flipped to `status = 'removed'` with the reason
in `error` (rows are never deleted: SPEC, «Идемпотентность и метки»). A platform
that refused keeps `status = 'ok'` — the post IS still public — and gets the
failure in `error`, so the operator sees what to remove by hand; pressing the
button again retries only those. Copies on Дзен (its Telegram autopublisher and
the RSS import) are outside our reach and stay.

Single-file, stdlib-only.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import publisher
import runlog
from publisher import PublishError, PublisherConfig

log = logging.getLogger("posinus-takedown")

REQUEST_GLOB = "takedown-*.json"
DEFAULT_REASON = "решение редактора"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ platforms


def remove_wildcar_org(cfg: PublisherConfig, news_id: int, url: str) -> None:
    """Delete the page and rebuild the index and the feed without it.

    The caller has already flipped the row: the index and the feed are built
    from `ok` rows only, so the removed item drops out of them by itself."""
    with publisher.wildcar_lock(cfg):
        page_dir = publisher.wildcar_page_dir(cfg, news_id)
        if page_dir.is_dir():
            shutil.rmtree(page_dir)
        publisher.write_wildcar_section(cfg, publisher._wildcar_published_entries(cfg, exclude_id=news_id))
    Path(cfg.requests_dir, publisher.WILDCAR_REBUILD_MARKER).touch()


def telegram_message_id(url: str) -> int:
    match = re.search(r"(?:t\.me/[^/]+/|^tg:[^:]+:)(\d+)$", url or "")
    if not match:
        raise PublishError(f"telegram: no message id in {url!r}")
    return int(match.group(1))


def remove_telegram(cfg: PublisherConfig, news_id: int, url: str) -> None:
    if not cfg.tg_token or not cfg.tg_chat:
        raise PublishError("telegram: no bot token or chat in the config")
    fields = {"chat_id": cfg.tg_chat, "message_id": telegram_message_id(url)}
    payload = publisher._post_json_result(
        f"https://api.telegram.org/bot{cfg.tg_token}/deleteMessage",
        urllib.parse.urlencode(fields).encode("utf-8"),
        "application/x-www-form-urlencoded", publisher.HTTP_TIMEOUT)
    if payload.get("ok"):
        return
    description = str(payload.get("description") or payload)
    if "not found" in description.lower():   # already deleted by hand
        log.info("news %s telegram: message already gone (%s)", news_id, description)
        return
    raise PublishError(f"telegram: {description}")


def vk_post_id(url: str) -> int:
    match = re.search(r"wall-?\d+_(\d+)", url or "")
    if not match:
        raise PublishError(f"vk: no post id in {url!r}")
    return int(match.group(1))


def remove_vk(cfg: PublisherConfig, news_id: int, url: str) -> None:
    # The admin's VK ID user token can delete any wall post of the community;
    # a community key cannot even delete postponed ones (tools/vk_probe.py).
    token = publisher.vk_id_token(cfg) if cfg.vk_id_store else ""
    publisher.vk_call(cfg, "wall.delete",
                      {"owner_id": f"-{cfg.vk_group_id}", "post_id": vk_post_id(url)}, token)


def site_note_id(page: str) -> str:
    match = re.search(r'data-note-id="(\d+)"', page) or re.search(r'id="note-id"[^>]*value="(\d+)"', page)
    return match.group(1) if match else ""


def remove_site(cfg: PublisherConfig, news_id: int, url: str) -> None:
    """wildcar.ru (Эгея): the same sign-in as the publisher, then note-delete.

    The note id is on the public page (`data-note-id`), the CSRF token on any
    form of the signed-in session. Success is judged by the page itself: Эгея
    answers a delete with a redirect either way."""
    if not url:
        raise PublishError("site: no post URL recorded")
    base = cfg.site_base
    session = publisher.Session(cfg.user_agent)
    form = session.get(base + "/new/", headers={"Referer": base + "/new/"})
    if "form-note" not in form.text:
        session.post_form(base + "/@actions/sign-in/", {"login": cfg.site_login, "password": cfg.site_password})
        form = session.get(base + "/new/", headers={"Referer": base + "/new/"})
    if "form-note" not in form.text:
        raise PublishError(f"site: cannot sign in (status {form.status})")

    page = session.get(url)
    if page.status == 404:
        log.info("news %s site: %s is already gone", news_id, url)
        return
    note_id = site_note_id(page.text)
    if not note_id:
        raise PublishError(f"site: no note id on {url} (status {page.status})")
    token = publisher.input_val(page.text, "token") or publisher.input_val(form.text, "token")
    if not token:
        raise PublishError("site: no CSRF token")
    session.post_form(base + "/@actions/note-delete/", {"note-id": note_id, "token": token},
                      headers={"X-CSRF-Token": token, "Referer": url})
    status, _ = publisher.http_send(url, method="GET", timeout=30)
    if status == 200:
        raise PublishError(f"site: {url} still answers 200 after note-delete")


REMOVERS: dict[str, Callable[[PublisherConfig, int, str], None]] = {
    "telegram": remove_telegram,
    "vk": remove_vk,
    "site": remove_site,
    # Last: its rebuild reads the other rows, and a page left over because the
    # others failed is no worse than one left over on its own.
    "wildcar_org": remove_wildcar_org,
}


# -------------------------------------------------------------------- storage


def _set_row(con: sqlite3.Connection, news_id: int, platform: str, status: str, error: str) -> None:
    with con:
        con.execute(
            "UPDATE publication SET status = ?, error = ?, updated_at = ? WHERE news_id = ? AND platform = ?",
            (status, error[:1000], _now(), news_id, platform))


def take_down(cfg: PublisherConfig, con: sqlite3.Connection, request: dict,
              removers: dict[str, Callable[[PublisherConfig, int, str], None]] | None = None) -> tuple[int, int, str]:
    """Take one item down. Returns (removed, failed, outcome) for the log."""
    removers = REMOVERS if removers is None else removers
    news_id = request.get("news_id")
    if not isinstance(news_id, int):
        return 0, 0, "no news_id"
    row = con.execute("SELECT status FROM prepared_item WHERE news_id = ?", (news_id,)).fetchone()
    if row is None or row["status"] != "published":
        # A partly published item is still in the publisher's queue and would go
        # out again on the next run; that case is not what the button is for.
        return 0, 0, f"news {news_id} is not published (status {row['status'] if row else 'none'})"

    operator = str(request.get("operator") or "")[:200]
    reason = str(request.get("reason") or DEFAULT_REASON).strip()[:500] or DEFAULT_REASON
    note = f"{reason} ({operator})" if operator else reason
    live = {r["platform"]: r["url"] or "" for r in con.execute(
        "SELECT platform, url FROM publication WHERE news_id = ? AND status = 'ok'", (news_id,))}
    if not live:
        return 0, 0, f"news {news_id}: nothing left to take down"

    order = [p for p in removers if p in live] + sorted(p for p in live if p not in removers)
    removed, failed = 0, 0
    for platform in order:
        url = live[platform]
        remover = removers.get(platform)
        if platform == "wildcar_org" and remover is not None:
            # Flip first: the rebuild takes only `ok` rows. Put it back if the
            # files could not be touched — the page would still be up.
            _set_row(con, news_id, platform, "removed", note)
        try:
            if remover is None:
                raise PublishError(f"{platform}: no way to take a post down here")
            remover(cfg, news_id, url)
        except (PublishError, OSError, ValueError, KeyError) as exc:
            failed += 1
            log.error("news %s %s: takedown failed: %s", news_id, platform, exc)
            _set_row(con, news_id, platform, "ok", f"снять не удалось: {exc}")
            continue
        removed += 1
        log.info("news %s %s: taken down (%s)", news_id, platform, url)
        _set_row(con, news_id, platform, "removed", note)
    return removed, failed, f"news {news_id}: {removed} removed, {failed} failed"


# ------------------------------------------------------------------- mailbox


def run(cfg: PublisherConfig, counters: dict | None = None,
        removers: dict[str, Callable[[PublisherConfig, int, str], None]] | None = None) -> int:
    directory = Path(cfg.requests_dir)
    try:
        files = sorted(directory.glob(REQUEST_GLOB))
    except OSError as exc:
        log.error("cannot read the mailbox %s: %s", directory, exc)
        return 1

    con = publisher.open_own_db(cfg.own_db)
    total_removed, total_failed, refused = 0, 0, 0
    try:
        for path in files:
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.error("%s is not a readable request: %s", path.name, exc)
                refused += 1
                _remove(path)
                continue
            # Out of the mailbox before the slow part: the path unit must not
            # start a second run on the same request while this one talks to VK.
            _remove(path)
            removed, failed, outcome = take_down(cfg, con, request, removers)
            if removed or failed:
                log.info("%s: %s", path.name, outcome)
            else:
                refused += 1
                log.warning("%s: %s", path.name, outcome)
            total_removed += removed
            total_failed += failed
    finally:
        con.close()
    if counters is not None:
        counters.update(removed=total_removed, failed=total_failed, refused=refused)
    log.info("finished: %d posts removed, %d failed, %d requests refused",
             total_removed, total_failed, refused)
    return 0


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.error("cannot remove %s: %s", path, exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Take published news down from every platform.")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cfg = PublisherConfig.from_env()
    with runlog.record("takedown", cfg.own_db, {}) as counters:
        return run(cfg, counters)


if __name__ == "__main__":
    sys.exit(main())
