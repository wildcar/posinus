from urllib.parse import urlsplit

from django import forms
from django.db import transaction
from django.utils import timezone

from .models import OperatorEvent, Source, SourceEndpoint, SourceRuntimeState


class SourceForm(forms.ModelForm):
    base_url = forms.URLField(label="Адрес сайта", assume_scheme="https")
    rss_url = forms.URLField(required=False, label="RSS/Atom URL", assume_scheme="https")
    sitemap_url = forms.URLField(required=False, label="Sitemap URL", assume_scheme="https")
    section_url = forms.URLField(required=False, label="Страница новостей", assume_scheme="https")
    include_patterns_text = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Включать URL (регулярные выражения, по одному в строке)")
    exclude_patterns_text = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Исключать URL")
    title_selector = forms.CharField(required=False, label="CSS-селектор заголовка")
    body_selector = forms.CharField(required=False, label="CSS-селектор текста")

    class Meta:
        model = Source
        fields = ["name", "base_url", "status", "interval_minutes", "download_delay_seconds", "use_playwright"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["include_patterns_text"].initial = "\n".join(self.instance.include_patterns or [])
            self.fields["exclude_patterns_text"].initial = "\n".join(self.instance.exclude_patterns or [])
            self.fields["title_selector"].initial = (self.instance.adapter_config or {}).get("title_selector", "")
            self.fields["body_selector"].initial = (self.instance.adapter_config or {}).get("body_selector", "")
            mapping = {SourceEndpoint.Kind.RSS: "rss_url", SourceEndpoint.Kind.SITEMAP: "sitemap_url", SourceEndpoint.Kind.HTML: "section_url"}
            for endpoint in self.instance.endpoints.filter(enabled=True).order_by("priority"):
                field = mapping[endpoint.kind]
                if not self.fields[field].initial:
                    self.fields[field].initial = endpoint.url

    @transaction.atomic
    def save(self, commit=True):
        source = super().save(commit=False)
        source.domain = (urlsplit(source.base_url).hostname or "").lower()
        source.include_patterns = [x.strip() for x in self.cleaned_data["include_patterns_text"].splitlines() if x.strip()]
        source.exclude_patterns = [x.strip() for x in self.cleaned_data["exclude_patterns_text"].splitlines() if x.strip()]
        source.adapter_config = {k: v for k, v in {
            "title_selector": self.cleaned_data["title_selector"].strip(),
            "body_selector": self.cleaned_data["body_selector"].strip(),
        }.items() if v}
        if source.status == Source.Status.PROBATION and not source.probation_started_at:
            source.probation_started_at = timezone.now()
        if commit:
            source.save()
            SourceRuntimeState.objects.get_or_create(source=source)
            endpoint_values = [
                (SourceEndpoint.Kind.RSS, self.cleaned_data["rss_url"]),
                (SourceEndpoint.Kind.SITEMAP, self.cleaned_data["sitemap_url"]),
                (SourceEndpoint.Kind.HTML, self.cleaned_data["section_url"]),
            ]
            for priority, (kind, url) in enumerate(endpoint_values, start=1):
                if url:
                    SourceEndpoint.objects.update_or_create(source=source, url=url, defaults={"kind": kind, "enabled": True, "priority": priority})
            OperatorEvent.objects.create(event_type="source_saved", source=source, message="Источник сохранен оператором")
        return source
