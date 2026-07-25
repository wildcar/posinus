from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class Source(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Активен"
        PROBATION = "probation", "Пробный режим"
        PROBATION_WAITING = "probation_waiting", "Ожидает оценок"
        PAUSED_LOW_YIELD = "paused_low_yield", "Пауза: низкая ценность"
        PAUSED_MANUAL = "paused_manual", "Остановлен оператором"
        BLOCKED = "blocked", "Недоступен"

    name = models.CharField(max_length=300)
    base_url = models.URLField(max_length=2000, unique=True)
    domain = models.CharField(max_length=255, db_index=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    is_auto_discovered = models.BooleanField(default=False)
    interval_minutes = models.PositiveIntegerField(default=60, validators=[MinValueValidator(5), MaxValueValidator(10080)])
    download_delay_seconds = models.FloatField(default=1.0, validators=[MinValueValidator(0.2), MaxValueValidator(60)])
    use_playwright = models.BooleanField(default=False)
    include_patterns = models.JSONField(default=list, blank=True)
    exclude_patterns = models.JSONField(default=list, blank=True)
    adapter_config = models.JSONField(default=dict, blank=True)
    probation_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sources"
        ordering = ["name"]

    def __str__(self):
        return self.name


class SourceEndpoint(models.Model):
    class Kind(models.TextChoices):
        RSS = "rss", "RSS/Atom"
        SITEMAP = "sitemap", "Sitemap"
        HTML = "html", "HTML-раздел"

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="endpoints")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    url = models.URLField(max_length=2000)
    enabled = models.BooleanField(default=True)
    priority = models.PositiveIntegerField(default=100)
    etag = models.TextField(blank=True)
    last_modified = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "source_endpoints"
        ordering = ["priority", "id"]
        constraints = [models.UniqueConstraint(fields=["source", "url"], name="uq_source_endpoint_url")]
        indexes = [models.Index(fields=["source", "enabled", "priority"], name="idx_endpoint_schedule")]


class SourceRuntimeState(models.Model):
    source = models.OneToOneField(Source, on_delete=models.CASCADE, primary_key=True, related_name="runtime")
    next_run_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    lease_owner = models.CharField(max_length=200, blank=True)
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_finished_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        db_table = "source_runtime_state"


class CrawlRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Выполняется"
        SUCCESS = "success", "Успешно"
        PARTIAL = "partial", "Частично"
        FAILED = "failed", "Ошибка"

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="crawl_runs")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING, db_index=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    fetched_count = models.PositiveIntegerField(default=0)
    saved_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "crawl_runs"
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["source", "-started_at"], name="idx_runs_source_time")]


class NewsItem(models.Model):
    title = models.TextField()
    body_text = models.TextField()
    language = models.CharField(max_length=16, blank=True, db_index=True)
    author = models.TextField(blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    first_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    content_hash = models.CharField(max_length=64, unique=True)
    simhash = models.BigIntegerField(db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "news_items"
        ordering = ["-first_seen_at"]
        indexes = [models.Index(fields=["language", "-first_seen_at"], name="idx_news_lang_time")]


class NewsOccurrence(models.Model):
    news_item = models.ForeignKey(NewsItem, on_delete=models.CASCADE, related_name="occurrences")
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="occurrences")
    url = models.URLField(max_length=3000)
    normalized_url = models.TextField(unique=True)
    canonical_url = models.URLField(max_length=3000)
    fetched_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    extraction_method = models.CharField(max_length=32)
    http_status = models.PositiveIntegerField(default=200)

    class Meta:
        db_table = "news_occurrences"
        ordering = ["fetched_at"]
        indexes = [models.Index(fields=["source", "-fetched_at"], name="idx_occ_source_time")]


class NewsTranslation(models.Model):
    news_item = models.OneToOneField(NewsItem, on_delete=models.CASCADE, related_name="russian_translation")
    title = models.TextField()
    body_text = models.TextField()
    summary = models.TextField()
    model_id = models.CharField(max_length=200, blank=True)
    generated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "news_translations"


class OutboundLink(models.Model):
    occurrence = models.ForeignKey(NewsOccurrence, on_delete=models.CASCADE, related_name="outbound_links")
    url = models.URLField(max_length=3000)
    domain = models.CharField(max_length=255, db_index=True)
    is_external = models.BooleanField(default=True)

    class Meta:
        db_table = "outbound_links"
        constraints = [models.UniqueConstraint(fields=["occurrence", "url"], name="uq_occurrence_link")]
        indexes = [models.Index(fields=["domain", "is_external"], name="idx_link_domain_external")]


class ReviewEvent(models.Model):
    class Decision(models.TextChoices):
        POSITIVE = "positive", "Позитивная"
        NOT_POSITIVE = "not_positive", "Не позитивная"
        SKIPPED = "skipped", "Пропущена"

    news_item = models.ForeignKey(NewsItem, on_delete=models.PROTECT, related_name="review_events", db_column="news_id")
    decision = models.CharField(max_length=16, choices=Decision.choices)
    score = models.FloatField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(1)])
    reason = models.TextField(blank=True)
    selector_name = models.CharField(max_length=200)
    selector_version = models.CharField(max_length=200, blank=True)
    idempotency_key = models.CharField(max_length=300)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "exchange_review_events"
        constraints = [
            models.UniqueConstraint(fields=["selector_name", "idempotency_key"], name="uq_review_idempotency"),
            models.CheckConstraint(condition=models.Q(score__isnull=True) | (models.Q(score__gte=0) & models.Q(score__lte=1)), name="ck_review_score"),
        ]
        indexes = [models.Index(fields=["news_item", "selector_name", "-created_at"], name="idx_review_latest")]


