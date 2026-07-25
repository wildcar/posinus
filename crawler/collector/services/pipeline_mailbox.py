"""The web side of the pipeline request mailbox.

The crawler and the pipeline share one directory (`POSINUS_PIPELINE_REQUESTS_DIR`,
group `posinus`, setgid): the web drops a small file in it, the pipeline reads it.
That is the whole protocol. The web never writes the pipeline's database — the
contract forbids it, and a third writer on that SQLite file is exactly what makes
the publisher post twice.

Two kinds of file:

- `pause` — the stop cock. While it is there the publisher sends nothing.
- `run-<service>` — run now. A systemd `.path` unit starts the service within a
  second and the service deletes the file before it works.

The directory does not exist on a development machine, and it must not exist for
the site to work: every reader here raises `MailboxUnavailable` instead, and the
views show that in words.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone

PAUSE_FILE = "pause"
RUN_SERVICES = ("evaluator", "preparer", "publisher")


class MailboxUnavailable(RuntimeError):
    """The shared directory is missing or not writable from the web process."""


@dataclass
class Pause:
    """An active stop cock as the operator sees it."""

    until: datetime | None
    reason: str


def mailbox_dir() -> Path:
    return Path(settings.POSINUS_PIPELINE_REQUESTS_DIR)


def _pause_path() -> Path:
    return mailbox_dir() / PAUSE_FILE


def read_pause() -> Pause | None:
    """The current stop cock, or None when publication runs normally.

    An expired file reads as «not paused»: the publisher removes it on its next
    run, and until then the operator should not see a pause that no longer holds.
    """
    path = _pause_path()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        if not path.parent.is_dir():
            raise MailboxUnavailable(f"{path.parent} does not exist") from None
        return None
    except OSError as exc:
        raise MailboxUnavailable(str(exc)) from exc

    until, reason = None, ""
    for line in raw.splitlines():
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key == "until" and value.strip():
            until = _parse_moment(value.strip())
        elif key == "reason":
            reason = value.strip()
    if until is not None and until <= timezone.now():
        return None
    return Pause(until, reason)


def set_pause(until: datetime | None, reason: str) -> None:
    """Stop publication until `until` (None = until the operator lifts it)."""
    lines = []
    if until is not None:
        lines.append(f"until={until.isoformat()}")
    lines.append(f"reason={reason.strip()}")
    _write(_pause_path(), "\n".join(lines) + "\n")


def clear_pause() -> None:
    try:
        _pause_path().unlink()
    except FileNotFoundError:
        if not mailbox_dir().is_dir():
            raise MailboxUnavailable(f"{mailbox_dir()} does not exist") from None
    except OSError as exc:
        raise MailboxUnavailable(str(exc)) from exc


def request_run(service: str) -> None:
    """Ask systemd to run one pipeline service now."""
    if service not in RUN_SERVICES:
        raise ValueError(f"unknown service {service!r}")
    _write(mailbox_dir() / f"run-{service}", f"requested_at={timezone.now().isoformat()}\n")


def _parse_moment(raw: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _write(path: Path, text: str) -> None:
    """Write the file in one move, so the pipeline never reads half of it."""
    directory = path.parent
    if not directory.is_dir():
        raise MailboxUnavailable(f"{directory} does not exist")
    try:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=f".{path.name}.", delete=False
        )
        with handle:
            handle.write(text)
        # The pipeline user must be able to remove and read it: same group, and
        # the directory carries setgid so the group is already right.
        os.chmod(handle.name, 0o660)
        os.replace(handle.name, path)
    except OSError as exc:
        raise MailboxUnavailable(str(exc)) from exc
