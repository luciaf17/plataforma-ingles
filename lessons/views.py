import logging

from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ai import client, planner, tutor
from learners.models import Learner

from .models import Lesson, Turn
from .templatetags.lesson_extras import tutor_line

log = logging.getLogger("lessons.views")


def learner_for(request):
    return Learner.for_user(request.user)


def own_lesson(request, lesson_id):
    return get_object_or_404(Lesson.objects.select_related("learner__user", "track", "topic", "grammar_topic"), id=lesson_id, learner=learner_for(request))


def next_sequence(lesson):
    return (lesson.turns.aggregate(Max("sequence"))["sequence__max"] or 0) + 1


def current_phase_key(plan, elapsed_seconds):
    """Which phase the clock says we are in, and seconds elapsed inside it."""
    start = 0
    phases = plan.get("phases", [])
    for phase in phases:
        length = int(phase.get("minutes") or 0) * 60
        if elapsed_seconds < start + length:
            return phase["key"], elapsed_seconds - start
        start += length
    if not phases:
        return planner.PHASES[-1], elapsed_seconds
    # Past the end: still in the last phase, counted from where it began.
    last = phases[-1]
    return last["key"], elapsed_seconds - (start - int(last.get("minutes") or 0) * 60)


# --------------------------------------------------------------------------- pages


@login_required
def speaking_today(request):
    """Sidebar entry: today's speaking lesson, prepared lazily if needed (spec 4.1)."""
    learner = learner_for(request)
    today = timezone.localdate()
    lesson = planner.lesson_for(learner, today)
    if lesson is None:
        try:
            lesson, _ = planner.prepare_next_lesson(learner, on=today, skill="speaking")
        except (client.AIUnavailable, planner.NothingToPlan) as exc:
            log.error("could not prepare today's lesson: %s", exc)
            return render(request, "lessons/unavailable.html", {"section": "speaking", "title": "Speaking", "error": str(exc)}, status=503)
    return redirect("lessons:runner", lesson_id=lesson.id)


@login_required
def runner(request, lesson_id):
    lesson = own_lesson(request, lesson_id)
    if lesson.status in (Lesson.Status.COMPLETED, Lesson.Status.ANALYZED):
        return redirect("lessons:finished", lesson_id=lesson.id)
    if lesson.status == Lesson.Status.PLANNED:
        lesson.status = Lesson.Status.IN_PROGRESS
        lesson.started_at = timezone.now()
        lesson.save(update_fields=["status", "started_at"])

    plan = lesson.plan or {}
    elapsed = int((timezone.now() - lesson.started_at).total_seconds()) if lesson.started_at else 0
    phase_key, _ = current_phase_key(plan, elapsed)
    turns = list(lesson.turns.order_by("sequence"))
    context = {
        "section": "speaking",
        "title": plan.get("title") or lesson.title,
        "lesson": lesson,
        "plan": plan,
        "phases": plan.get("phases", []),
        "turns": [{"role": t.role, "text": t.text, "html": tutor_line(t.text) if t.role == "tutor" else None} for t in turns],
        "targeted": plan.get("targeted_errors", []),
        "hints": plan.get("if_stuck_hints", []),
        "config": {
            "lesson_id": lesson.id,
            "turn_url": f"/lessons/{lesson.id}/turn/",
            "tutor_url": f"/lessons/{lesson.id}/tutor/",
            "phases": [{"key": p["key"], "title": p.get("title", ""), "minutes": p.get("minutes", 0)} for p in plan.get("phases", [])],
            "duration_min": plan.get("duration_min", 20),
            "elapsed_seconds": elapsed,
            "current_phase": phase_key,
            "has_turns": bool(turns),
        },
    }
    return render(request, "lessons/speaking.html", context)


@login_required
def finished(request, lesson_id):
    """Placeholder until the report page lands (module 9)."""
    lesson = own_lesson(request, lesson_id)
    return render(request, "lessons/finished.html", {"section": "speaking", "title": "Lesson finished", "lesson": lesson})


# --------------------------------------------------------------------------- endpoints


def _in_progress_or_error(lesson):
    if lesson.status != Lesson.Status.IN_PROGRESS:
        return JsonResponse({"error": f"Lesson is {lesson.status}, not in progress"}, status=409)
    return None


def _read_phase(request):
    phase = request.POST.get("phase") or planner.PHASES[0]
    if phase not in planner.PHASES:
        phase = planner.PHASES[0]
    try:
        elapsed = max(0, int(float(request.POST.get("elapsed_in_phase") or 0)))
    except ValueError:
        elapsed = 0
    return phase, elapsed


