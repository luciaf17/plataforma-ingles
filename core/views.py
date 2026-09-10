import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.static import serve

from ai import client, planner
from ai.models import ApiCall
from learners.models import Learner, Topic, Track
from lessons.models import ErrorItem, Lesson

from . import dashboard

log = logging.getLogger("core.views")

# Title and subtitle per sidebar section, lifted from the prototype copy.
SECTIONS = {
    "grammar": ("Grammar", "Built from your own errors, not a textbook index."),
    "vocabulary": ("Vocabulary", "Words you looked up, words the tutor planted, and words you've started using on your own."),
    "errors": ("My errors", "Every error you've made, where it came from, and how close it is to being gone."),
    "progress": ("Progress", "The number that matters is the last one."),
}

PICKER_DURATIONS = [10, 15, 20, 30]


def todays_lesson(learner, today):
    """The lesson the dashboard shows: the open one, else the latest finished today."""
    lesson = planner.lesson_for(learner, today)
    if lesson is None:
        lesson = learner.lessons.filter(scheduled_for=today).order_by("-created_at").first()
    return lesson


def lesson_card_context(request, learner, lesson, error=None):
    targeted = []
    if lesson:
        plan = lesson.plan or {}
        occurrences = {
            e.id: e.occurrences for e in ErrorItem.objects.filter(learner=learner, id__in=plan.get("targeted_error_ids", []))
        }
        targeted = [{**t, "occurrences": occurrences.get(t.get("id"))} for t in plan.get("targeted_errors", [])]
    return {
        "lesson": lesson,
        "targeted": targeted,
        "prepare_error": error,
        "picker": {
            "skills": [s for s in planner.SKILLS if s in settings.LESSON_SKILLS_ENABLED],
            "tracks": Track.objects.filter(slug__in=planner.TRACKS).order_by("slug"),
            "durations": PICKER_DURATIONS,
            "topics": Topic.objects.filter(is_active=True).select_related("track").order_by("track__slug", "title"),
        },
    }


@login_required
def today(request):
    learner = Learner.for_user(request.user)
    now = timezone.localtime()
    today_date = now.date()
    lesson = todays_lesson(learner, today_date)
    context = {
        "section": "today",
        "title": f"{now:%A}, {now:%B} {now.day}",
        "day_number": dashboard.day_number(learner, today_date),
        "yesterday_line": dashboard.yesterday_line(learner, today_date),
        "streak": dashboard.streak(learner, today_date),
        "last_days": dashboard.last_days(learner, today_date),
        "skill_levels": dashboard.skill_levels(learner),
        "program": dashboard.program_progress(learner),
        "recent": dashboard.recent_lessons(learner, today_date),
        **lesson_card_context(request, learner, lesson),
    }
    return render(request, "core/today.html", context)


@login_required
@require_POST
def prepare_today(request):
    """Prepare (or, with force, replace) today's lesson. Answers the HTMX card or redirects."""
    learner = Learner.for_user(request.user)
    today_date = timezone.localdate()
    force = request.POST.get("force") == "1"
    skill = request.POST.get("skill") or None
    track = request.POST.get("track") or None
    duration = int(request.POST.get("duration") or planner.DEFAULT_DURATION)
    topic = request.POST.get("topic") or None
    request_text = (request.POST.get("request") or "").strip()[:500]
    if request.POST.get("surprise"):
        skill, track, topic, duration, request_text = None, None, None, planner.DEFAULT_DURATION, ""
    if skill and skill not in settings.LESSON_SKILLS_ENABLED:
        skill = None
    if duration not in PICKER_DURATIONS:
        duration = planner.DEFAULT_DURATION

    error = None
    try:
        lesson, _ = planner.prepare_next_lesson(
            learner, on=today_date, skill=skill, track=track, topic=int(topic) if topic else None, duration=duration, force=force,
            request=request_text,
        )
    except (client.AIUnavailable, planner.NothingToPlan, ValueError, Topic.DoesNotExist, Track.DoesNotExist) as exc:
        log.error("prepare_today failed: %s", exc)
        error = str(exc)
        lesson = todays_lesson(learner, today_date)
    if not request.headers.get("HX-Request"):
        return redirect("core:today")
    status = 503 if error else 200
    return render(request, "core/_today_lesson.html", lesson_card_context(request, learner, lesson, error=error), status=status)


@login_required
def protected_media(request, path):
    return serve(request, path, document_root=settings.MEDIA_ROOT)


@login_required
def placeholder(request, section):
    if section not in SECTIONS:
        raise Http404
    title, subtitle = SECTIONS[section]
    context = {"section": section, "title": title, "subtitle": subtitle}
    if section == "progress":
        # The full Progress screen is module 22; the running API cost is shown from day one.
        context["usage"] = ApiCall.totals()
    return render(request, "core/placeholder.html", context)
