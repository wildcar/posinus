import json

from django.core.management.base import BaseCommand, CommandError

from collector.forms import SourceForm
from collector.models import Source

FIELDS = ("name", "base_url", "status", "interval_minutes", "download_delay_seconds", "rss_url",
          "sitemap_url", "section_url", "include_patterns", "exclude_patterns")
DEFAULTS = {"status": Source.Status.PROBATION, "interval_minutes": 60, "download_delay_seconds": 1.0}


class Command(BaseCommand):
    help = (
        "Add sources from a JSON list through the same form the operator uses, "
        "so the banned-domain check, endpoints and the audit event are identical. "
        "A source whose base URL or domain is already known is skipped, whatever "
        "its status: a paused source stays paused."
    )

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        try:
            with open(options["path"], encoding="utf-8") as handle:
                entries = json.load(handle)
        except (OSError, ValueError) as exc:
            raise CommandError(f"cannot read {options['path']}: {exc}") from exc
        added, skipped, failed = [], [], []
        for entry in entries:
            unknown = set(entry) - set(FIELDS)
            if unknown:
                raise CommandError(f"{entry.get('name')}: unknown fields {sorted(unknown)}")
            data = {**DEFAULTS, **entry}
            data["include_patterns_text"] = "\n".join(data.pop("include_patterns", []))
            data["exclude_patterns_text"] = "\n".join(data.pop("exclude_patterns", []))
            form = SourceForm(data=data)
            if not form.is_valid():
                failed.append(f"{entry.get('name')}: {form.errors.as_text()}")
                continue
            domain = form.cleaned_data["base_url"].split("/")[2].lower()
            if Source.objects.filter(base_url=form.cleaned_data["base_url"]).exists() or Source.objects.filter(domain=domain).exists():
                skipped.append(domain)
                continue
            if not options["dry_run"]:
                form.save()
            added.append(domain)
        verb = "would add" if options["dry_run"] else "added"
        self.stdout.write(f"{verb} {len(added)}: {', '.join(added)}")
        if skipped:
            self.stdout.write(f"skipped {len(skipped)} already known: {', '.join(skipped)}")
        for line in failed:
            self.stderr.write(f"rejected {line}")
        if failed:
            raise CommandError(f"{len(failed)} entries rejected")
