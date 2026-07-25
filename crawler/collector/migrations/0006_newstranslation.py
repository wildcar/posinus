import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("collector", "0005_latestevaluationscore")]

    operations = [
        migrations.CreateModel(
            name="NewsTranslation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.TextField()),
                ("body_text", models.TextField()),
                ("summary", models.TextField()),
                ("model_id", models.CharField(blank=True, max_length=200)),
                ("generated_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "news_item",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="russian_translation",
                        to="collector.newsitem",
                    ),
                ),
            ],
            options={"db_table": "news_translations"},
        ),
    ]
