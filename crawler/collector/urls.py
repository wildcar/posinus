from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("sources/", views.source_list, name="source_list"),
    path("sources/new/", views.source_create, name="source_create"),
    path("sources/<int:pk>/", views.source_detail, name="source_detail"),
    path("sources/<int:pk>/edit/", views.source_edit, name="source_edit"),
    path("sources/<int:pk>/resume/", views.source_resume, name="source_resume"),
    path("news/", views.news_list, name="news_list"),
    path("news/<int:pk>/", views.news_detail, name="news_detail"),
    path("news/<int:pk>/translate/", views.news_translate, name="news_translate"),
    path("news/<int:pk>/select/", views.news_select, name="news_select"),
    path("runs/", views.run_list, name="run_list"),
    path("events/", views.event_list, name="event_list"),
]
