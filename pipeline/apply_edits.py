#!/usr/bin/env python3
"""Apply the operator's edits to a prepared news item.

The model writes «спас троих» where the original says four, and until now the
choice was to open `sqlite3` or not to publish. The web cannot fix it directly —
it must never write this database — so it drops a request file into the mailbox
and this script applies it:

```json
{"news_id": 6412, "title": "…", "body": "…", "lead_image_id": 17,
 "drop_image_ids": [18], "operator": "wildcar"}
```

Every applied edit sets `edited_at`, and that flag is not decoration: the
preparer refuses to regenerate an edited item afterwards. Without it a single
failure would re-queue the news and quietly throw the human's correction away —
exactly the kind of loss nobody notices until the wrong text is public.

A request for a news item that is not prepared, or already published, is
refused and logged: there is nothing useful to do with it, and pretending
otherwise would leave the operator thinking the fix landed.

Single-file, stdlib-only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import preparer
import runlog

log = logging.getLogger("posinus-apply-edits")

REQUEST_GLOB = "edit-*.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def apply_edit(con, request: dict) -> str:
    """Apply one request; returns a short outcome for the log and the run row."""
    news_id = request.get("news_id")
    if not isinstance(news_id, int):
        return "no news_id"

    row = con.execute(
        "SELECT status FROM prepared_item WHERE news_id = ?", (news_id,)
    ).fetchone()
    if row is None:
        return f"news {news_id} is not prepared"
    if row["status"] == "published":
        # The post is already out; editing the source text now would only make
        # the database disagree with what readers can see.
        return f"news {news_id} is already published"

    fields, values = [], []
    if isinstance(request.get("title"), str) and request["title"].strip():
        fields.append("retold_title = ?")
        values.append(request["title"].strip())
    if isinstance(request.get("body"), str) and request["body"].strip():
        fields.append("retold_body_md = ?")
        values.append(request["body"].strip())

    with con:
        if fields:
            fields += ["edited_at = ?", "edited_by = ?"]
            values += [_now(), str(request.get("operator", ""))[:200], news_id]
            con.execute(f"UPDATE prepared_item SET {', '.join(fields)} WHERE news_id = ?", values)

        drop_ids = [i for i in request.get("drop_image_ids", []) if isinstance(i, int)]
        if drop_ids:
            marks = ",".join("?" * len(drop_ids))
            con.execute(
                f"DELETE FROM illustration WHERE news_id = ? AND id IN ({marks})",
                (news_id, *drop_ids),
            )

        lead = request.get("lead_image_id")
        if isinstance(lead, int):
            # Position 0 is the lead: the publisher takes the first illustration.
            # Renumber the rest behind it instead of swapping, so the order the
            # operator sees on the card is the order that goes out.
            others = [
                r["id"]
                for r in con.execute(
                    "SELECT id FROM illustration WHERE news_id = ? AND id != ? ORDER BY position, id",
                    (news_id, lead),
                )
            ]
            con.execute("UPDATE illustration SET position = 0 WHERE id = ? AND news_id = ?", (lead, news_id))
            for index, image_id in enumerate(others, start=1):
                con.execute("UPDATE illustration SET position = ? WHERE id = ?", (index, image_id))

        if not fields and not drop_ids and not isinstance(lead, int):
            return f"news {news_id}: nothing to change"
    return f"news {news_id}: applied"


def run(requests_dir: str, own_db: str, counters: dict | None = None) -> int:
    directory = Path(requests_dir)
    try:
        files = sorted(directory.glob(REQUEST_GLOB))
    except OSError as exc:
        log.error("cannot read the mailbox %s: %s", directory, exc)
        return 1

    con = preparer.open_own_db(own_db)
    applied, refused = 0, 0
    try:
        for path in files:
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.error("%s is not a readable request: %s", path.name, exc)
                refused += 1
                _remove(path)
                continue
            outcome = apply_edit(con, request)
            if outcome.endswith("applied"):
                applied += 1
                log.info("%s: %s", path.name, outcome)
            else:
                refused += 1
                log.warning("%s: %s", path.name, outcome)
            _remove(path)
    finally:
        con.close()
    if counters is not None:
        counters.update(applied=applied, refused=refused)
    log.info("finished: %d applied, %d refused", applied, refused)
    return 0


def _remove(path: Path) -> None:
    """Take the request out of the mailbox, so the path unit does not loop."""
    try:
        path.unlink()
    except OSError as exc:
        log.error("cannot remove %s: %s", path, exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply operator edits from the request mailbox.")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    requests_dir = os.environ.get("REQUESTS_DIR", "/var/lib/posinus/pipeline/requests")
    own_db = os.environ.get("EVALUATOR_DB_PATH", runlog.DEFAULT_DB)
    with runlog.record("apply-edits", own_db, {}) as counters:
        return run(requests_dir, own_db, counters)


if __name__ == "__main__":
    sys.exit(main())