def _tutor_turn(lesson, phase, event, elapsed_in_phase):
    """Ask the tutor, store the turn, try to voice it. TTS failure is not fatal (spec 12)."""
    text = tutor.respond(lesson, phase_key=phase, event=event, elapsed_in_phase_s=elapsed_in_phase)
    turn = Turn.objects.create(lesson=lesson, role=Turn.Role.TUTOR, text=text, phase=phase, sequence=next_sequence(lesson))
    audio_url = None
    try:
        audio = client.speak(tutor.for_speech(text))
        turn.audio_file.save(f"tutor-{lesson.id}-{turn.sequence}.mp3", ContentFile(audio), save=True)
        audio_url = turn.audio_file.url
    except client.AIUnavailable as exc:
        log.warning("TTS failed for lesson %s turn %s, text only: %s", lesson.id, turn.sequence, exc)
    return turn, audio_url


def _tutor_payload(turn, audio_url):
    return {
        "id": turn.id,
        "sequence": turn.sequence,
        "phase": turn.phase,
        "text": turn.text,
        "html": tutor_line(turn.text),
        "audio_url": audio_url,
    }


@login_required
@require_POST
def tutor_prompt(request, lesson_id):
    """The tutor speaks without a learner turn: lesson start and phase changes."""
    lesson = own_lesson(request, lesson_id)
    if error := _in_progress_or_error(lesson):
        return error
    phase, elapsed = _read_phase(request)
    event = request.POST.get("event") or "phase_start"
    if event not in tutor.EVENTS:
        event = "phase_start"
    try:
        turn, audio_url = _tutor_turn(lesson, phase, event, elapsed)
    except client.AIUnavailable as exc:
        return JsonResponse({"error": f"The tutor is unavailable right now ({exc}). Try again in a moment."}, status=503)
    return JsonResponse({"tutor": _tutor_payload(turn, audio_url)})


@login_required
@require_POST
def turn(request, lesson_id):
    """Learner spoke (audio) or typed (text). Transcribe -> tutor -> TTS -> {text, audio_url}."""
    lesson = own_lesson(request, lesson_id)
    if error := _in_progress_or_error(lesson):
        return error
    phase, elapsed = _read_phase(request)
    audio = request.FILES.get("audio")
    text = (request.POST.get("text") or "").strip()
    try:
        duration_ms = int(float(request.POST.get("duration_ms") or 0)) or None
    except ValueError:
        duration_ms = None

    if audio:
        try:
            transcript = client.transcribe((audio.name or "turn.webm", audio.read()))
        except client.AIUnavailable as exc:
            return JsonResponse({"error": f"Could not transcribe that ({exc}). Try again."}, status=503)
        text = transcript.text
        audio.seek(0)

    if not text:
        return JsonResponse({"error": "I didn't catch anything. Hold the button while you speak, then release."}, status=422)

    learner_turn = Turn.objects.create(
        lesson=lesson,
        role=Turn.Role.LEARNER,
        text=text,
        phase=phase,
        sequence=next_sequence(lesson),
        audio_duration_ms=duration_ms,
    )
    if audio:
        learner_turn.audio_file.save(f"learner-{lesson.id}-{learner_turn.sequence}.webm", audio, save=True)

    learner_payload = {"id": learner_turn.id, "sequence": learner_turn.sequence, "text": text, "word_count": learner_turn.word_count}
    try:
        tutor_turn, audio_url = _tutor_turn(lesson, phase, "turn", elapsed)
    except client.AIUnavailable as exc:
        return JsonResponse({"learner": learner_payload, "error": f"The tutor is unavailable right now ({exc}). Try again."}, status=503)
    return JsonResponse({"learner": learner_payload, "tutor": _tutor_payload(tutor_turn, audio_url)})


@login_required
@require_POST
def end(request, lesson_id):
    lesson = own_lesson(request, lesson_id)
    if lesson.status == Lesson.Status.IN_PROGRESS:
        lesson.status = Lesson.Status.COMPLETED
        lesson.completed_at = timezone.now()
        if lesson.started_at:
            lesson.duration_seconds = int((lesson.completed_at - lesson.started_at).total_seconds())
        lesson.save(update_fields=["status", "completed_at", "duration_seconds"])
    return redirect("lessons:finished", lesson_id=lesson.id)
