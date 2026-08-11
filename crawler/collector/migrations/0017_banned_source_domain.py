import django.utils.timezone
from django.db import migrations, models


# The triggers are the backstop the Python checks cannot give: the Shotam
# source that prompted the ban was added by an ad-hoc batch script, not through
# the form or discovery, so only the database itself can refuse the next one.
FORWARD_SQL = """
CREATE TRIGGER sources_banned_domain_insert
BEFORE INSERT ON sources
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM banned_source_domains b
    WHERE lower(NEW.domain) = b.domain
       OR lower(NEW.domain) LIKE '%.' || b.domain
)
BEGIN
    SELECT RAISE(ABORT, 'source domain is banned by the owner');
END;

CREATE TRIGGER sources_banned_domain_update
BEFORE UPDATE ON sources
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM banned_source_domains b
    WHERE lower(NEW.domain) = b.domain
       OR lower(NEW.domain) LIKE '%.' || b.domain
)
BEGIN
    SELECT RAISE(ABORT, 'source domain is banned by the owner');
END;
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS sources_banned_domain_update;
DROP TRIGGER IF EXISTS sources_banned_domain_insert;
"""


def seed_shotam(apps, schema_editor):
    BannedSourceDomain = apps.get_model("collector", "BannedSourceDomain")
    BannedSourceDomain.objects.get_or_create(
        domain="shotam.info",
        defaults={"reason": "Запрет владельца: военная тематика (снятая новость 8949), 2026-08-11."},
    )


def unseed_shotam(apps, schema_editor):
    apps.get_model("collector", "BannedSourceDomain").objects.filter(domain="shotam.info").delete()


class Migration(migrations.Migration):
    dependencies = [("collector", "0016_daypicslot_date_on_the_plate")]

    operations = [
        migrations.CreateModel(
            name="BannedSourceDomain",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("domain", models.CharField(max_length=255, unique=True)),
                ("reason", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={"db_table": "banned_source_domains", "ordering": ["domain"]},
        ),
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
        migrations.RunPython(seed_shotam, unseed_shotam),
    ]
