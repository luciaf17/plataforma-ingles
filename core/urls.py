from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.today, name="today"),
    path("today/prepare/", views.prepare_today, name="prepare_today"),
    path("today/drop/<int:lesson_id>/", views.drop_lesson, name="drop_lesson"),
    # Placeholder routes so the sidebar is navigable before each screen exists.
    # Each one gets replaced by its real view in the module that builds it.
    path("grammar/", views.grammar_page, name="grammar"),
    path("grammar/drill/", views.start_drill, name="start_drill"),
    path("vocabulary/", views.vocabulary_page, name="vocabulary"),
    path("vocabulary/<int:item_id>/status/", views.vocab_status, name="vocab_status"),
    path("errors/", views.errors_page, name="errors"),
    path("errors/<int:error_id>/status/", views.error_status, name="error_status"),
    path("progress/", views.placeholder, {"section": "progress"}, name="progress"),
]
