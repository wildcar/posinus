from django.db import migrations


FORWARD_SQL = """
CREATE VIEW exchange_latest_evaluation_scores AS
SELECT
    r.news_id,
    r.selector_name,
    r.id AS review_event_id,
    r.created_at,
    s.characteristic_key,
    s.value
FROM exchange_latest_reviews r
JOIN exchange_evaluation_scores s ON s.review_event_id = r.id;

CREATE TRIGGER exchange_evaluation_scores_no_update
BEFORE UPDATE ON exchange_evaluation_scores
BEGIN
    SELECT RAISE(ABORT, 'exchange_evaluation_scores is append-only');
END;

CREATE TRIGGER exchange_evaluation_scores_no_delete
BEFORE DELETE ON exchange_evaluation_scores
BEGIN
    SELECT RAISE(ABORT, 'exchange_evaluation_scores is append-only');
END;
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS exchange_evaluation_scores_no_delete;
DROP TRIGGER IF EXISTS exchange_evaluation_scores_no_update;
DROP VIEW IF EXISTS exchange_latest_evaluation_scores;
"""

LOWER = "lower_bound"
UPPER = "upper_bound"

# Evaluation-axis set v1 from the News Evaluator specification
# (~/repo/positive-news-evaluator/AGENTS/SPEC.md). Every axis is an
# independent integer 0-10; 0 also means "not applicable".
CHARACTERISTICS = [
    # (key, category, title, description, anchor_low, anchor_high, direction)
    ("positivity", "Тональность", "Позитивность",
     "Насколько новость в целом вызывает положительные эмоции",
     "мрачная или нейтральная", "чистая радость", LOWER),
    ("negativity", "Тональность", "Негативность",
     "Доля страдания, ущерба, потерь, конфликта в содержании",
     "негатива нет вовсе", "тяжёлый негатив", UPPER),
    ("heartwarming", "Эмоциональный отклик", "Трогательность",
     "Душевное тепло: доброта, забота, взаимовыручка",
     "не трогает", "«до слёз»", LOWER),
    ("cuteness", "Эмоциональный отклик", "Милота",
     "Животные, детёныши, уютные детали",
     "ничего милого", "максимально мило", LOWER),
    ("humor", "Эмоциональный отклик", "Курьёзность",
     "Забавность, желание улыбнуться и пересказать как анекдот",
     "совсем не смешно", "уморительно", LOWER),
    ("pride_humanity", "Эмоциональный отклик", "Гордость за человечество",
     "Достижения науки, прогресса, солидарности людей",
     "нет повода", "триумф человечества", LOWER),
    ("pride_russia", "Эмоциональный отклик", "Гордость за Россию",
     "Российские достижения, люди, города и регионы",
     "не про Россию / нет повода", "безусловная гордость", LOWER),
    ("heroism", "Эмоциональный отклик", "Героизм",
     "Подвиг, самопожертвование, спасение",
     "нет героики", "настоящий подвиг", LOWER),
    ("inspiration", "Эмоциональный отклик", "Вдохновляющая сила",
     "Мотивирует действовать: «и я так могу/хочу»",
     "не мотивирует", "немедленно хочется делать", LOWER),
    ("beauty", "Эмоциональный отклик", "Эстетика",
     "Красота места, события, объекта по описанию",
     "ничего красивого", "очень красиво", LOWER),
    ("interestingness", "Внимание и новизна", "Интересность",
     "Насколько захватывает и удерживает внимание (скучная = 0–2)",
     "скучная", "не оторваться", LOWER),
    ("surprise", "Внимание и новизна", "Неожиданность",
     "Удивляет ли факт или исход события",
     "всё предсказуемо", "полная неожиданность", LOWER),
    ("uniqueness", "Внимание и новизна", "Необычность",
     "Редкость, странность самого явления («такого не бывает»)",
     "рядовое явление", "уникальный случай", LOWER),
    ("memorability", "Внимание и новизна", "Запоминаемость",
     "Вспомнится ли через неделю, захочется ли пересказать",
     "забудется сразу", "врежется в память", LOWER),
    ("importance", "Значимость и практика", "Важность",
     "Общественная значимость, влияние на жизнь людей",
     "ни на что не влияет", "меняет жизнь многих", LOWER),
    ("impact_scale", "Значимость и практика", "Масштаб",
     "Широта охвата: двор → город → страна → мир",
     "один человек/двор", "весь мир", LOWER),
    ("usefulness", "Значимость и практика", "Полезность",
     "Практическая ценность: можно сходить, применить, воспользоваться",
     "ничего практического", "прямое руководство к действию", LOWER),
    ("clickbait", "Служебные фильтры", "Кликбейтность",
     "Сенсационность, заголовок обещает больше, чем есть в тексте",
     "честная подача", "чистый кликбейт", UPPER),
    ("controversy", "Служебные фильтры", "Конфликтность",
     "Политизированность, поляризация, распри",
     "бесспорная тема", "остро конфликтная", UPPER),
    ("promo", "Служебные фильтры", "Рекламность",
     "Пресс-релиз, скрытая реклама, продвижение товара/услуги",
     "обычная новость", "откровенная реклама", UPPER),
]


def seed_characteristics(apps, schema_editor):
    characteristic = apps.get_model("collector", "EvaluationCharacteristic")
    for position, (key, category, title, description, anchor_low, anchor_high, direction) in enumerate(CHARACTERISTICS, start=1):
        characteristic.objects.update_or_create(
            key=key,
            defaults={
                "category": category,
                "title": title,
                "description": description,
                "anchor_low": anchor_low,
                "anchor_high": anchor_high,
                "threshold_direction": direction,
                "position": position,
            },
        )


def unseed_characteristics(apps, schema_editor):
    characteristic = apps.get_model("collector", "EvaluationCharacteristic")
    characteristic.objects.filter(key__in=[row[0] for row in CHARACTERISTICS]).delete()


class Migration(migrations.Migration):
    dependencies = [("collector", "0003_evaluation_models")]
    operations = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
        migrations.RunPython(seed_characteristics, unseed_characteristics),
    ]
