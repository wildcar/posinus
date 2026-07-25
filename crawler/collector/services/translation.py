import json
import re
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from collector.models import NewsItem, NewsTranslation

from .model_router import call_chat


class TranslationError(RuntimeError):
    pass


TITLE_MARKER = "<<<TITLE>>>"
SUMMARY_MARKER = "<<<SUMMARY>>>"
BODY_MARKER = "<<<BODY>>>"
END_MARKER = "<<<END>>>"
MAX_FORMAT_ATTEMPTS = 2


@dataclass(frozen=True)
class TranslationResult:
    title: str
    body_text: str
    summary: str
    model_id: str


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    source = fenced.group(1) if fenced else text
    start = source.find("{")
    decoder = json.JSONDecoder()
    if start < 0:
        raise TranslationError("model response does not contain JSON")
    try:
        payload, _ = decoder.raw_decode(source[start:])
    except json.JSONDecodeError as exc:
        raise TranslationError("model response contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise TranslationError("model response is not a JSON object")
    return payload


def _extract_sections(text: str) -> dict[str, str]:
    positions = [
        text.find(TITLE_MARKER),
        text.find(SUMMARY_MARKER),
        text.find(BODY_MARKER),
        text.find(END_MARKER),
    ]
    if positions != sorted(positions) or any(position < 0 for position in positions):
        raise TranslationError("model response does not contain the required sections")
    title_start = positions[0] + len(TITLE_MARKER)
    summary_start = positions[1] + len(SUMMARY_MARKER)
    body_start = positions[2] + len(BODY_MARKER)
    return {
        "title": text[title_start:positions[1]].strip(),
        "summary": text[summary_start:positions[2]].strip(),
        "body_text": text[body_start:positions[3]].strip(),
    }


def _extract_translation(text: str) -> dict[str, str]:
    if TITLE_MARKER in text:
        payload = _extract_sections(text)
    else:
        payload = _extract_json(text)
    fields = {key: payload.get(key) for key in ("title", "body_text", "summary")}
    if any(not isinstance(value, str) or not value.strip() for value in fields.values()):
        raise TranslationError("model response has missing or empty translation fields")
    return {key: value.strip() for key, value in fields.items()}


def translate_news(item: NewsItem) -> NewsTranslation:
    messages = [
        {
            "role": "system",
            "content": (
                "Translate the supplied news article into natural Russian and summarize it in Russian. "
                "Preserve facts, names, numbers, links, and paragraph breaks. Do not add facts or opinions. "
                "The summary must be two to four concise sentences. Return the result in exactly these sections:\n"
                f"{TITLE_MARKER}\nRussian title\n"
                f"{SUMMARY_MARKER}\nRussian summary\n"
                f"{BODY_MARKER}\nFull Russian translation\n"
                f"{END_MARKER}\n"
                "Do not use these markers inside the translated text and return no other text."
            ),
        },
        {
            "role": "user",
            "content": f"Title:\n{item.title}\n\nArticle language: {item.language}\n\nBody:\n{item.body_text}",
        },
    ]
    reply = None
    payload = None
    last_error = None
    for attempt in range(MAX_FORMAT_ATTEMPTS):
        reply = call_chat(
            url=settings.NEWSCRAWLER_ROUTER_MCP_URL,
            token=settings.NEWSCRAWLER_ROUTER_AUTH_TOKEN,
            messages=messages,
            external_user_id="positive-news-crawler-translation",
            provider=settings.NEWSCRAWLER_TRANSLATION_PROVIDER,
            model_id=settings.NEWSCRAWLER_TRANSLATION_MODEL,
            tier=settings.NEWSCRAWLER_TRANSLATION_TIER,
            params={
                "temperature": settings.NEWSCRAWLER_TRANSLATION_TEMPERATURE,
                "max_tokens": settings.NEWSCRAWLER_TRANSLATION_MAX_TOKENS,
            },
            timeout=settings.NEWSCRAWLER_ROUTER_TIMEOUT_SECONDS,
        )
        try:
            payload = _extract_translation(reply["text"])
            break
        except TranslationError as exc:
            last_error = exc
            if attempt + 1 < MAX_FORMAT_ATTEMPTS:
                messages.extend(
                    [
                        {"role": "assistant", "content": reply["text"]},
                        {
                            "role": "user",
                            "content": (
                                "The response format was invalid. Return the same translation again using "
                                f"exactly {TITLE_MARKER}, {SUMMARY_MARKER}, {BODY_MARKER}, and {END_MARKER}."
                            ),
                        },
                    ]
                )
    if payload is None or reply is None:
        raise TranslationError("model did not return a valid translation format") from last_error
    fields = payload
    model_id = str(reply.get("model_id") or settings.NEWSCRAWLER_TRANSLATION_MODEL)
    translation, _ = NewsTranslation.objects.update_or_create(
        news_item=item,
        defaults={
            "title": fields["title"].strip(),
            "body_text": fields["body_text"].strip(),
            "summary": fields["summary"].strip(),
            "model_id": model_id[:200],
            "generated_at": timezone.now(),
        },
    )
    return translation
