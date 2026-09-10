from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.today, name="today"),
    path("today/prepare/", views.prepare_today, name="prepare_today"),
    # Placeholder routes so the sidebar is navigable before each screen exists.
    # Each one gets replaced by its real view in the module that builds it.
    path("listening/", views.placeholder, {"section": "listening"}, name="listening"),
    path("grammar/", views.placeholder, {"section": "grammar"}, name="grammar"),
    path("vocabulary/", views.placeholder, {"section": "vocabulary"}, name="vocabulary"),
    path("errors/", views.placeholder, {"section": "errors"}, name="errors"),
    path("progress/", views.placeholder, {"section": "progress"}, name="progress"),
]
