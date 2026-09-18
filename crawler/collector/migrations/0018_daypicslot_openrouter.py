# Written by hand on 2026-09-18.

from django.db import migrations

# The router's codex-oauth provider is switched off (2026-09-18), so a slot still
# pointed at it gets no prompt and no picture: every daypic run since then has
# been falling back to the built-in template and failing on the image. The
# owner's replacements on OpenRouter: the chat call goes to openai/gpt-5.6-sol
# (the same model, now through OpenRouter — reasoning and web search work
# there, verified live 2026-09-18), the picture to openai/gpt-image-2.5-sunburst
# (OpenRouter's images endpoint; 1024x1536 / 1536x1024 JPEG, verified through
# the router the same day). Only rows still on codex-oauth are touched: an
# operator who already moved a slot elsewhere keeps their choice.
CHAT_MODELS = {
    "gpt-5.6-sol": "openai/gpt-5.6-sol",
    "gpt-5.6-terra": "openai/gpt-5.6-terra",
    "gpt-5.6-luna": "openai/gpt-5.6-luna",
    "gpt-5.5": "openai/gpt-5.5",
    "gpt-5.4": "openai/gpt-5.4",
    "gpt-5.4-mini": "openai/gpt-5.4-mini",
}
DEFAULT_CHAT_MODEL = "openai/gpt-5.6-sol"
IMAGE_MODEL = "openai/gpt-image-2.5-sunburst"
IMAGE_MODELS = {"gpt-image-2": IMAGE_MODEL, "": IMAGE_MODEL}


def move_to_openrouter(apps, schema_editor):
    slot_model = apps.get_model("collector", "DaypicSlot")
    for slot in slot_model.objects.filter(chat_provider="codex-oauth"):
        slot.chat_provider = "openrouter"
        slot.chat_model = CHAT_MODELS.get(slot.chat_model, DEFAULT_CHAT_MODEL)
        slot.save(update_fields=["chat_provider", "chat_model"])
    for slot in slot_model.objects.filter(image_provider="codex-oauth"):
        slot.image_provider = "openrouter"
        slot.image_model = IMAGE_MODELS.get(slot.image_model, IMAGE_MODEL)
        slot.save(update_fields=["image_provider", "image_model"])


def move_back_to_codex(apps, schema_editor):
    slot_model = apps.get_model("collector", "DaypicSlot")
    back = {v: k for k, v in CHAT_MODELS.items()}
    for slot in slot_model.objects.filter(chat_provider="openrouter", chat_model__in=list(back)):
        slot.chat_provider = "codex-oauth"
        slot.chat_model = back[slot.chat_model]
        slot.save(update_fields=["chat_provider", "chat_model"])
    for slot in slot_model.objects.filter(image_provider="openrouter", image_model=IMAGE_MODEL):
        slot.image_provider = "codex-oauth"
        slot.image_model = "gpt-image-2"
        slot.save(update_fields=["image_provider", "image_model"])


class Migration(migrations.Migration):

    dependencies = [
        ("collector", "0017_banned_source_domain"),
    ]

    operations = [
        migrations.RunPython(move_to_openrouter, move_back_to_codex),
    ]
