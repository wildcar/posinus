import logging
from dataclasses import replace
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, Max, Min, OuterRef, Q
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import SourceForm
from .models import (
    CrawlRun,
    EvaluationCharacteristic,
    LatestEvaluationScore,
    LatestReview,
    NewsItem,
    OperatorEvent,
    PublicationPlan,
    ReviewEvent,
    SelectionBound,
    Source,
)
from .services.broadcast import broadcast_state
from .services.calibration import (
    ADDED_LIMIT,
    NEAR_MISS_LIMIT,
    apply_profile,
    corpus_scores,
    near_misses,
    titles_for,
)
from .services.console import attention, feed_mix, pipeline_counters, today_counters
from .services.manual_review import mark_selected
from .services.model_router import ModelRouterError
from .services.pipeline_db import PipelineUnavailable
from .services.pipeline_mailbox import (
    MailboxUnavailable,
    clear_pause,
    read_pause,
    request_run,
    set_pause,
)
from .services.pipeline_status import machine_block
from .services.selection import Bound, active_profile, explain, profile_bounds
from .services.stages import stages_for
from .services.translation import TranslationError, translate_news

SCORE_MIN = 0
SCORE_MAX = 10
NEWS_PAGE_SIZE = 50

# Two axis columns by default, changeable per request: seven columns is the
# limit of a readable row, and axes would otherwise pile up until nothing fits.
DEFAULT_SCORE_COLUMNS = ("positivity", "interestingness")

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


PAUSE_DURATIONS = {
    "hour": "на час",
    "today": "до конца дня",
    "forever": "до отмены",
}


def _pause_deadline(choice: str):
    """Turn the operator's choice of duration into a moment, or None for «до отмены»."""
    now = timezone.now()
    if choice == "hour":
        return now + timezone.timedelta(hours=1)
    if choice == "today":
        zone = ZoneInfo(settings.POSINUS_OPERATOR_TIMEZONE)
        local = now.astimezone(zone)
        end_of_day = local.replace(hour=23, minute=59, second=0, microsecond=0)
        if end_of_day <= local:
            end_of_day += timezone.timedelta(days=1)
        return end_of_day
    return None


def _dashboard_context(request) -> dict:
    """The live half of the dashboard, shared by the page and its refresh fragment."""
    try:
        pause, mailbox_error = read_pause(), ""
    except MailboxUnavailable as exc:
        pause, mailbox_error = None, str(exc)
    runs, pipeline_queue, pipeline_error = machine_block()
    try:
        pipeline_numbers = pipeline_counters()
    except PipelineUnavailable:
        pipeline_numbers = []
    return {
        "pause": pause,
        "mailbox_error": mailbox_error,
        "pause_durations": PAUSE_DURATIONS.items(),
        "service_runs": runs,
        "pipeline_queue": pipeline_queue,
        "pipeline_error": pipeline_error,
        "counters": today_counters() + pipeline_numbers,
        "attention": attention(),
    }


def dashboard_fragment(request):
    """The same live blocks again, for the minute refresh.

    Answers 401 instead of redirecting when the session has expired: a redirect
    would paste the login form inside the block, and the script stops on 401.
    A version tag lets the server say «не изменилось» with 204 rather than make
    the page repaint every minute for nothing.
    """
    if not request.user.is_authenticated:
        return HttpResponse(status=401)
    context = _dashboard_context(request)
    version = str(
        hash((
            tuple((c.title, c.value) for c in context["counters"]),
            tuple(p.text for p in context["attention"]),
            tuple((r.service, r.status, str(r.finished_at)) for r in context["service_runs"]),
            bool(context["pause"]),
        ))
    )
    if request.GET.get("version") == version:
        return HttpResponse(status=204)
    response = render(request, "collector/_dashboard_live.html", context)
    response["X-Dashboard-Version"] = version
    return response


@login_required
def dashboard(request):
    latest_backup = OperatorEvent.objects.filter(event_type__in=["backup_success", "backup_failed"]).first()
    context = {
        **_dashboard_context(request),
        "feed_mix": feed_mix(),
        "source_counts": Source.objects.values("status").annotate(count=Count("id")).order_by("status"),
        "news_count": NewsItem.objects.filter(purged_at__isnull=True).count(),
        "unreviewed_count": NewsItem.objects.filter(purged_at__isnull=True, review_events__isnull=True).count(),
        "recent_runs": CrawlRun.objects.select_related("source")[:10],
        "recent_events": OperatorEvent.objects.select_related("source")[:10],
        "latest_backup": latest_backup,
    }
    return render(request, "collector/dashboard.html", context)


