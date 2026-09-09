from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.today, name="today"),
    # Placeholder routes so the sidebar is navigable before each screen exists.
    # Each one gets replaced by its real view in the module that builds it.
    path("speaking/", views.placeholder, {"section": "speaking"}, name="speaking"),
    path("listening/", views.placeholder, {"section": "listening"}, name="listening"),
    path("reading/", views.placeholder, {"section": "reading"}, name="reading"),
    path("writing/", views.placeholder, {"section": "writing"}, name="writing"),
    path("grammar/", views.placeholder, {"section": "grammar"}, name="grammar"),
    path("vocabulary/", views.placeholder, {"section": "vocabulary"}, name="vocabulary"),
    path("errors/", views.placeholder, {"section": "errors"}, name="errors"),
    path("progress/", views.placeholder, {"section": "progress"}, name="progress"),
]
