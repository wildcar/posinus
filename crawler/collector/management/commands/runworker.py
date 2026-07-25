import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from collector.models import Source, SourceRuntimeState
from collector.models import OperatorEvent
from collector.services.crawler import crawl_source, lease_next_source
from collector.services.maintenance import create_backup, evaluate_sources, process_positive_discovery, purge_old_content


@contextmanager
def single_worker_lock():
    path = Path(settings.DB_PATH).with_suffix(".worker.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CommandError("Another crawler worker is already running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise CommandError("Another crawler worker is already running") from exc
        yield
    finally:
        handle.close()


class Command(BaseCommand):
    help = "Run the single SQLite crawler worker"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--sleep", type=int, default=60)

    def handle(self, *args, **options):
        for source in Source.objects.all():
            SourceRuntimeState.objects.get_or_create(source=source)
        owner = f"{socket.gethostname()}:{os.getpid()}"
        latest_backup = OperatorEvent.objects.filter(event_type="backup_success").first()
        last_maintenance = timezone.localdate(latest_backup.created_at) if latest_backup else None
        with single_worker_lock():
            while True:
                state = lease_next_source(owner)
                if state:
                    crawl_source(state.source)
                today = timezone.localdate()
                if last_maintenance != today:
                    evaluate_sources()
                    process_positive_discovery()
                    purge_old_content()
                    create_backup()
                    last_maintenance = today
                if options["once"]:
                    break
                time.sleep(options["sleep"] if not state else 1)
