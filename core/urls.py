from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.today, name="today"),
    path("onboarding/", views.onboarding, name="onboarding"),
    path("today/prepare/", views.prepare_today, name="prepare_today"),
    path("today/drop/<int:lesson_id>/", views.drop_lesson, name="drop_lesson"),
    # Placeholder routes so the sidebar is navigable before each screen exists.
    # Each one gets replaced by its real view in the module that builds it.
    path("grammar/", views.grammar_page, name="grammar"),
    path("grammar/drill/", views.start_drill, name="start_drill"),
    path("vocabulary/", views.vocabulary_page, name="vocabulary"),
    path("vocabulary/<int:item_id>/status/", views.vocab_status, name="vocab_status"),
    path("vocabulary/<int:item_id>/define/", views.vocab_define, name="vocab_define"),
    path("errors/", views.errors_page, name="errors"),
    path("errors/<int:error_id>/status/", views.error_status, name="error_status"),
    path("progress/", views.progress_page, name="progress"),
    path("progress/review/", views.progress_review, name="progress_review"),
    # Installable app: manifest and service worker at the root, no login needed.
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="service_worker"),
    path("offline/", views.offline, name="offline"),
]