@login_required
@require_POST
def publication_pause(request):
    """The stop cock: hold every publication, let the queue grow."""
    choice = request.POST.get("duration", "forever")
    if choice not in PAUSE_DURATIONS:
        choice = "forever"
    reason = request.POST.get("reason", "").strip()
    until = _pause_deadline(choice)
    try:
        set_pause(until, reason)
    except MailboxUnavailable as exc:
        logger.error("Cannot write the pause file: %s", exc)
        messages.error(request, "Не получилось остановить публикации: нет доступа к каталогу заявок конвейера.")
    else:
        OperatorEvent.objects.create(
            event_type="publication_paused",
            message=f"Публикации остановлены {PAUSE_DURATIONS[choice]}. Причина: {reason or 'не указана'}",
        )
        messages.success(
            request,
            f"Публикации остановлены {PAUSE_DURATIONS[choice]}. Новости копятся в очереди, ничего не теряется.",
        )
    return redirect("dashboard")


@login_required
@require_POST
def publication_resume(request):
    try:
        clear_pause()
    except MailboxUnavailable as exc:
        logger.error("Cannot remove the pause file: %s", exc)
        messages.error(request, "Не получилось снять паузу: нет доступа к каталогу заявок конвейера.")
    else:
        OperatorEvent.objects.create(event_type="publication_resumed", message="Публикации возобновлены оператором")
        messages.success(request, "Публикации возобновлены. Ближайший выход - в ближайший прогон публикатора.")
    return redirect("dashboard")


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

    # Decisions are matched against the latest event of each news/selector pair.
    # The raw event table would also match superseded verdicts: the contract
    # corrects a decision by appending an event, so one news item can carry
    # 'skipped' and 'not_positive' in its history and would show up under both.
    decision = request.GET.get("decision", "")
    if decision == "unreviewed":
        items = items.filter(review_events__isnull=True)
    elif decision:
        latest = LatestReview.objects.filter(news_id=OuterRef("pk"), decision=decision)
        items = items.filter(Exists(latest))

    # Search covers the body, not only the headline: a person remembers «была
    # новость про кота в Норвегии» and never the exact title.
    query = request.GET.get("q", "").strip()
    if query:
        items = items.filter(Q(title__icontains=query) | Q(body_text__icontains=query))

    raw_source = request.GET.get("source", "")
    selected_source = int(raw_source) if raw_source.isdigit() else None
    if selected_source is not None:
        items = items.filter(occurrences__source_id=selected_source)

    # Every characteristic renders as a dual-threshold slider; only ranges
    # narrower than 0..10 filter. A narrowed range requires a matching row in
    # the latest evaluation of the news item, so unevaluated news drops out.
    # Only the configured evaluator's scores count: the operator's manual review
    # snapshots those same scores under its own selector name, and matching both
    # copies would make one evaluation look like two.
    score_selector = settings.POSINUS_MANUAL_SCORE_SELECTOR
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
                selector_name=score_selector,
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

    # Pages of a fixed size with the real total, instead of the former slice of
    # 200 rows that hid both the rest of the corpus and its size.
    paginator = Paginator(items, NEWS_PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    other_params = request.GET.copy()
    other_params.pop("page", None)

    # Two extra queries for the whole page: the verdicts and the pipeline state.
    page_items = list(page.object_list)
    stages = stages_for([item.pk for item in page_items])
    score_columns = [
        axis for axis in request.GET.getlist("axis") or DEFAULT_SCORE_COLUMNS
        if axis in {c.key for c in EvaluationCharacteristic.objects.all()}
    ] or list(DEFAULT_SCORE_COLUMNS)
    column_scores = {
        (row.news_id, row.characteristic_key): row.value
        for row in LatestEvaluationScore.objects.filter(
            selector_name=score_selector,
            news_id__in=[item.pk for item in page_items],
            characteristic_key__in=score_columns,
        )
    }
    axis_titles = {c.key: c.title for c in EvaluationCharacteristic.objects.all()}
    for item in page_items:
        item.stage = stages.get(item.pk)
        item.column_scores = [
            {"key": axis, "title": axis_titles.get(axis, axis), "value": column_scores.get((item.pk, axis))}
            for axis in score_columns
        ]

    context = {
        "page": page,
        "items": page_items,
        "query": query,
        "score_columns": score_columns,
        "score_headers": [axis_titles.get(axis, axis) for axis in score_columns],
        "all_axes": [{"key": c.key, "title": c.title} for c in EvaluationCharacteristic.objects.all()],
        "total_count": paginator.count,
        "page_params": other_params.urlencode(),
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

    # Why this news item did or did not pass, computed from the thresholds in
    # force — the same rows the evaluator decides by.
    verdict = None
    profile = active_profile()
    evaluator_scores = {
        row["characteristic"].key: row["value"]
        for row in evaluations.get(settings.POSINUS_MANUAL_SCORE_SELECTOR, {}).get("scores", [])
    }
    if profile is not None and evaluator_scores:
        verdict = explain(profile.name, profile.revision, profile_bounds(profile), evaluator_scores)

    context = {
        "item": item,
        "verdict": verdict,
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
        messages.success(request, "Новость уже отправлена в публикацию, повторное нажатие ничего не меняет.")
    elif score_count:
        messages.success(
            request,
            f"Новость отправлена в публикацию, выйдет примерно через два часа. Сохранено баллов: {score_count}.",
        )
    else:
        messages.success(
            request,
            "Новость отправлена в публикацию, выйдет примерно через два часа. Баллов автоматической оценки у неё пока нет.",
        )
    return redirect("news_detail", pk=pk)


def _draft_from_form(data, bounds: list[Bound]) -> tuple[list[Bound], bool]:
    """Read the edited thresholds out of the form; unchanged means «no draft»."""
    draft, changed = [], False
    for bound in bounds:
        raw = data.get(f"{bound.kind}__{bound.key}")
        if raw == "":
            changed = True  # the operator dropped the condition entirely
            continue
        value = _score_bound(raw, bound.value)
        changed = changed or value != bound.value
        draft.append(replace(bound, value=value))
    return draft, changed


@login_required
def selection(request):
    """The calibration screen: the rule in force, a draft, and what both do to the corpus."""
    profile = active_profile()
    if profile is None:
        return render(request, "collector/selection.html", {"profile": None})

    bounds = profile_bounds(profile)
    draft, has_draft = _draft_from_form(request.GET, bounds)
    corpus = corpus_scores()
    current = apply_profile(bounds, corpus)
    draft_outcome = apply_profile(draft, corpus) if has_draft else None

    titles = {axis.key: axis.title for axis in EvaluationCharacteristic.objects.all()}
    blockers = sorted(
        (
            {"title": titles.get(key, key) if key else "ни одной сильной стороны", "count": count}
            for key, count in current.blocked_by.items()
        ),
        key=lambda row: row["count"],
        reverse=True,
    )
    used = {bound.key for bound in bounds}
    unused = [axis.title for axis in EvaluationCharacteristic.objects.all() if axis.key not in used]

    added_ids = (draft_outcome.passed - current.passed) if draft_outcome else set()
    lost_ids = (current.passed - draft_outcome.passed) if draft_outcome else set()
    misses = near_misses(draft if has_draft else bounds, corpus)

    context = {
        "profile": profile,
        "gates_min": [b for b in bounds if b.kind == SelectionBound.Kind.GATE_MIN],
        "gates_max": [b for b in bounds if b.kind == SelectionBound.Kind.GATE_MAX],
        "highlights": [b for b in bounds if b.kind == SelectionBound.Kind.HIGHLIGHT_MIN],
        "draft": {b.kind + "__" + b.key: b.value for b in draft} if has_draft else {},
        "has_draft": has_draft,
        "current": current,
        "draft_outcome": draft_outcome,
        "blockers": blockers,
        "unused_axes": unused,
        "added_count": len(added_ids),
        "lost_count": len(lost_ids),
        "added_items": titles_for(added_ids, ADDED_LIMIT),
        "lost_items": titles_for(lost_ids, ADDED_LIMIT),
        "near_miss_count": len(misses),
        "near_misses": [
            {"item": item, "reason": dict(misses).get(item.pk, "")}
            for item in titles_for([news_id for news_id, _ in misses], NEAR_MISS_LIMIT)
        ],
    }
    return render(request, "collector/selection.html", context)


@login_required
@require_POST
def selection_apply(request):
    """Make the edited thresholds the rule in force, for both readers at once."""
    profile = active_profile()
    if profile is None:
        messages.error(request, "Действующего профиля нет, применять нечего.")
        return redirect("selection")

    bounds = profile_bounds(profile)
    draft, changed = _draft_from_form(request.POST, bounds)
    if not changed:
        messages.success(request, "Пороги не изменились.")
        return redirect("selection")

    with transaction.atomic():
        profile.bounds.all().delete()
        SelectionBound.objects.bulk_create(
            [
                SelectionBound(profile=profile, characteristic_id=b.key, kind=b.kind, value=b.value)
                for b in draft
            ]
        )
        profile.revision += 1
        profile.save(update_fields=["revision", "updated_at"])
    OperatorEvent.objects.create(
        event_type="selection_profile_changed",
        message=f"Профиль «{profile.name}» изменён, редакция {profile.revision}",
        details={"bounds": [{"axis": b.key, "kind": b.kind, "value": b.value} for b in draft]},
    )
    messages.success(
        request,
        f"Новые пороги действуют, редакция {profile.revision}. Уже оценённые новости "
        "не пересчитываются: правило подействует на те, что придут дальше.",
    )
    return redirect("selection")


@login_required
@require_POST
def selection_rescore(request):
    """Ask the evaluator to re-apply the rule to everything it has already scored."""
    try:
        request_run("evaluator-backfill")
    except MailboxUnavailable as exc:
        logger.error("Cannot request a rescore: %s", exc)
        messages.error(request, "Не получилось запросить пересчёт: нет доступа к каталогу заявок конвейера.")
    else:
        OperatorEvent.objects.create(
            event_type="selection_rescore_requested",
            message="Запрошен пересчёт уже оценённых новостей по действующему профилю",
        )
        messages.success(
            request,
            "Пересчёт запущен. Модель при этом не вызывается: решения считаются по сохранённым баллам, "
            "исправления добавляются новыми записями.",
        )
    return redirect("selection")


@login_required
def broadcast(request):
    """«Эфир»: the queue, what went out, and the platforms — all read-only."""
    state, error = broadcast_state()
    tab = request.GET.get("tab", "queue")
    if tab not in {"queue", "published", "platforms"}:
        tab = "queue"
    try:
        pause, mailbox_error = read_pause(), ""
    except MailboxUnavailable as exc:
        pause, mailbox_error = None, str(exc)
    return render(
        request,
        "collector/broadcast.html",
        {"tab": tab, "pipeline_error": error, "pause": pause, "mailbox_error": mailbox_error, **state},
    )


QUEUE_ACTIONS = {
    "up": "поднята выше",
    "down": "опущена ниже",
    "hold": "отложена на сутки",
    "drop": "снята с очереди",
    "restore": "возвращена в очередь",
}


@login_required
@require_POST
def queue_action(request):
    """Move one news item in the publication queue, or take it out of it.

    The plan lives in the crawler's own database and the publisher reads it
    through `exchange_publication_order`; nothing here touches the pipeline's
    data. «Выше» and «ниже» are relative on purpose — the operator moves one
    story past the others, not renumbers a list.
    """
    action = request.POST.get("action", "")
    news_id = request.POST.get("news_id", "")
    if action not in QUEUE_ACTIONS or not news_id.isdigit():
        messages.error(request, "Непонятное действие с очередью.")
        return redirect("broadcast")

    item = get_object_or_404(NewsItem, pk=int(news_id))
    plan, _ = PublicationPlan.objects.get_or_create(news_item=item)
    now = timezone.now()
    if action == "up":
        top = PublicationPlan.objects.aggregate(low=Min("rank"))["low"] or 0
        plan.rank = min(top, 0) - 1
    elif action == "down":
        bottom = PublicationPlan.objects.aggregate(high=Max("rank"))["high"] or 0
        plan.rank = max(bottom, 0) + 1
    elif action == "hold":
        plan.hold_until = now + timezone.timedelta(days=1)
    elif action == "drop":
        plan.dropped_at = now
    else:
        plan.rank, plan.hold_until, plan.dropped_at = 0, None, None
    plan.save()

    OperatorEvent.objects.create(
        event_type="queue_changed",
        message=f"Новость {item.pk} {QUEUE_ACTIONS[action]}",
        details={"news_id": item.pk, "action": action, "rank": plan.rank},
    )
    messages.success(request, f"Новость {QUEUE_ACTIONS[action]}. Публикатор увидит это в ближайший прогон.")
    return redirect("broadcast")


@login_required
def run_list(request):
    return render(request, "collector/run_list.html", {"runs": CrawlRun.objects.select_related("source")[:300]})


@login_required
def event_list(request):
    return render(request, "collector/event_list.html", {"events": OperatorEvent.objects.select_related("source")[:300]})
