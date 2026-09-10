from django.urls import path

from . import views

app_name = "lessons"

urlpatterns = [
    path("speaking/", views.speaking_today, name="speaking"),
    path("writing/", views.writing_today, name="writing"),
    path("reading/", views.reading_today, name="reading"),
    path("listening/", views.listening_today, name="listening"),
    path("lessons/<int:lesson_id>/audio/", views.listening_audio, name="listening_audio"),
    path("lessons/<int:lesson_id>/listened/", views.listening_listened, name="listening_listened"),
    path("lessons/<int:lesson_id>/listen/", views.listening_submit, name="listening_submit"),
    path("lessons/<int:lesson_id>/read/", views.reading_submit, name="reading_submit"),
    path("lessons/<int:lesson_id>/vocab/", views.vocab_lookup, name="vocab_lookup"),
    path("lessons/<int:lesson_id>/mini-lesson/", views.mini_lesson_check, name="mini_lesson_check"),
    path("lessons/<int:lesson_id>/checkpoint/<str:step>/", views.checkpoint_step, name="checkpoint_step"),
    path("lessons/<int:lesson_id>/checkpoint-finish/", views.checkpoint_finish, name="checkpoint_finish"),
    path("lessons/<int:lesson_id>/write/", views.writing_submit, name="writing_submit"),
    path("lessons/<int:lesson_id>/", views.runner, name="runner"),
    path("lessons/<int:lesson_id>/turn/", views.turn, name="turn"),
    path("lessons/<int:lesson_id>/tutor/", views.tutor_prompt, name="tutor"),
    path("lessons/<int:lesson_id>/end/", views.end, name="end"),
    path("lessons/<int:lesson_id>/analyzing/", views.analyzing, name="analyzing"),
    path("lessons/<int:lesson_id>/analyze/", views.analyze, name="analyze"),
    path("lessons/<int:lesson_id>/report/", views.report, name="report"),
]
