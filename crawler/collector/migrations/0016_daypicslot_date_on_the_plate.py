# Written by hand on 2026-08-09.

from django.db import migrations

# The plate at the bottom listed the holidays and nothing else, so the picture
# never said which day it was — the date lives only in the post title. The
# owner asked for it on the plate itself, first: «9 августа • День строителя •
# …». Only a slot whose task still equals the 0015 text is updated: an
# operator's edit wins.
OLD_PROMPT = (
    "Подготовь промпт для модели генерации картинок, чтобы она сделала «картину дня»: "
    "отрази праздники и приметы именно этого дня, сезон и атмосферу, без текста и "
    "текстовых табличек на самой картинке. В списке праздников приоритет российским. "
    "Только внизу картинки — небольшая плашка шрифтом среднего размера, в том же стиле, "
    "что и вся картинка, с указанием на русском языке, какие сегодня праздники. "
    "Добавь на картинку небольшой весёлый визуальный сюрприз — деталь, которую зрителю "
    "будет приятно поискать и найти. "
    "Ориентацию кадра в промпте не задавай: картинка рисуется и вертикальной, и "
    "горизонтальной. Переданный стиль возьми базовым стилем картинки и передай его "
    "в промпт."
)

NEW_PROMPT = (
    "Подготовь промпт для модели генерации картинок, чтобы она сделала «картину дня»: "
    "отрази праздники и приметы именно этого дня, сезон и атмосферу, без текста и "
    "текстовых табличек на самой картинке. В списке праздников приоритет российским. "
    "Только внизу картинки — небольшая плашка шрифтом среднего размера, в том же стиле, "
    "что и вся картинка: сначала дата по-русски числом и месяцем («9 августа»), затем "
    "через разделитель — какие сегодня праздники. "
    "Добавь на картинку небольшой весёлый визуальный сюрприз — деталь, которую зрителю "
    "будет приятно поискать и найти. "
    "Ориентацию кадра в промпте не задавай: картинка рисуется и вертикальной, и "
    "горизонтальной. Переданный стиль возьми базовым стилем картинки и передай его "
    "в промпт."
)


def put_the_date_on_the_plate(apps, schema_editor):
    slot_model = apps.get_model("collector", "DaypicSlot")
    slot_model.objects.filter(slot="day", prompt=OLD_PROMPT).update(prompt=NEW_PROMPT)


def take_it_off(apps, schema_editor):
    slot_model = apps.get_model("collector", "DaypicSlot")
    slot_model.objects.filter(slot="day", prompt=NEW_PROMPT).update(prompt=OLD_PROMPT)


class Migration(migrations.Migration):

    dependencies = [
        ('collector', '0015_daypicslot_web_search'),
    ]

    operations = [
        migrations.RunPython(put_the_date_on_the_plate, take_it_off),
    ]
