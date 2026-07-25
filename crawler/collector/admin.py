from django.contrib import admin
from .models import CrawlRun, NewsItem, NewsOccurrence, OperatorEvent, ReviewEvent, Source, SourceEndpoint, SourceRuntimeState

admin.site.register([Source, SourceEndpoint, SourceRuntimeState, CrawlRun, NewsItem, NewsOccurrence, ReviewEvent, OperatorEvent])

