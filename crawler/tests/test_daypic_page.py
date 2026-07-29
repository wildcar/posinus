"""«Картина дня»: the settings the operator edits and the gallery the pipeline fills."""

import sqlite3

import pytest
from django.urls import reverse

from collector.models import DaypicSlot, OperatorEvent


@pytest.fixture
def pipeline_db(settings, tmp_path):
    """A pipeline database with the daypic tables and one published issue."""
    path = tmp_path / "evaluator.sqlite3"
    pictures = tmp_path / "daypic"
    pictures.mkdir()
    picture = pictures / "2026-07-29-day.png"
    picture.write_bytes(b"\x89PNG picture bytes")
    wide = pictures / "2026-07-29-day-wide.png"
    wide.write_bytes(b"\x89PNG wide bytes")
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE daypic_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, slot TEXT, status TEXT,
            title TEXT, style TEXT, prompt TEXT, caption TEXT, file_path TEXT,
            file_path_wide TEXT, prompt_model_id TEXT,
            image_model_id TEXT, attempts INTEGER DEFAULT 0, error TEXT,
            generated_at TEXT, published_at TEXT, file_purged_at TEXT
        );
        CREATE TABLE daypic_publication (
            item_id INTEGER, platform TEXT, status TEXT, url TEXT, error TEXT,
            attempts INTEGER DEFAULT 0, updated_at TEXT
        );
        """
    )
    con.execute(
        "INSERT INTO daypic_item (day, slot, status, style, prompt, caption, file_path, "
        "file_path_wide, image_model_id, published_at) "
        "VALUES ('2026-07-29', 'day', 'published', 'low-poly', 'промпт', "
        "'Сегодня день дружбы.', ?, ?, 'gpt-image-2', '2026-07-29T05:10:00+00:00')",
        (str(picture), str(wide)),
    )
    con.execute(
        "INSERT INTO daypic_publication (item_id, platform, status, url, attempts, updated_at) "
        "VALUES (1, 'telegram', 'ok', 'https://t.me/posinus/9', 1, '2026-07-29T05:11:00+00:00')"
    )
    con.commit()
    con.close()
    settings.POSINUS_PIPELINE_DB_PATH = str(path)
    settings.POSINUS_DAYPIC_DIR = str(pictures)
    return path


@pytest.mark.django_db
def test_the_migration_seeds_the_day_slot_switched_off():
    slot = DaypicSlot.objects.get(pk="day")
    assert not slot.enabled
    assert "картину дня" in slot.prompt
    assert len(slot.styles.splitlines()) == 31  # one per day of the longest month


@pytest.mark.django_db
def test_the_page_shows_the_slot_and_survives_a_missing_pipeline_db(operator, settings, tmp_path):
    settings.POSINUS_PIPELINE_DB_PATH = str(tmp_path / "absent.sqlite3")

    response = operator.get(reverse("daypic"))

    assert response.status_code == 200
    assert "Слот «day»".encode() in response.content
    assert response.context["pipeline_error"]


@pytest.mark.django_db
def test_saving_a_slot_updates_it_and_logs_the_operator_event(operator):
    response = operator.post(
        reverse("daypic_save", args=["day"]),
        {
            "day-enabled": "on", "day-title": "Картина дня", "day-generate_at": "9:30",
            "day-prompt": "новое задание", "day-system_prompt": "с", "day-styles": "realistic",
            "day-chat_provider": "", "day-chat_model": "", "day-image_provider": "",
            "day-image_model": "", "day-image_size": "1024x1536",
            "day-image_size_wide": "1536x1024",
        },
        follow=True,
    )

    slot = DaypicSlot.objects.get(pk="day")
    assert response.status_code == 200
    assert slot.enabled
    assert slot.prompt == "новое задание"
    assert slot.generate_at == "09:30"  # normalized to two digits
    assert OperatorEvent.objects.filter(event_type="daypic_slot_saved").exists()


@pytest.mark.django_db
def test_a_broken_time_does_not_save(operator):
    response = operator.post(
        reverse("daypic_save", args=["day"]),
        {"day-title": "Картина дня", "day-generate_at": "скоро", "day-prompt": "з",
         "day-system_prompt": "", "day-styles": "", "day-chat_provider": "",
         "day-chat_model": "", "day-image_provider": "", "day-image_model": "",
         "day-image_size": "", "day-image_size_wide": ""},
    )

    assert response.status_code == 200
    assert DaypicSlot.objects.get(pk="day").generate_at == "08:00"


@pytest.mark.django_db
def test_a_new_slot_copies_the_first_and_starts_switched_off(operator):
    operator.post(reverse("daypic_create"), {"slot": "evening"}, follow=True)

    evening = DaypicSlot.objects.get(pk="evening")
    day = DaypicSlot.objects.get(pk="day")
    assert not evening.enabled
    assert evening.prompt == day.prompt
    assert evening.styles == day.styles


@pytest.mark.django_db
def test_a_duplicate_or_bad_slot_key_is_rejected(operator):
    operator.post(reverse("daypic_create"), {"slot": "day"})
    operator.post(reverse("daypic_create"), {"slot": "не латиница"})

    assert DaypicSlot.objects.count() == 1


@pytest.mark.django_db
def test_the_gallery_shows_the_issue_with_its_platform_link(operator, pipeline_db):
    response = operator.get(reverse("daypic"))

    assert response.status_code == 200
    issues = response.context["issues"]
    assert len(issues) == 1
    assert issues[0].status == "published"
    assert issues[0].platforms[0]["url"] == "https://t.me/posinus/9"
    assert issues[0].caption == "Сегодня день дружбы."
    assert b"2026-07-29-day.png" in response.content
    assert b"2026-07-29-day-wide.png" in response.content


@pytest.mark.django_db
def test_the_picture_is_served_only_when_the_pipeline_knows_it(operator, pipeline_db):
    ok = operator.get(reverse("daypic_image", args=["2026-07-29-day.png"]))
    wide = operator.get(reverse("daypic_image", args=["2026-07-29-day-wide.png"]))
    missing = operator.get(reverse("daypic_image", args=["stolen.png"]))

    assert ok.status_code == 200
    assert b"".join(ok.streaming_content) == b"\x89PNG picture bytes"
    assert wide.status_code == 200
    assert missing.status_code == 404


@pytest.mark.django_db
def test_run_now_drops_the_request_file(operator, settings, tmp_path):
    directory = tmp_path / "requests"
    directory.mkdir()
    settings.POSINUS_PIPELINE_REQUESTS_DIR = str(directory)

    operator.post(reverse("daypic_run"), follow=True)

    assert (directory / "run-daypic").exists()
    assert OperatorEvent.objects.filter(event_type="daypic_run_requested").exists()
