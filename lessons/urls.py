from django.urls import path

from . import views

app_name = "lessons"

urlpatterns = [
    path("speaking/", views.speaking_today, name="speaking"),
    path("lessons/<int:lesson_id>/", views.runner, name="runner"),
    path("lessons/<int:lesson_id>/turn/", views.turn, name="turn"),
    path("lessons/<int:lesson_id>/tutor/", views.tutor_prompt, name="tutor"),
    path("lessons/<int:lesson_id>/end/", views.end, name="end"),
    path("lessons/<int:lesson_id>/finished/", views.finished, name="finished"),
]
