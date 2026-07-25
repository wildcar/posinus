import pytest
from django.urls import reverse

from collector.models import Source


@pytest.fixture
def source(db):
    return Source.objects.create(name="Alpha", base_url="https://alpha.example/", domain="alpha.example")


@pytest.mark.django_db
def test_detail_shows_score_heat_cells(operator, source, make_news, make_review):
    item = make_news("Scored news", source, day=10, seed="d1")
    make_review(item, {"positivity": 8, "negativity": 1, "cuteness": 0}, key="d1-e1")
    html = operator.get(reverse("news_detail", args=[item.pk])).content.decode()
    assert "Баллы по характеристикам" in html
    assert "Отборщик: evaluator" in html
    assert 'class="heat heat-8"' in html
    assert "Позитивность" in html and "Милота" in html
    # Category headings from the seeded characteristic set
    assert "Тональность" in html and "Эмоциональный отклик" in html


@pytest.mark.django_db
def test_detail_orders_scores_by_characteristic_position(operator, source, make_news, make_review):
    item = make_news("Ordered news", source, day=10, seed="d2")
    make_review(item, {"promo": 3, "positivity": 7}, key="d2-e1")
    html = operator.get(reverse("news_detail", args=[item.pk])).content.decode()
    assert html.index("Позитивность") < html.index("Рекламность")


@pytest.mark.django_db
def test_detail_shows_only_latest_event_scores(operator, source, make_news, make_review):
    item = make_news("Corrected news", source, day=10, seed="d3")
    make_review(item, {"positivity": 8}, key="d3-e1")
    make_review(item, {"positivity": 2}, key="d3-e2", decision="not_positive")
    html = operator.get(reverse("news_detail", args=[item.pk])).content.decode()
    assert 'class="heat heat-2"' in html
    assert 'class="heat heat-8"' not in html


@pytest.mark.django_db
def test_detail_shows_block_per_selector(operator, source, make_news, make_review):
    item = make_news("Twice scored news", source, day=10, seed="d4")
    make_review(item, {"positivity": 8}, key="d4-e1", selector="evaluator")
    make_review(item, {"positivity": 5}, key="d4-e2", selector="second")
    html = operator.get(reverse("news_detail", args=[item.pk])).content.decode()
    assert html.count("Отборщик:") == 2
    assert "Отборщик: evaluator" in html and "Отборщик: second" in html


@pytest.mark.django_db
def test_detail_without_scores_shows_placeholder(operator, source, make_news):
    item = make_news("Plain news", source, day=10, seed="d5")
    html = operator.get(reverse("news_detail", args=[item.pk])).content.decode()
    assert "Оценок по характеристикам пока нет" in html
    assert 'class="heat heat-' not in html
