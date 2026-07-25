from django.apps import AppConfig
from django.db.backends.signals import connection_created


def configure_sqlite(sender, connection, **kwargs):
    if connection.vendor != "sqlite":
        return
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")


class CollectorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "collector"

    def ready(self):
        connection_created.connect(configure_sqlite, dispatch_uid="collector.sqlite_pragmas")

