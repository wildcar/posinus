from django.core.management.base import BaseCommand
from collector.services.maintenance import (
    create_backup,
    evaluate_sources,
    process_positive_discovery,
    purge_old_content,
    purge_rejected_content,
)


class Command(BaseCommand):
    help = "Run daily source evaluation, discovery, retention and backup"

    def handle(self, *args, **kwargs):
        evaluate_sources()
        process_positive_discovery()
        rejected = purge_rejected_content()
        count = purge_old_content()
        backup = create_backup()
        self.stdout.write(self.style.SUCCESS(f"Maintenance complete: rejected={rejected}, purged={count}, backup={backup}"))

