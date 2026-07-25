import pytest
from django.utils import timezone

from collector.models import NewsItem, Source
from collector.services.ingest import ingest_article


@pytest.mark.django_db
def test_exact_duplicate_becomes_second_occurrence():
    first = Source.objects.create(name="One", base_url="https://one.example/", domain="one.example")
    second = Source.objects.create(name="Two", base_url="https://two.example/", domain="two.example")
    body = "A useful and positive article with enough original content. " * 20
    item1, _, created1 = ingest_article(source=first, url="https://one.example/a?utm_source=x", title="New park", body=body, language="en")
    item2, _, created2 = ingest_article(source=second, url="https://two.example/copy", title="New park", body=body, language="en")
    assert created1 and created2
    assert item1.pk == item2.pk
    assert item1.occurrences.count() == 2
    assert NewsItem.objects.count() == 1


@pytest.mark.django_db
def test_translations_are_not_merged():
    source = Source.objects.create(name="One", base_url="https://one.example/", domain="one.example")
    english, _, _ = ingest_article(source=source, url="https://one.example/en", title="A school opened", body="A new school opened for children. " * 20, language="en")
    russian, _, _ = ingest_article(source=source, url="https://one.example/ru", title="Открылась школа", body="Для детей открылась новая современная школа. " * 20, language="ru")
    assert english.pk != russian.pk

