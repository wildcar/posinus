import json

import pytest
from django.core.management import CommandError, call_command

from collector.models import OperatorEvent, Source, SourceEndpoint


@pytest.mark.django_db
def test_imports_through_the_form_and_skips_known_domains(tmp_path):
    Source.objects.create(name="Old", base_url="https://old.example/", domain="old.example",
                          status=Source.Status.PAUSED_LOW_YIELD)
    path = tmp_path / "sources.json"
    path.write_text(json.dumps([
        {"name": "New", "base_url": "https://new.example/", "status": "active",
         "rss_url": "https://new.example/feed/", "include_patterns": [r"new\.example/news/"]},
        {"name": "Old again", "base_url": "https://old.example/news/", "rss_url": "https://old.example/rss"},
    ]), encoding="utf-8")

    call_command("importsources", str(path))

    new = Source.objects.get(domain="new.example")
    assert new.status == Source.Status.ACTIVE and new.include_patterns == [r"new\.example/news/"]
    assert SourceEndpoint.objects.get(source=new).url == "https://new.example/feed/"
    assert OperatorEvent.objects.filter(source=new, event_type="source_saved").exists()
    assert Source.objects.get(domain="old.example").status == Source.Status.PAUSED_LOW_YIELD
    assert Source.objects.filter(domain="old.example").count() == 1


@pytest.mark.django_db
def test_default_status_is_probation_and_dry_run_writes_nothing(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps([{"name": "Trial", "base_url": "https://trial.example/"}]), encoding="utf-8")

    call_command("importsources", str(path), "--dry-run")
    assert not Source.objects.exists()

    call_command("importsources", str(path))
    assert Source.objects.get().status == Source.Status.PROBATION


@pytest.mark.django_db
def test_unknown_field_is_an_error(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps([{"name": "X", "base_url": "https://x.example/", "feed": "oops"}]), encoding="utf-8")

    with pytest.raises(CommandError):
        call_command("importsources", str(path))


@pytest.mark.django_db
def test_the_committed_list_is_valid(tmp_path):
    from pathlib import Path

    for listing in Path("sources").glob("*.json"):
        call_command("importsources", str(listing), "--dry-run")
