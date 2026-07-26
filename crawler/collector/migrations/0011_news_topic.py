import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


# The closed list from the concept (§3.9). Descriptions are not decoration: they
# go into the evaluator's prompt verbatim, and they are the only thing keeping
# «люди и поступки» from swallowing every other rubric.
TOPICS = [
    ("animals", "Животные", "Питомцы, дикая природа, спасение и судьба животных.", True),
    ("science", "Наука и техника", "Открытия, изобретения, космос, инженерные решения.", True),
    ("medicine", "Медицина", "Лечение, здоровье, работа врачей, новые методы.", True),
    ("children", "Дети и школа", "Дети, образование, школьные и студенческие истории.", True),
    ("culture", "Культура", "Искусство, книги, кино, музыка, музеи, история.", True),
    ("sport", "Спорт", "Соревнования, спортсмены, любительский спорт.", True),
    ("city", "Город", "Благоустройство, транспорт, жильё, городские проекты.", True),
    ("people", "Люди и поступки", "Помощь, поступок человека, воссоединение, доброе дело.", True),
    ("nature", "Природа", "Экология, погода, ландшафты, восстановление природы.", True),
    ("oddities", "Курьёзы", "Забавное, странное, необычное происшествие без вреда.", True),
    # Never offered to the model: it is what a news item gets when the answer
    # was unusable, and what the corpus evaluated before rubrics existed carries.
    ("unknown", "Не определена", "Тема не определялась или ответ модели не подошёл.", False),
]

PLACEHOLDER = "unknown"


def seed_topics(apps, schema_editor):
    topic_model = apps.get_model("collector", "Topic")
    for position, (key, title, description, assignable) in enumerate(TOPICS):
        topic_model.objects.update_or_create(
            key=key,
            defaults={
                "title": title,
                "description": description,
                "assignable": assignable,
                "position": position,
            },
        )


def unseed_topics(apps, schema_editor):
    apps.get_model("collector", "Topic").objects.all().delete()


def fill_placeholder(apps, schema_editor):
    """Every already-evaluated news item gets the placeholder rubric.

    Deciding their rubric for real would mean running the model over the whole
    corpus, and the corpus is mostly rejected news whose text the retention pass
    blanks anyway — money spent on stories nobody will ever look at again. The
    evaluator's queue never revisits an item that already carries a verdict, so
    without this row they would have no topic at all and «Состав ленты» could not
    tell «нет темы» from «не считали».
    """
    news_topic = apps.get_model("collector", "NewsTopic")
    review_event = apps.get_model("collector", "ReviewEvent")
    now = django.utils.timezone.now()
    reviewed = (
        review_event.objects.order_by()
        .values_list("news_item_id", flat=True)
        .distinct()
        .iterator(chunk_size=2000)
    )
    batch = []
    for news_id in reviewed:
        batch.append(
            news_topic(
                news_item_id=news_id,
                topic_id=PLACEHOLDER,
                selector_name="migration:0011",
                created_at=now,
            )
        )
        if len(batch) >= 2000:
            news_topic.objects.bulk_create(batch, ignore_conflicts=True)
            batch = []
    if batch:
        news_topic.objects.bulk_create(batch, ignore_conflicts=True)


def drop_placeholder(apps, schema_editor):
    apps.get_model("collector", "NewsTopic").objects.filter(
        selector_name="migration:0011"
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("collector", "0010_translation_job"),
    ]

    operations = [
        migrations.CreateModel(
            name="Topic",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=32, unique=True)),
                ("title", models.CharField(max_length=100)),
                ("description", models.TextField(blank=True)),
                ("assignable", models.BooleanField(default=True)),
                ("position", models.PositiveIntegerField()),
            ],
            options={
                "db_table": "exchange_topic",
                "ordering": ["position", "id"],
            },
        ),
        migrations.CreateModel(
            name="NewsTopic",
            fields=[
                ("news_item", models.OneToOneField(db_column="news_id", on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name="topic_row", serialize=False, to="collector.newsitem")),
                ("selector_name", models.CharField(max_length=200)),
                ("selector_version", models.CharField(blank=True, max_length=200)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("topic", models.ForeignKey(db_column="topic_key", on_delete=django.db.models.deletion.PROTECT, related_name="news", to="collector.topic", to_field="key")),
            ],
            options={
                "db_table": "exchange_news_topic",
            },
        ),
        migrations.RunPython(seed_topics, unseed_topics),
        migrations.RunPython(fill_placeholder, drop_placeholder),
    ]
