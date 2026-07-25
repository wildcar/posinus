"""«Эфир»: what goes out next, what went out, and which platform is broken."""

import sqlite3
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from collector.models import Source
from collector.services import broadcast

SCHEMA = """
CREATE TABLE prepared_item (
    news_id INTEGER PRIMARY KEY, status TEXT NOT NULL, retold_title TEXT,
    retold_body_md TEXT, model_id TEXT, prepared_at TEXT, published_at TEXT, error TEXT
);
CREATE TABLE illustration (
    id INTEGER PRIMARY KEY AUTOINCREMENT, news_id INTEGER NOT NULL, position INTEGER NOT NULL,
    file_path TEXT NOT NULL, caption TEXT, source_url TEXT, downloaded_at TEXT
);
CREATE TABLE publication (
    news_id INTEGER NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL, url TEXT,
    error TEXT, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT,
    PRIMARY KEY (news_id, platform)
);
CREATE TABLE service_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL, status TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, counters TEXT NOT NULL DEFAULT '{}',
    config TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT ''
);
"""


@pytest.fixture
def source(db):
    return Source.objects.create(name="Alpha", base_url="https://alpha.example/", domain="alpha.example")


@pytest.fixture
def pipeline(settings, tmp_path):
    path = tmp_path / "evaluator.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    settings.POSINUS_PIPELINE_DB_PATH = str(path)

    def run(sql, params=()):
        con = sqlite3.connect(path)
        con.execute(sql, params)
        con.commit()
        con.close()

    return run


@pytest.mark.django_db
def test_queue_carries_age_strength_and_expected_time(operator, source, make_news, make_review, pipeline):
    strong = make_news("Strong one", source, day=10, seed="q1")
    make_review(strong, {"positivity": 9, "uniqueness": 9, "interestingness": 8}, key="q1")
    plain = make_news("Plain one", source, day=10, seed="q2")
    make_review(plain, {"positivity": 8, "uniqueness": 2, "interestingness": 3}, key="q2")
    for item, when in ((strong, "2026-07-24T10:00:00+00:00"), (plain, "2026-07-24T09:00:00+00:00")):
        pipeline(
            "INSERT INTO prepared_item (news_id, status, retold_title, prepared_at) VALUES (?, 'prepared', ?, ?)",
            (item.pk, f"Пересказ {item.pk}", when),
        )
    pipeline(
        "INSERT INTO service_run (service, status, started_at, config) VALUES ('publisher', 'ok', ?, ?)",
        (timezone.now().isoformat(), '{"min_interval_minutes": 60, "window": ""}'),
    )

    items, config = broadcast.queue()

    assert [item.news_id for item in items] == [plain.pk, strong.pk]  # preparation order, as the publisher does
    assert items[1].strength > items[0].strength                      # but the strong one is stronger
    assert config["min_interval_minutes"] == 60
    assert items[0].expected_at < items[1].expected_at
    assert (items[1].expected_at - items[0].expected_at) == timedelta(minutes=60)


@pytest.mark.django_db
def test_strength_prefers_the_best_strong_side():
    assert broadcast.strength({"positivity": 9, "uniqueness": 9, "interestingness": 8}) > broadcast.strength(
        {"positivity": 9, "uniqueness": 2, "interestingness": 3}
    )
    assert broadcast.strength({}) == 0.0


@pytest.mark.django_db
def test_expected_time_waits_for_the_window(operator, source, make_news, pipeline):
    item = make_news("Night one", source, day=10, seed="w1")
    pipeline(
        "INSERT INTO prepared_item (news_id, status, retold_title, prepared_at) VALUES (?, 'prepared', 'T', ?)",
        (item.pk, "2026-07-24T10:00:00+00:00"),
    )
    pipeline(
        "INSERT INTO service_run (service, status, started_at, config) VALUES ('publisher', 'ok', ?, ?)",
        (timezone.now().isoformat(), '{"min_interval_minutes": 60, "window": "08:00-22:00 Europe/Moscow"}'),
    )

    items, _ = broadcast.queue()

    from zoneinfo import ZoneInfo

    local = items[0].expected_at.astimezone(ZoneInfo("Europe/Moscow"))
    assert 8 <= local.hour < 22


@pytest.mark.django_db
def test_published_feed_links_every_platform(operator, source, make_news, pipeline):
    item = make_news("Published one", source, day=10, seed="p1")
    pipeline(
        "INSERT INTO prepared_item (news_id, status, retold_title, published_at) "
        "VALUES (?, 'published', 'Кошка вернулась домой', '2026-07-25T11:20:00+00:00')",
        (item.pk,),
    )
    for platform, status, url in (("telegram", "ok", "https://t.me/posinus/7"), ("vk", "error", "")):
        pipeline(
            "INSERT INTO publication (news_id, platform, status, url, error, attempts, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, '2026-07-25T11:21:00+00:00')",
            (item.pk, platform, status, url, "" if status == "ok" else "ключ доступа не подошёл"),
        )

    html = operator.get(reverse("broadcast"), {"tab": "published"}).content.decode()

    assert "Кошка вернулась домой" in html
    assert "https://t.me/posinus/7" in html
    assert "ВКонтакте: не вышло" in html


