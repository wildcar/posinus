from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("fragment/dashboard/", views.dashboard_fragment, name="dashboard_fragment"),
    path("publication/pause/", views.publication_pause, name="publication_pause"),
    path("publication/resume/", views.publication_resume, name="publication_resume"),
    path("sources/", views.source_list, name="source_list"),
    path("sources/new/", views.source_create, name="source_create"),
    path("sources/<int:pk>/", views.source_detail, name="source_detail"),
    path("sources/<int:pk>/edit/", views.source_edit, name="source_edit"),
    path("sources/<int:pk>/resume/", views.source_resume, name="source_resume"),
    path("news/", views.news_list, name="news_list"),
    path("news/<int:pk>/", views.news_detail, name="news_detail"),
    path("news/<int:pk>/image/<str:filename>", views.news_image, name="news_image"),
    path("news/<int:pk>/translate/", views.news_translate, name="news_translate"),
    path("news/<int:pk>/select/", views.news_select, name="news_select"),
    path("broadcast/", views.broadcast, name="broadcast"),
    path("broadcast/queue/", views.queue_action, name="queue_action"),
    path("selection/", views.selection, name="selection"),
    path("selection/apply/", views.selection_apply, name="selection_apply"),
    path("selection/rescore/", views.selection_rescore, name="selection_rescore"),
    path("runs/", views.run_list, name="run_list"),
    path("events/", views.event_list, name="event_list"),
]
