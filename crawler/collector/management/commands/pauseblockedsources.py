from django.core.management.base import BaseCommand

from collector.models import OperatorEvent, Source
from collector.services.maintenance import is_blocked_discovery_domain


class Command(BaseCommand):
    help = (
        "Pause probation sources whose domain is on the discovery blocklist. "
        "Discovery no longer creates them, but sources accepted before a "
        "blocklist entry existed keep occupying the crawl queue."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        candidates = Source.objects.filter(
            status__in=[Source.Status.PROBATION, Source.Status.PROBATION_WAITING]
        ).order_by("domain")
        paused = []
        for source in candidates:
            if not is_blocked_discovery_domain(source.domain):
                continue
            if not options["dry_run"]:
                source.status = Source.Status.PAUSED_MANUAL
                source.save(update_fields=["status", "updated_at"])
                OperatorEvent.objects.create(
                    event_type="source_status",
                    source=source,
                    message="Остановлен: домен из блок-листа дискавери",
                    details={"domain": source.domain},
                )
            paused.append(source.domain)
        verb = "would pause" if options["dry_run"] else "paused"
        self.stdout.write(f"{verb} {len(paused)}: {', '.join(paused)}")
