"""«Картина дня»: the gallery of past issues, read from the pipeline's database.

The split mirrors the rest of the system: the settings (what to draw, when,
in which style) live in the crawler DB and are edited here; what the machine
actually did — the pictures, the prompts it used, where each issue was posted —
lives in the pipeline's own database and is only read, through the same
rules as everything in `pipeline_db`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from django.conf import settings

from collector.services.pipeline_db import PipelineUnavailable, fetch_all

PLATFORM_TITLES = {
    "telegram": "Telegram", "site": "wildcar.ru", "vk": "ВКонтакте",
    "wildcar_org": "wildcar.org",
}

# `SELECT *` on purpose, same as the news card: the pipeline owns this schema
# and adds columns on its own schedule (`file_path_wide` and `caption` arrived
# after the first deploy). Naming them here would blind the gallery for as long
# as the two deploys are out of step.
ITEMS_SQL = """
SELECT *
FROM daypic_item
ORDER BY day DESC, slot ASC
LIMIT ?
"""

PUBLICATIONS_SQL = """
SELECT item_id, platform, status, url, error, attempts
FROM daypic_publication
"""


@dataclass
class DaypicIssue:
    item_id: int
    day: str
    slot: str
    status: str
    style: str
    prompt: str
    caption: str
    filename: str
    filename_wide: str
    image_model: str
    attempts: int
    error: str
    file_purged: bool
    published_at: datetime | None
    platforms: list[dict] = field(default_factory=list)


def _moment(raw) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return value


def _column(row, name: str):
    return row[name] if name in row.keys() else None


def gallery(limit: int = 60) -> tuple[list[DaypicIssue], str]:
    """Past issues, newest first, with per-platform links; ('', reason) style.

    Returns the reason as the second value when the pipeline DB is unreachable
    or predates the daypic tables — the settings half of the page still works.
    """
    try:
        rows = fetch_all(ITEMS_SQL, (limit,))
        publications = fetch_all(PUBLICATIONS_SQL)
    except PipelineUnavailable as exc:
        return [], str(exc)
    issues: dict[int, DaypicIssue] = {}
    for row in rows:
        wide = _column(row, "file_path_wide")
        issues[row["id"]] = DaypicIssue(
            item_id=row["id"],
            day=row["day"],
            slot=row["slot"],
            status=row["status"],
            style=row["style"] or "",
            prompt=row["prompt"] or "",
            caption=_column(row, "caption") or "",
            filename=Path(row["file_path"]).name if row["file_path"] else "",
            filename_wide=Path(wide).name if wide else "",
            image_model=row["image_model_id"] or "",
            attempts=row["attempts"],
            error=row["error"] or "",
            file_purged=bool(row["file_purged_at"]),
            published_at=_moment(row["published_at"]),
        )
    for row in publications:
        issue = issues.get(row["item_id"])
        if issue is None:
            continue
        issue.platforms.append({
            "platform": row["platform"],
            "title": PLATFORM_TITLES.get(row["platform"], row["platform"]),
            "status": row["status"],
            "url": row["url"] or "",
            "error": row["error"] or "",
            "attempts": row["attempts"],
        })
    for issue in issues.values():
        issue.platforms.sort(key=lambda row: row["title"])
    return list(issues.values()), ""


def picture_path(filename: str) -> Path | None:
    """Absolute path of one daily picture, if the pipeline really has that row.

    The name comes from a URL, so it is never trusted: it must match a row in
    the pipeline DB (either orientation) and resolve inside the daypic directory.
    """
    try:
        rows = fetch_all("SELECT * FROM daypic_item")
    except PipelineUnavailable:
        return None
    names = set()
    for row in rows:
        for column in ("file_path", "file_path_wide"):
            value = _column(row, column)
            if value:
                names.add(Path(value).name)
    if filename not in names:
        return None
    root = Path(settings.POSINUS_DAYPIC_DIR).resolve()
    candidate = (root / filename).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate
