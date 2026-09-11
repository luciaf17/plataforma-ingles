import hashlib
import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles.storage import staticfiles_storage
from django.http import Http404
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.static import serve

from ai import client, planner, review as ai_review, vocab as ai_vocab
from learners.models import CEFR_ORDER, GrammarTopic, Learner, Topic, Track
from lessons import views as lesson_views
from lessons.models import ErrorItem, Lesson, ProgressReview, VocabItem

from . import dashboard, grammar, progress

log = logging.getLogger("core.views")

# Title and subtitle per sidebar section, lifted from the prototype copy.
SECTIONS = {
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
        "warm_audio_url": warm_audio_url(lesson),
        "picker": {
            "skills": [s for s in planner.SKILLS if s in settings.LESSON_SKILLS_ENABLED],
            "tracks": Track.objects.filter(slug__in=planner.TRACKS).order_by("slug"),
            "durations": PICKER_DURATIONS,
            "topics": Topic.objects.filter(is_active=True).select_related("track").order_by("track__slug", "title"),
        },
    }


def warm_audio_url(lesson):
    """Where Today should quietly start voicing a listening class.

    The lines are voiced by the text-to-speech API, which takes a few seconds.
    Doing it while she reads the card means pressing play is instant. Empty
    string when there is nothing to warm up.
    """
    if lesson is None or lesson.skill != "listening":
        return ""
    if lesson.status not in (Lesson.Status.PLANNED, Lesson.Status.IN_PROGRESS):
        return ""
    if lesson_views.audio_ready((lesson.plan or {}).get("listening_task") or {}):
        return ""
    return reverse("lessons:listening_audio", args=[lesson.id])


def needs_onboarding(learner):
    """Never filled the form: no goal and no placement yet (spec 7c.1)."""
    return not learner.placement_done and not learner.goal_statement


ONBOARDING_LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]
ONBOARDING_TARGETS = ["B1", "B2", "C1"]


@login_required
def onboarding(request):
    """Spec 13, module 20: EF SET levels, goal, target, then a short checkpoint."""
    learner = Learner.for_user(request.user)
    form = {
        "cefr_listening": learner.cefr_listening,
        "cefr_reading": learner.cefr_reading,
        "placement_notes": learner.placement_notes,
        "first_name": request.user.first_name,
        "target_level": learner.target_level,
        "goal_statement": learner.goal_statement,
    }
    errors = {}
    if request.method == "POST":
        form = {key: (request.POST.get(key) or "").strip() for key in form}
        if form["cefr_listening"] and form["cefr_listening"] not in ONBOARDING_LEVELS or form["cefr_reading"] and form["cefr_reading"] not in ONBOARDING_LEVELS:
            errors["levels"] = "Pick a CEFR level from the list."
        if form["target_level"] not in ONBOARDING_TARGETS:
            errors["goal"] = "Pick a target level."
        if not form["goal_statement"]:
            errors["goal"] = "Say why you are learning, even briefly. The planner uses it."
        if not errors:
            learner.cefr_listening = form["cefr_listening"]
            learner.cefr_reading = form["cefr_reading"]
            learner.placement_notes = form["placement_notes"][:300]
            learner.target_level = form["target_level"]
            learner.goal_statement = form["goal_statement"][:400]
            learner.save()
            if form["first_name"]:
                request.user.first_name = form["first_name"][:40]
                request.user.save(update_fields=["first_name"])
            if request.POST.get("next") == "checkpoint":
                existing = learner.lessons.filter(skill="checkpoint", status__in=["planned", "in_progress"]).order_by("-created_at").first()
                if existing:
                    return redirect("lessons:runner", lesson_id=existing.id)
                try:
                    lesson = planner.prepare_checkpoint(learner, short=True)
                except client.AIUnavailable as exc:
                    log.error("placement checkpoint could not be prepared: %s", exc)
                    return render(request, "lessons/unavailable.html", {"section": "today", "title": "Checkpoint", "error": str(exc)}, status=503)
                return redirect("lessons:runner", lesson_id=lesson.id)
            return redirect("core:today")
    return render(request, "core/onboarding.html", {
        "section": "today", "title": "Set up your file", "form": form, "errors": errors,
        "levels": ONBOARDING_LEVELS, "targets": ONBOARDING_TARGETS,
    })


