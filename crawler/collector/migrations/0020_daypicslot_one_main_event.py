# Written by hand on 2026-10-19.

from django.db import migrations

# The picture tried to fit every holiday and event of the day at once and came
# out overloaded, with too many details. The owner asked (2026-10-19) for one
# main event: the model picks it, and both the picture and the plate show only
# it. The description under the picture still names the rest of the day. The
# hidden «surprise» detail goes away too. Only a slot whose task still equals
# the 0016 text is updated: an operator's edit wins.
OLD_PROMPT = (
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

NEW_PROMPT = (
    "Подготовь промпт для модели генерации картинок, чтобы она сделала «картину дня». "
    "Из всех праздников и событий именно этого дня выбери одно главное, с приоритетом "
    "российским, и построй картинку только на нём: один ясный сюжет, простая композиция, "
    "немного деталей. Остальные события дня на картинку не переноси. Без текста и "
    "текстовых табличек на самой картинке. "
    "Только внизу картинки — небольшая плашка шрифтом среднего размера, в том же стиле, "
    "что и вся картинка: сначала дата по-русски числом и месяцем («9 августа»), затем "
    "через разделитель — название этого главного события, и больше ничего. "
    "В описании дня (description) назови главное событие и упомяни остальные праздники "
    "и события этого дня. "
    "Ориентацию кадра в промпте не задавай: картинка рисуется и вертикальной, и "
    "горизонтальной. Переданный стиль возьми базовым стилем картинки и передай его "
    "в промпт."
)


def one_main_event(apps, schema_editor):
    slot_model = apps.get_model("collector", "DaypicSlot")
    slot_model.objects.filter(slot="day", prompt=OLD_PROMPT).update(prompt=NEW_PROMPT)


def back_to_all_events(apps, schema_editor):
    slot_model = apps.get_model("collector", "DaypicSlot")
    slot_model.objects.filter(slot="day", prompt=NEW_PROMPT).update(prompt=OLD_PROMPT)


class Migration(migrations.Migration):

    dependencies = [
        ('collector', '0019_publication_order_russia'),
    ]

    operations = [
        migrations.RunPython(one_main_event, back_to_all_events),
    ]
