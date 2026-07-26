"""The rubric of a news item: the closed list, the filter, the feed mix.

Rubrics exist for one reason — three rescued dogs in a row read as a robot, and
nothing else in the system can see that. So the tests here are about what the
operator can actually find out, not about the table.
"""

import pytest
from django.urls import reverse

from collector.models import NewsTopic, Topic


@pytest.fixture
def source(db):
    from collector.models import Source

    return Source.objects.create(name="Alpha", base_url="https://alpha.example/", domain="alpha.example")


def set_topic(item, key, selector="news-evaluator"):
    return NewsTopic.objects.create(news_item=item, topic_id=key, selector_name=selector)


@pytest.mark.django_db
def test_the_rubric_list_is_seeded_and_closed():
    keys = set(Topic.objects.values_list("key", flat=True))
    assert {"animals", "science", "people", "unknown"} <= keys
    assert Topic.objects.count() == 11
    # The placeholder is ours to write, never the model's to choose.
    assert Topic.objects.get(key="unknown").assignable is False
    assert Topic.objects.filter(assignable=True).count() == 10


@pytest.mark.django_db
def test_the_flow_shows_the_topic_and_filters_by_it(operator, source, make_news):
    dogs = make_news("Спасли собаку", source, day=10, seed="topic-dogs")
    rocket = make_news("Запустили ракету", source, day=11, seed="topic-rocket")
    set_topic(dogs, "animals")
    set_topic(rocket, "science")

    everything = operator.get(reverse("news_list")).content.decode()
    assert "Животные" in everything and "Наука и техника" in everything

    only_animals = operator.get(reverse("news_list"), {"topic": "animals"}).content.decode()
    assert "Спасли собаку" in only_animals
    assert "Запустили ракету" not in only_animals


@pytest.mark.django_db
def test_an_unknown_rubric_in_the_query_is_ignored_not_an_error(operator, source, make_news):
    """A hand-typed address must not hide the whole corpus behind an empty page."""
    make_news("Видна всегда", source, day=10, seed="topic-bad-filter")

    response = operator.get(reverse("news_list"), {"topic": "котики"})

    assert response.status_code == 200
    assert "Видна всегда" in response.content.decode()


@pytest.mark.django_db
def test_news_without_a_topic_says_so_rather_than_lying(operator, source, make_news):
    item = make_news("Без темы", source, day=10, seed="topic-none")

    response = operator.get(reverse("news_detail", args=[item.pk]))

    assert "не определена" in response.content.decode()


@pytest.mark.django_db
def test_the_placeholder_is_a_topic_you_can_filter_by(operator, source, make_news):
    """The corpus evaluated before rubrics existed carries it, and it has to be findable."""
    old = make_news("Оценена до рубрик", source, day=10, seed="topic-placeholder")
    set_topic(old, "unknown", selector="migration:0011")

    response = operator.get(reverse("news_list"), {"topic": "unknown"})

    assert "Оценена до рубрик" in response.content.decode()


@pytest.mark.django_db
def test_feed_mix_counts_rubric_shares(monkeypatch, source, make_news):
    from collector.services import console

    dogs = make_news("Собака", source, day=10, seed="mix-dogs")
    cats = make_news("Кошка", source, day=11, seed="mix-cats")
    rocket = make_news("Ракета", source, day=12, seed="mix-rocket")
    for item in (dogs, cats):
        set_topic(item, "animals")
    set_topic(rocket, "science")

    monkeypatch.setattr(
        console, "fetch_all", lambda *args, **kwargs: [{"news_id": item.pk} for item in (dogs, cats, rocket)]
    )
    mix = console.feed_mix(days=30)

    assert mix["total"] == 3
    assert mix["topics"][0] == {"key": "animals", "name": "Животные", "count": 2, "share": 67}
    assert {row["key"] for row in mix["topics"]} == {"animals", "science"}