@login_required
def today(request):
    learner = Learner.for_user(request.user)
    if needs_onboarding(learner):
        return redirect("core:onboarding")
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
        "unfinished": dashboard.unfinished_lessons(learner, today_date),
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


ERROR_FILTERS = [
    ("active", "Active"), ("due", "Due today"), ("grammar", "Grammar"), ("vocabulary", "Vocabulary"),
    ("fluency", "Fluency"), ("discourse", "Discourse"), ("mastered", "Mastered"), ("dismissed", "Dismissed"),
]


def error_rows(learner, filter_key):
    today = timezone.localdate()
    qs = ErrorItem.objects.filter(learner=learner).select_related("source_lesson")
    if filter_key == "due":
        qs = ErrorItem.objects.due_for(learner).select_related("source_lesson")
    elif filter_key in ("grammar", "vocabulary", "fluency", "discourse", "pronunciation"):
        qs = qs.filter(status="active", category=filter_key)
    elif filter_key in ("mastered", "dismissed"):
        qs = qs.filter(status=filter_key)
    else:
        qs = qs.filter(status="active")
    return [error_row(e, today) for e in qs.order_by("-occurrences", "next_review_at", "-last_seen_at")]


def error_row(e, today=None):
    today = today or timezone.localdate()
    lesson = e.source_lesson
    return {
        "error": e,
        "boxes": [i < e.srs_box for i in range(5)],
        "source": f"{lesson.get_skill_display()}, {lesson.scheduled_for:%b %d}".replace(" 0", " ") if lesson else "",
        "due": e.status == "active" and e.next_review_at <= today,
    }


def error_counts(learner):
    active = ErrorItem.objects.filter(learner=learner, status="active")
    return {
        "active": active.count(),
        "due": ErrorItem.objects.due_for(learner).count(),
        "mastered": ErrorItem.objects.filter(learner=learner, status="mastered").count(),
        "dismissed": ErrorItem.objects.filter(learner=learner, status="dismissed").count(),
    }


@login_required
def errors_page(request):
    """Spec 8.6: filterable table with SRS boxes and manual dismiss."""
    learner = Learner.for_user(request.user)
    filter_key = request.GET.get("f", "active")
    if filter_key not in dict(ERROR_FILTERS):
        filter_key = "active"
    counts = error_counts(learner)
    context = {
        "section": "errors",
        "title": "My errors",
        "rows": error_rows(learner, filter_key),
        "filter": filter_key,
        "filters": ERROR_FILTERS,
        "counts": counts,
    }
    return render(request, "core/errors.html", context)


@login_required
@require_POST
def error_status(request, error_id):
    """Dismiss (or restore) one error. Answers the HTMX row or redirects."""
    learner = Learner.for_user(request.user)
    error = ErrorItem.objects.filter(id=error_id, learner=learner).first()
    if error is None:
        raise Http404
    action = request.POST.get("action", "dismiss")
    if action == "dismiss":
        error.status = ErrorItem.Status.DISMISSED
    elif action == "restore":
        error.status = ErrorItem.Status.ACTIVE
        error.srs_box = 0
        error.next_review_at = timezone.localdate()
    error.save()
    if request.headers.get("HX-Request"):
        return render(request, "core/_error_row.html", {"row": error_row(error), "just_changed": True})
    return redirect(request.POST.get("next") or "/errors/")


VOCAB_FILTERS = [("target", "Target"), ("emerging", "Emerging"), ("acquired", "Acquired")]


@login_required
def vocabulary_page(request):
    """Spec 8.5: cards by status target / emerging / acquired."""
    learner = Learner.for_user(request.user)
    filter_key = request.GET.get("f", "target")
    if filter_key not in dict(VOCAB_FILTERS):
        filter_key = "target"
    counts = {key: VocabItem.objects.filter(learner=learner, status=key).count() for key, _ in VOCAB_FILTERS}
    items = VocabItem.objects.filter(learner=learner, status=filter_key).select_related("track").order_by("next_review_at", "term")
    all_terms = VocabItem.objects.filter(learner=learner).values_list("term", flat=True)
    context = {
        "section": "vocabulary",
        "title": "Vocabulary",
        "items": items,
        "total_count": len(all_terms),
        "chunk_count": sum(1 for term in all_terms if " " in term.strip()),
        "filter": filter_key,
        "filters": VOCAB_FILTERS,
        "counts": counts,
        "due_count": VocabItem.objects.due_for(learner).count(),
    }
    return render(request, "core/vocabulary.html", context)


@login_required
@require_POST
def vocab_define(request, item_id):
    """Words the analyzer files as gaps arrive without a definition; fetch one on demand."""
    learner = Learner.for_user(request.user)
    item = VocabItem.objects.filter(id=item_id, learner=learner).first()
    if item is None:
        raise Http404
    if not item.definition_en:
        try:
            data = ai_vocab.define(item.term, sentence=item.example_sentence, cefr=learner.cefr_for("reading"))
        except client.AIUnavailable as exc:
            log.error("could not define %r: %s", item.term, exc)
            return render(request, "core/_vocab_card.html", {"v": item, "filter": request.POST.get("f", "target"), "error": f"Could not look it up ({exc})."}, status=503)
        item.definition_en = data["definition_en"]
        item.example_sentence = item.example_sentence or data["example"]
        item.save(update_fields=["definition_en", "example_sentence"])
    return render(request, "core/_vocab_card.html", {"v": item, "filter": request.POST.get("f", "target")})


@login_required
@require_POST
def vocab_status(request, item_id):
    """Manual moves: mark acquired, back to target, or remove."""
    learner = Learner.for_user(request.user)
    item = VocabItem.objects.filter(id=item_id, learner=learner).first()
    if item is None:
        raise Http404
    action = request.POST.get("action")
    if action == "acquired":
        item.status = VocabItem.Status.ACQUIRED
        item.save()
    elif action == "target":
        item.status = VocabItem.Status.TARGET
        item.srs_box = 0
        item.next_review_at = timezone.localdate()
        item.save()
    elif action == "remove":
        item.delete()
    return redirect(request.POST.get("next") or "/vocabulary/")


SKILL_LABELS = {"speaking": "Speaking", "listening": "Listening", "reading": "Reading", "writing": "Writing"}


def review_block(learner):
    """Context for the AI review card: the stored review, if any."""
    return {
        "review": ProgressReview.objects.filter(learner=learner).first(),
        "can_review": learner.lessons.filter(status="analyzed").exists(),
    }


@login_required
def progress_page(request):
    """Spec 8.7: lessons, errors mastered, share of targeted errors avoided,
    the avoided-per-lesson chart, speaking pace, the last checkpoint and cost."""
    learner = Learner.for_user(request.user)
    today = timezone.localdate()
    context = progress.screen(learner, today)
    checkpoint = context["checkpoint"]
    context.update({
        "section": "progress",
        "title": "Progress",
        "chart_from": context["chart"][0]["date"] if context["chart"] else None,
        "chart_to": context["chart"][-1]["date"] if context["chart"] else None,
        "checkpoint_levels": [
            {"name": SKILL_LABELS[s], "estimate": (checkpoint.results.get(s) or {}).get("estimate", "")}
            for s in SKILL_LABELS
            if checkpoint and (checkpoint.results.get(s) or {}).get("assessed")
        ],
        **review_block(learner),
    })
    return render(request, "core/progress.html", context)


@login_required
@require_POST
def progress_review(request):
    """Generate a fresh review of the recent lessons. Answers the HTMX card."""
    learner = Learner.for_user(request.user)
    today = timezone.localdate()
    lessons, errors, mastered, vocabulary, checkpoint = progress.review_context(learner, today)
    if not lessons:
        return render(request, "core/_progress_review.html", {**review_block(learner), "review_error": "There are no analyzed lessons to review yet."}, status=422)
    context = ai_review.build_context(learner, lessons, errors, mastered, vocabulary, last_checkpoint=checkpoint)
    try:
        result = ai_review.review(learner, context)
    except client.AIUnavailable as exc:
        log.error("progress review failed: %s", exc)
        return render(request, "core/_progress_review.html", {**review_block(learner), "review_error": f"Could not write the review right now ({exc}). Try again in a moment."}, status=503)
    review = ProgressReview.objects.create(
        learner=learner,
        summary_es=result["summary_es"],
        improving=result["improving"],
        stuck=result["stuck"],
        focus=result["focus"],
        lessons_covered=len(lessons),
        period_start=lessons[-1]["date"],
        period_end=lessons[0]["date"],
        raw=result["raw"],
    )
    return render(request, "core/_progress_review.html", {"review": review, "can_review": True})