class EvaluationCharacteristic(models.Model):
    class ThresholdDirection(models.TextChoices):
        LOWER_BOUND = "lower_bound", "Не ниже порога"
        UPPER_BOUND = "upper_bound", "Не выше порога"

    key = models.CharField(max_length=64, unique=True)
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=100)
    description = models.TextField()
    anchor_low = models.CharField(max_length=200)
    anchor_high = models.CharField(max_length=200)
    threshold_direction = models.CharField(max_length=16, choices=ThresholdDirection.choices, default=ThresholdDirection.LOWER_BOUND)
    position = models.PositiveIntegerField()

    class Meta:
        db_table = "exchange_evaluation_characteristics"
        ordering = ["position", "id"]

    def __str__(self):
        return self.key


class EvaluationScore(models.Model):
    review_event = models.ForeignKey(ReviewEvent, on_delete=models.PROTECT, related_name="evaluation_scores")
    characteristic = models.ForeignKey(
        EvaluationCharacteristic,
        to_field="key",
        db_column="characteristic_key",
        on_delete=models.PROTECT,
        related_name="scores",
    )
    value = models.PositiveSmallIntegerField(validators=[MinValueValidator(0), MaxValueValidator(10)])

    class Meta:
        db_table = "exchange_evaluation_scores"
        constraints = [
            models.UniqueConstraint(fields=["review_event", "characteristic"], name="uq_evaluation_score_axis"),
            models.CheckConstraint(condition=models.Q(value__gte=0) & models.Q(value__lte=10), name="ck_evaluation_score_range"),
        ]
        indexes = [models.Index(fields=["characteristic", "value"], name="idx_eval_axis_value")]


class LatestEvaluationScore(models.Model):
    """Read-only mapping of the exchange_latest_evaluation_scores SQL view.

    The view returns the scores attached to the latest review event per
    news/selector pair. Rows are unique per (review event, characteristic).
    Used for operator UI filtering subqueries; never write through it.
    """

    pk = models.CompositePrimaryKey("review_event_id", "characteristic_key")
    news_id = models.IntegerField()
    selector_name = models.CharField(max_length=200)
    review_event_id = models.IntegerField()
    created_at = models.DateTimeField()
    characteristic_key = models.CharField(max_length=64)
    value = models.PositiveSmallIntegerField()

    class Meta:
        managed = False
        db_table = "exchange_latest_evaluation_scores"


class OperatorEvent(models.Model):
    event_type = models.CharField(max_length=64, db_index=True)
    source = models.ForeignKey(Source, null=True, blank=True, on_delete=models.SET_NULL, related_name="operator_events")
    message = models.TextField()
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "operator_events"
        ordering = ["-created_at"]


class DiscoveryDomain(models.Model):
    review_event = models.ForeignKey(ReviewEvent, on_delete=models.CASCADE, related_name="discovered_domains")
    domain = models.CharField(max_length=255)
    url = models.URLField(max_length=3000)
    status = models.CharField(max_length=32, default="pending", db_index=True)
    error = models.TextField(blank=True)
    source = models.ForeignKey(Source, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "discovery_domains"
        constraints = [models.UniqueConstraint(fields=["review_event", "domain"], name="uq_discovery_review_domain")]
