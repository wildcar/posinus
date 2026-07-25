import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Exists, Min, OuterRef
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import SourceForm
from .models import (
    CrawlRun,
    EvaluationCharacteristic,
    LatestEvaluationScore,
    NewsItem,
    OperatorEvent,
    ReviewEvent,
    Source,
)
from .services.manual_review import mark_selected
from .services.model_router import ModelRouterError
from .services.translation import TranslationError, translate_news

SCORE_MIN = 0
SCORE_MAX = 10

NEWS_SORT_ORDERS = {
    "date_desc": ("-display_date", "-id"),
    "date_asc": ("display_date", "id"),
    "source_asc": ("primary_source", "-display_date"),
    "source_desc": ("-primary_source", "-display_date"),
}

logger = logging.getLogger(__name__)


def _score_bound(raw, default):
    """Parse a slider threshold; fall back to the default and clamp to 0..10."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(SCORE_MIN, min(SCORE_MAX, value))


@login_required
def dashboard(request):
    latest_backup = OperatorEvent.objects.filter(event_type__in=["backup_success", "backup_failed"]).first()
    context = {
        "source_counts": Source.objects.values("status").annotate(count=Count("id")).order_by("status"),
        "news_count": NewsItem.objects.filter(purged_at__isnull=True).count(),
        "unreviewed_count": NewsItem.objects.filter(purged_at__isnull=True, review_events__isnull=True).count(),
        "recent_runs": CrawlRun.objects.select_related("source")[:10],
        "recent_events": OperatorEvent.objects.select_related("source")[:10],
        "latest_backup": latest_backup,
    }
    return render(request, "collector/dashboard.html", context)


@login_required
def source_list(request):
    status = request.GET.get("status")
    sources = Source.objects.select_related("runtime").annotate(news_count=Count("occurrences", distinct=True))
    if status:
        sources = sources.filter(status=status)
    return render(request, "collector/source_list.html", {"sources": sources, "statuses": Source.Status.choices, "selected_status": status})


@login_required
def source_create(request):
    form = SourceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        source = form.save()
        messages.success(request, "Источник добавлен. Worker обнаружит доступные ленты при следующем запуске.")
        return redirect("source_detail", pk=source.pk)
    return render(request, "collector/source_form.html", {"form": form, "heading": "Новый источник"})


@login_required
def source_edit(request, pk):
    source = get_object_or_404(Source, pk=pk)
    form = SourceForm(request.POST or None, instance=source)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Источник обновлен")
        return redirect("source_detail", pk=source.pk)
    return render(request, "collector/source_form.html", {"form": form, "heading": f"Источник: {source.name}"})


@login_required
def source_detail(request, pk):
    source = get_object_or_404(Source.objects.select_related("runtime"), pk=pk)
    cutoff = timezone.now() - timezone.timedelta(days=30)
    decisions = ReviewEvent.objects.filter(news_item__occurrences__source=source, created_at__gte=cutoff).values("decision").annotate(count=Count("id"))
    return render(request, "collector/source_detail.html", {"source": source, "runs": source.crawl_runs.all()[:30], "decisions": decisions})


@login_required
def source_resume(request, pk):
    if request.method == "POST":
        source = get_object_or_404(Source, pk=pk)
        source.status = Source.Status.PROBATION
        source.probation_started_at = timezone.now()
        source.save(update_fields=["status", "probation_started_at", "updated_at"])
        OperatorEvent.objects.create(event_type="source_resumed", source=source, message="Источник возвращен в probation оператором")
        messages.success(request, "Источник возвращен в пробный режим")
    return redirect("source_detail", pk=pk)


@login_required
def news_list(request):
    items = NewsItem.objects.prefetch_related("occurrences__source").annotate(
        source_count=Count("occurrences__source", distinct=True),
        display_date=Coalesce("published_at", "first_seen_at"),
        primary_source=Min("occurrences__source__name"),
    )

    decision = request.GET.get("decision", "")
    if decision == "unreviewed":
        items = items.filter(review_events__isnull=True)
    elif decision:
        items = items.filter(review_events__decision=decision)

    raw_source = request.GET.get("source", "")
    selected_source = int(raw_source) if raw_source.isdigit() else None
    if selected_source is not None:
        items = items.filter(occurrences__source_id=selected_source)

    # Every characteristic renders as a dual-threshold slider; only ranges
    # narrower than 0..10 filter. A narrowed range requires a matching row in
    # the latest evaluation of the news item, so unevaluated news drops out.
    score_groups: dict[str, list[dict]] = {}
    for characteristic in EvaluationCharacteristic.objects.all():
        low = _score_bound(request.GET.get(f"{characteristic.key}_min"), SCORE_MIN)
        high = _score_bound(request.GET.get(f"{characteristic.key}_max"), SCORE_MAX)
        if low > high:
            low, high = high, low
        active = (low, high) != (SCORE_MIN, SCORE_MAX)
        if active:
            latest_scores = LatestEvaluationScore.objects.filter(
                news_id=OuterRef("pk"),
                characteristic_key=characteristic.key,
                value__gte=low,
                value__lte=high,
            )
            items = items.filter(Exists(latest_scores))
        score_groups.setdefault(characteristic.category, []).append(
            {"characteristic": characteristic, "low": low, "high": high, "active": active}
        )

    sort = request.GET.get("sort", "date_desc")
    if sort not in NEWS_SORT_ORDERS:
        sort = "date_desc"
    items = items.order_by(*NEWS_SORT_ORDERS[sort])

    context = {
        "items": items[:200],
        "decision": decision,
        "sort": sort,
        "sources": Source.objects.order_by("name"),
        "selected_source": selected_source,
        "score_groups": list(score_groups.items()),
    }
    return render(request, "collector/news_list.html", context)


@login_required
def news_detail(request, pk):
    item = get_object_or_404(NewsItem.objects.prefetch_related("occurrences__source", "review_events"), pk=pk)
    characteristics = {c.key: c for c in EvaluationCharacteristic.objects.all()}
    evaluations: dict[str, dict] = {}
    for row in LatestEvaluationScore.objects.filter(news_id=item.pk):
        characteristic = characteristics.get(row.characteristic_key)
        if characteristic is None:
            continue
        entry = evaluations.setdefault(
            row.selector_name,
            {"selector_name": row.selector_name, "created_at": row.created_at, "scores": []},
        )
        entry["scores"].append({"characteristic": characteristic, "value": row.value})
    for entry in evaluations.values():
        entry["scores"].sort(key=lambda score: score["characteristic"].position)
    context = {
        "item": item,
        "evaluations": sorted(evaluations.values(), key=lambda entry: entry["selector_name"]),
        "translation": getattr(item, "russian_translation", None),
        "manual_selected": item.review_events.filter(
            selector_name=f"operator:{request.user.get_username()}"[:200],
            idempotency_key=f"selected:{item.pk}",
        ).exists(),
    }
    return render(request, "collector/news_detail.html", context)


@login_required
@require_POST
def news_translate(request, pk):
    item = get_object_or_404(NewsItem, pk=pk)
    if not item.body_text.strip():
        messages.error(request, "Текст новости уже удалён, перевести его нельзя.")
    else:
        try:
            translate_news(item)
        except (ModelRouterError, TranslationError):
            logger.exception("Translation failed for news %s", item.pk)
            messages.error(request, "Не удалось получить перевод от модели. Подробности записаны в журнал сервера.")
        else:
            messages.success(request, "Перевод и краткий пересказ готовы.")
    return redirect("news_detail", pk=pk)


@login_required
@require_POST
def news_select(request, pk):
    item = get_object_or_404(NewsItem, pk=pk)
    _, created, score_count = mark_selected(item, request.user.get_username())
    if not created:
        messages.success(request, "Новость уже отмечена как отобранная.")
    elif score_count:
        messages.success(request, f"Новость отобрана. Сохранено баллов: {score_count}.")
    else:
        messages.success(request, "Новость отобрана. У неё пока нет баллов автоматической оценки.")
    return redirect("news_detail", pk=pk)


@login_required
def run_list(request):
    return render(request, "collector/run_list.html", {"runs": CrawlRun.objects.select_related("source")[:300]})


@login_required
def event_list(request):
    return render(request, "collector/event_list.html", {"events": OperatorEvent.objects.select_related("source")[:300]})