@pytest.mark.django_db
def test_platform_cards_count_successes_and_failures(operator, pipeline):
    pipeline("INSERT INTO publication (news_id, platform, status, url, attempts, updated_at) "
             "VALUES (1, 'telegram', 'ok', 'https://t.me/x/1', 1, '2026-07-25T10:00:00+00:00')")
    pipeline("INSERT INTO publication (news_id, platform, status, error, attempts, updated_at) "
             "VALUES (2, 'vk', 'error', 'код 214: сообщество не разрешает публикацию', 8, '2026-07-25T10:05:00+00:00')")

    cards = {card.platform: card for card in broadcast.platforms()}

    assert cards["telegram"].ok_count == 1 and cards["telegram"].error_count == 0
    assert cards["vk"].error_count == 1 and cards["vk"].given_up == 1
    assert "214" in cards["vk"].last_error


@pytest.mark.django_db
def test_broadcast_page_survives_a_missing_pipeline(operator, settings, tmp_path):
    settings.POSINUS_PIPELINE_DB_PATH = str(tmp_path / "absent.sqlite3")

    response = operator.get(reverse("broadcast"))

    assert response.status_code == 200
    assert "Нет связи с базой конвейера" in response.content.decode()


@pytest.mark.django_db
def test_failed_preparations_are_listed_as_temporary(operator, source, make_news, pipeline):
    item = make_news("Broken one", source, day=10, seed="f1")
    pipeline(
        "INSERT INTO prepared_item (news_id, status, retold_title, error, prepared_at) "
        "VALUES (?, 'error', 'T', 'модель не ответила', '2026-07-25T10:00:00+00:00')",
        (item.pk,),
    )

    html = operator.get(reverse("broadcast")).content.decode()

    assert "Не удалось подготовить" in html
    assert "модель не ответила" in html
    assert "вернутся в очередь сами" in html


@pytest.mark.django_db
def test_dashboard_counters_carry_a_typical_range(operator, source, make_news, pipeline):
    from collector.services import console

    for day, seed in ((10, "n1"), (10, "n2"), (11, "n3")):
        make_news(f"Old {seed}", source, day=day, seed=seed)
    make_news("Today", source, day=10, seed="today")

    numbers = {row.title: row for row in console.today_counters()}

    assert "Собрано сегодня" in numbers
    assert numbers["Ждут оценки"].value == 4  # nothing reviewed yet
    assert numbers["Отобрано сегодня"].url.endswith("decision=positive")


@pytest.mark.django_db
def test_attention_names_a_broken_platform_and_a_looping_preparation(operator, pipeline):
    pipeline("INSERT INTO publication (news_id, platform, status, error, attempts, updated_at) "
             "VALUES (1, 'vk', 'error', 'ключ доступа не подошёл', 4, '2026-07-25T10:00:00+00:00')")
    pipeline("INSERT INTO prepared_item (news_id, status, retold_title, error) "
             "VALUES (2, 'error', 'Упрямая новость', 'модель не ответила')")

    from collector.services import console

    problems = [row.text for row in console.attention()]

    assert any("ВКонтакте не принимает посты" in text for text in problems)
    assert any("Упрямая новость" in text for text in problems)


@pytest.mark.django_db
def test_attention_is_empty_when_nothing_is_wrong(operator, pipeline):
    from collector.services import console

    assert console.attention() == []
    assert "Всё в порядке" in operator.get(reverse("dashboard")).content.decode()


@pytest.mark.django_db
def test_feed_mix_counts_source_shares(operator, source, make_news, pipeline):
    other = Source.objects.create(name="Beta", base_url="https://beta.example/", domain="beta.example")
    first = make_news("One", source, day=10, seed="m1")
    second = make_news("Two", source, day=10, seed="m2")
    third = make_news("Three", other, day=10, seed="m3")
    for item in (first, second, third):
        pipeline(
            "INSERT INTO prepared_item (news_id, status, retold_title, published_at) VALUES (?, 'published', 'T', ?)",
            (item.pk, timezone.now().isoformat()),
        )

    from collector.services import console

    mix = console.feed_mix()

    assert mix["total"] == 3
    assert mix["sources"][0] == {"name": "Alpha", "count": 2, "share": 67}
