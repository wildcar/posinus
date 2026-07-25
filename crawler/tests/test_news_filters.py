import pytest
from django.urls import reverse

from collector.models import Source


@pytest.fixture
def corpus(db, make_news, make_review):
    alpha = Source.objects.create(name="Alpha", base_url="https://alpha.example/", domain="alpha.example")
    beta = Source.objects.create(name="Beta", base_url="https://beta.example/", domain="beta.example")
    old_alpha = make_news("Old alpha", alpha, day=10, seed="h1")
    unscored = make_news("Unscored alpha", alpha, day=11, seed="h2")
    new_beta = make_news("New beta", beta, day=12, seed="h3")
    make_review(old_alpha, {"positivity": 8, "negativity": 1}, key="e1")
    make_review(new_beta, {"positivity": 3, "negativity": 6}, key="e2")
    return {"alpha": alpha, "beta": beta, "old_alpha": old_alpha, "unscored": unscored, "new_beta": new_beta}


def _ids(response):
    return [item.pk for item in response.context["items"]]


@pytest.mark.django_db
def test_default_order_is_newest_first(operator, corpus):
    response = operator.get(reverse("news_list"))
    assert _ids(response) == [corpus["new_beta"].pk, corpus["unscored"].pk, corpus["old_alpha"].pk]


@pytest.mark.django_db
def test_sort_by_date_ascending(operator, corpus):
    response = operator.get(reverse("news_list"), {"sort": "date_asc"})
    assert _ids(response) == [corpus["old_alpha"].pk, corpus["unscored"].pk, corpus["new_beta"].pk]


@pytest.mark.django_db
def test_sort_by_source_groups_names(operator, corpus):
    response = operator.get(reverse("news_list"), {"sort": "source_asc"})
    assert _ids(response) == [corpus["unscored"].pk, corpus["old_alpha"].pk, corpus["new_beta"].pk]
    response = operator.get(reverse("news_list"), {"sort": "source_desc"})
    assert _ids(response) == [corpus["new_beta"].pk, corpus["unscored"].pk, corpus["old_alpha"].pk]


@pytest.mark.django_db
def test_filter_by_source(operator, corpus):
    response = operator.get(reverse("news_list"), {"source": str(corpus["alpha"].pk)})
    assert set(_ids(response)) == {corpus["old_alpha"].pk, corpus["unscored"].pk}


@pytest.mark.django_db
def test_decision_filter_still_works(operator, corpus):
    response = operator.get(reverse("news_list"), {"decision": "unreviewed"})
    assert _ids(response) == [corpus["unscored"].pk]


@pytest.mark.django_db
def test_score_lower_threshold(operator, corpus):
    response = operator.get(reverse("news_list"), {"positivity_min": "5"})
    assert _ids(response) == [corpus["old_alpha"].pk]


@pytest.mark.django_db
def test_score_upper_threshold(operator, corpus):
    response = operator.get(reverse("news_list"), {"negativity_max": "2"})
    assert _ids(response) == [corpus["old_alpha"].pk]


@pytest.mark.django_db
def test_score_range_bounds_swap_when_reversed(operator, corpus):
    response = operator.get(reverse("news_list"), {"positivity_min": "4", "positivity_max": "2"})
    assert _ids(response) == [corpus["new_beta"].pk]


@pytest.mark.django_db
def test_full_range_keeps_unscored_news(operator, corpus):
    response = operator.get(reverse("news_list"), {"positivity_min": "0", "positivity_max": "10"})
    assert corpus["unscored"].pk in _ids(response)


@pytest.mark.django_db
def test_narrowed_range_drops_unscored_news(operator, corpus):
    response = operator.get(reverse("news_list"), {"positivity_max": "9"})
    assert corpus["unscored"].pk not in _ids(response)


@pytest.mark.django_db
def test_score_filter_uses_latest_event(operator, corpus, make_review):
    make_review(corpus["old_alpha"], {"positivity": 2, "negativity": 1}, key="e3", decision="not_positive")
    response = operator.get(reverse("news_list"), {"positivity_min": "5"})
    assert _ids(response) == []
    response = operator.get(reverse("news_list"), {"positivity_max": "4"})
    assert set(_ids(response)) == {corpus["old_alpha"].pk, corpus["new_beta"].pk}


@pytest.mark.django_db
def test_invalid_parameters_fall_back_to_defaults(operator, corpus):
    response = operator.get(
        reverse("news_list"),
        {"sort": "bogus", "source": "abc", "positivity_min": "junk", "negativity_max": "99"},
    )
    assert response.status_code == 200
    assert len(_ids(response)) == 3


@pytest.mark.django_db
def test_all_sliders_render_at_once(operator, corpus):
    html = operator.get(reverse("news_list")).content.decode()
    assert "Пороги оценок" in html
    assert html.count('type="range"') == 40
    assert 'name="positivity_min"' in html and 'name="promo_max"' in html