@login_required
def grammar_page(request):
    """Spec 7b: the A2 -> B2 program with statuses on top, recurring errors below."""
    learner = Learner.for_user(request.user)
    filter_key = request.GET.get("f", "all")
    context = {
        "section": "grammar",
        "title": "Grammar",
        "program": grammar.program(learner),
        "program_progress": dashboard.program_progress(learner),
        "patterns": grammar.recurring_errors(learner, filter_key),
        "filter": filter_key,
        "filters": grammar.FILTERS,
        "due_count": ErrorItem.objects.due_for(learner).count(),
    }
    return render(request, "core/grammar.html", context)


@login_required
@require_POST
def start_drill(request):
    """'Drill this now' / 'Drill' / 'Drill what's due': a 5-minute drill lesson on one thing."""
    learner = Learner.for_user(request.user)
    topic = None
    errors = []
    topic_id = request.POST.get("topic")
    subcategory = request.POST.get("subcategory")
    if topic_id:
        topic = GrammarTopic.objects.filter(id=topic_id).first()
        if topic is None:
            raise Http404
        errors = list(ErrorItem.objects.filter(learner=learner, status="active", subcategory__in=topic.related_subcategories or [])[:6])
    elif subcategory:
        errors = list(ErrorItem.objects.filter(learner=learner, status="active", subcategory=subcategory)[:6])
        topic = planner.topic_for_error(errors[0], learner) if errors else None
    else:
        errors = list(ErrorItem.objects.due_for(learner)[:8])
    try:
        lesson = planner.prepare_drill(learner, grammar_topic=topic, errors=errors)
    except (client.AIUnavailable, planner.NothingToPlan) as exc:
        log.error("drill could not be prepared: %s", exc)
        return render(request, "lessons/unavailable.html", {"section": "grammar", "title": "Drill", "error": str(exc)}, status=503)
    return redirect("lessons:runner", lesson_id=lesson.id)


@login_required
@require_POST
def drop_lesson(request, lesson_id):
    """Discard an unfinished lesson from an earlier day. Started speaking lessons
    are closed and analyzed instead, so what was said is not lost."""
    learner = Learner.for_user(request.user)
    lesson = Lesson.objects.filter(id=lesson_id, learner=learner).first()
    if lesson is None:
        raise Http404
    if lesson.status == Lesson.Status.IN_PROGRESS and lesson.turns.filter(role="learner").exists():
        lesson.status = Lesson.Status.COMPLETED
        lesson.completed_at = timezone.now()
        lesson.save(update_fields=["status", "completed_at"])
        return redirect("lessons:analyzing", lesson_id=lesson.id)
    if lesson.status in (Lesson.Status.PLANNED, Lesson.Status.IN_PROGRESS):
        lesson.delete()
    return redirect("core:today")


@login_required
def protected_media(request, path):
    return serve(request, path, document_root=settings.MEDIA_ROOT)


@login_required
def placeholder(request, section):
    if section not in SECTIONS:
        raise Http404
    title, subtitle = SECTIONS[section]
    return render(request, "core/placeholder.html", {"section": section, "title": title, "subtitle": subtitle})


# --- Installable app (PWA) -------------------------------------------------
# Bump when offline.html or the service worker logic changes; static file
# names already carry their own hash in production.
PWA_VERSION = "1"


def manifest(request):
    return render(request, "core/manifest.webmanifest", content_type="application/manifest+json")


def service_worker(request):
    precache = [reverse("core:offline"), static("css/app.css"), static("js/app.js")]
    version = PWA_VERSION + "-" + hashlib.sha1("|".join(precache).encode()).hexdigest()[:8]
    response = render(
        request, "core/sw.js",
        {"version": version, "offline_url": precache[0], "precache": json.dumps(precache),
         "static_url": staticfiles_storage.base_url},
        content_type="application/javascript",
    )
    # Served from the root so it covers every page; browsers re-check it on each load.
    response["Cache-Control"] = "no-cache"
    return response


def offline(request):
    return render(request, "core/offline.html")
