from django.urls import path

from . import views

app_name = "lessons"

urlpatterns = [
    path("speaking/", views.speaking_today, name="speaking"),
    path("writing/", views.writing_today, name="writing"),
    path("lessons/<int:lesson_id>/write/", views.writing_submit, name="writing_submit"),
    path("lessons/<int:lesson_id>/", views.runner, name="runner"),
    path("lessons/<int:lesson_id>/turn/", views.turn, name="turn"),
    path("lessons/<int:lesson_id>/tutor/", views.tutor_prompt, name="tutor"),
    path("lessons/<int:lesson_id>/end/", views.end, name="end"),
    path("lessons/<int:lesson_id>/analyzing/", views.analyzing, name="analyzing"),
    path("lessons/<int:lesson_id>/analyze/", views.analyze, name="analyze"),
    path("lessons/<int:lesson_id>/report/", views.report, name="report"),
]
