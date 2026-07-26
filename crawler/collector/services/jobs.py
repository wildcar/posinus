"""The background worker for translations.

A translation can take minutes. Inside an HTTP request that meant Nginx cutting
the answer at its own timeout while the model kept working, and an operator
clicking again — paying for a second answer that also went nowhere. So the
button leaves a `TranslationJob` row and returns at once, and this thread does
the work.

A thread rather than a second service: Waitress is multi-threaded and already
running, the queue is one job at a time, and a job queue in a repository whose
deploy is `git pull` plus a restart should not need a broker. If it ever needs
more, the crawler's worker already sleeps a minute between sources.

A restart while a job is running leaves it on `running` with nobody to finish
it, so the thread marks those rows failed on startup instead of leaving the card
promising a translation that will never come.
"""

from __future__ import annotations

import logging
import threading
import time

from django.db import close_old_connections
from django.utils import timezone

log = logging.getLogger(__name__)

POLL_SECONDS = 5
_started = threading.Lock()
_running = False


def claim_next_job():
    """Take the oldest queued job, atomically enough for a single worker."""
    from collector.models import TranslationJob

    job = TranslationJob.objects.filter(status=TranslationJob.Status.QUEUED).order_by("created_at").first()
    if job is None:
        return None
    updated = TranslationJob.objects.filter(pk=job.pk, status=TranslationJob.Status.QUEUED).update(
        status=TranslationJob.Status.RUNNING, started_at=timezone.now()
    )
    if not updated:
        return None
    job.refresh_from_db()
    return job


def run_job(job) -> None:
    """Translate one news item and record how it went."""
    from collector.models import TranslationJob
    from collector.services.translation import translate_news

    try:
        translate_news(job.news_item)
    except Exception as exc:  # the row must always close, whatever failed
        log.exception("Translation job %s failed", job.pk)
        TranslationJob.objects.filter(pk=job.pk).update(
            status=TranslationJob.Status.FAILED, error=str(exc)[:2000], finished_at=timezone.now()
        )
    else:
        TranslationJob.objects.filter(pk=job.pk).update(
            status=TranslationJob.Status.DONE, finished_at=timezone.now()
        )


def reset_interrupted_jobs() -> int:
    """Rows left running by a restart: nobody is going to finish them."""
    from collector.models import TranslationJob

    return TranslationJob.objects.filter(status=TranslationJob.Status.RUNNING).update(
        status=TranslationJob.Status.FAILED,
        error="Перевод прервался при перезапуске сервера. Нажмите ещё раз.",
        finished_at=timezone.now(),
    )


def _loop() -> None:
    reset_interrupted_jobs()
    while True:
        try:
            job = claim_next_job()
            if job is None:
                time.sleep(POLL_SECONDS)
                continue
            run_job(job)
        except Exception:  # a broken loop would silently stop every translation
            log.exception("Translation worker hiccup")
            time.sleep(POLL_SECONDS)
        finally:
            close_old_connections()


def start_worker() -> bool:
    """Start the worker thread once per process. Returns True when it started."""
    global _running
    with _started:
        if _running:
            return False
        _running = True
    thread = threading.Thread(target=_loop, name="translation-worker", daemon=True)
    thread.start()
    log.info("Translation worker started")
    return True
