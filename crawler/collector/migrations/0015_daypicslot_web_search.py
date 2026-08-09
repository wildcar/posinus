# Written by hand on 2026-08-09.

from django.db import migrations, models

# The 0013 texts were written for a chat model without web search («Веб-поиска
# у тебя нет...»). The daypic chat call now runs codex-oauth gpt-5.5 with
# medium reasoning and the native web-search tool on (the owner's call,
# 2026-08-09), so the system prompt sends the model to look up the date's
# events on the web (Russian ones first) instead of forbidding it, and the
# task asks to hide a small cheerful visual surprise for the viewer to find.
# Only a slot whose texts still equal the 0013 seed is updated: an operator's
# edit wins.
OLD_PROMPT = (
    "Подготовь промпт для модели генерации картинок, чтобы она сделала «картину дня»: "
    "отрази праздники и приметы именно этого дня, сезон и атмосферу, без текста и "
    "текстовых табличек на самой картинке. В списке праздников приоритет российским. "
    "Только внизу картинки — небольшая плашка шрифтом среднего размера, в том же стиле, "
    "что и вся картинка, с указанием на русском языке, какие сегодня праздники. "
    "Ориентацию кадра в промпте не задавай: картинка рисуется и вертикальной, и "
    "горизонтальной. Переданный стиль возьми базовым стилем картинки и передай его "
    "в промпт."
)

NEW_PROMPT = (
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

OLD_SYSTEM_PROMPT = (
    "Ты готовишь безопасный и точный промпт для модели генерации изображений и короткое "
    "описание дня для подписи под картинкой. "
    "Веб-поиска у тебя нет: опирайся на переданную дату и своё знание календаря — "
    "государственные, народные и международные праздники этого дня. Если день связан с "
    "конфликтными, трагическими или политически чувствительными событиями, не тащи их в "
    "визуал и описание: оставь только нейтральное настроение дня, сезон, атмосферу и "
    "праздники. Промпт и описание пиши по-русски."
)

NEW_SYSTEM_PROMPT = (
    "Ты готовишь безопасный и точный промпт для модели генерации изображений и короткое "
    "описание дня для подписи под картинкой. "
    "У тебя есть веб-поиск: поищи в интернете, какие праздники и события приходятся на "
    "переданную дату — государственные, народные и международные, с приоритетом "
    "российских. Держись именно этой даты. Если день связан с "
    "конфликтными, трагическими или политически чувствительными событиями, не тащи их в "
    "визуал и описание: оставь только нейтральное настроение дня, сезон, атмосферу и "
    "праздники. Промпт и описание пиши по-русски."
)


def refresh_day_seed(apps, schema_editor):
    slot_model = apps.get_model("collector", "DaypicSlot")
    slot_model.objects.filter(slot="day", prompt=OLD_PROMPT).update(prompt=NEW_PROMPT)
    slot_model.objects.filter(slot="day", system_prompt=OLD_SYSTEM_PROMPT).update(
        system_prompt=NEW_SYSTEM_PROMPT
    )
    # The concrete chat call the owner asked for: codex-oauth gpt-5.5, medium
    # reasoning, web search on. Provider and model fill only blanks — an
    # operator's explicit choice wins; the two new columns are set outright,
    # they did not exist before this migration.
    slot_model.objects.filter(slot="day", chat_provider="").update(chat_provider="codex-oauth")
    slot_model.objects.filter(slot="day", chat_model="").update(chat_model="gpt-5.5")
    slot_model.objects.filter(slot="day").update(
        chat_reasoning_effort="medium", chat_web_search=True
    )


def restore_day_seed(apps, schema_editor):
    slot_model = apps.get_model("collector", "DaypicSlot")
    slot_model.objects.filter(slot="day", prompt=NEW_PROMPT).update(prompt=OLD_PROMPT)
    slot_model.objects.filter(slot="day", system_prompt=NEW_SYSTEM_PROMPT).update(
        system_prompt=OLD_SYSTEM_PROMPT
    )
    slot_model.objects.filter(slot="day", chat_model="gpt-5.5").update(chat_model="")


class Migration(migrations.Migration):

    dependencies = [
        ('collector', '0014_alter_daypicslot_image_size_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='daypicslot',
            name='chat_reasoning_effort',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name='daypicslot',
            name='chat_web_search',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(refresh_day_seed, restore_day_seed),
    ]
