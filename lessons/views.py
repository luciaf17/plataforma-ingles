import logging

from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from ai import analyzer, client, level_assessor, minilesson, planner, tutor, vocab, writing
from learners.models import Learner

from . import postprocess
from .diff import change_count, diff_html
from .models import Checkpoint, ErrorItem, Lesson, LessonReport, Turn, VocabItem
from .templatetags.lesson_extras import tutor_line

log = logging.getLogger("lessons.views")


NON_CONVERSATION_PHASES = ("listening", "writing")


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


def skill_today(request, skill):
    """Sidebar entry for a skill: today's open lesson for it, prepared lazily if needed (spec 4.1)."""
    learner = learner_for(request)
    today = timezone.localdate()
    lesson = planner.lesson_for(learner, today, skill=skill)
    if lesson is None:
        try:
            lesson, _ = planner.prepare_next_lesson(learner, on=today, skill=skill)
        except (client.AIUnavailable, planner.NothingToPlan) as exc:
            log.error("could not prepare today's %s lesson: %s", skill, exc)
            return render(request, "lessons/unavailable.html", {"section": skill, "title": skill.capitalize(), "error": str(exc)}, status=503)
    return redirect("lessons:runner", lesson_id=lesson.id)


@login_required
def speaking_today(request):
    return skill_today(request, "speaking")


@login_required
def writing_today(request):
    return skill_today(request, "writing")


@login_required
def reading_today(request):
    return skill_today(request, "reading")


@login_required
def listening_today(request):
    return skill_today(request, "listening")


def _start(lesson):
    if lesson.status == Lesson.Status.PLANNED:
        lesson.status = Lesson.Status.IN_PROGRESS
        lesson.started_at = timezone.now()
        lesson.save(update_fields=["status", "started_at"])


@login_required
def runner(request, lesson_id):
    """One URL per lesson; the template depends on the skill."""
    lesson = own_lesson(request, lesson_id)
    if lesson.status == Lesson.Status.ANALYZED:
        return redirect("lessons:report", lesson_id=lesson.id)
    if lesson.status == Lesson.Status.COMPLETED:
        return redirect("lessons:analyzing", lesson_id=lesson.id)
    _start(lesson)
    if lesson.skill == "writing":
        return writing_runner(request, lesson)
    if lesson.skill == "reading":
        return reading_runner(request, lesson)
    if lesson.skill == "listening":
        return listening_runner(request, lesson)
    if lesson.skill == "checkpoint":
        return checkpoint_runner(request, lesson)
    return speaking_runner(request, lesson)


# --------------------------------------------------------------------------- checkpoint (spec 7c)

CHECKPOINT_STEP_TITLES = {"listening": "Listening", "reading": "Reading", "writing": "Writing", "speaking": "Speaking"}


def checkpoint_state(lesson):
    plan = lesson.plan or {}
    cp = plan.get("checkpoint") or {}
    steps = cp.get("steps", ["listening", "reading", "writing", "speaking"])
    progress = cp.get("progress", {})
    current = next((s for s in steps if s not in progress), None)
    return steps, progress, current


def save_checkpoint_progress(lesson, step, data):
    plan = lesson.plan or {}
    cp = dict(plan.get("checkpoint") or {})
    cp["progress"] = {**cp.get("progress", {}), step: data}
    lesson.plan = {**plan, "checkpoint": cp}
    lesson.save(update_fields=["plan"])


def checkpoint_runner(request, lesson, *, error="", answers=None, text=""):
    steps, progress, current = checkpoint_state(lesson)
    plan = lesson.plan or {}
    if current == "speaking":
        return speaking_runner(request, lesson, checkpoint=True)
    if current == "listening" and not audio_ready(plan.get("listening_task") or {}):
        return render(request, "lessons/listening_audio.html", {"section": "today", "title": lesson.title, "lesson": lesson, "task": plan.get("listening_task") or {}})
    task = plan.get(f"{current}_task") if current else None
    context = {
        "section": "today",
        "title": lesson.title,
        "lesson": lesson,
        "plan": plan,
        "steps": [{"key": s, "title": CHECKPOINT_STEP_TITLES[s], "done": s in progress, "current": s == current} for s in steps],
        "current": current,
        "task": task,
        "paragraphs": [p.strip() for p in ((task or {}).get("text") or "").split("\n\n") if p.strip()] if current == "reading" else [],
        "answers": answers or {},
        "text": text,
        "error": error,
        "config": {
            "segments": [s["url"] for s in (task or {}).get("segments", [])],
            "listens": (task or {}).get("listens", 0),
            "max_listens": (task or {}).get("max_listens", 2),
            "listened_url": f"/lessons/{lesson.id}/listened/",
            "vocab_url": f"/lessons/{lesson.id}/vocab/",
            "glossary": (task or {}).get("glossary", []),
        },
    }
    if current is None:
        return render(request, "lessons/checkpoint_finish.html", context)
    return render(request, "lessons/checkpoint.html", context)


@login_required
@require_POST
def checkpoint_step(request, lesson_id, step):
    """Hand in one mini-test. Comprehension is graded in code; writing and speaking are stored for the assessor."""
    lesson = own_lesson(request, lesson_id)
    if lesson.skill != "checkpoint":
        return JsonResponse({"error": "Not a checkpoint"}, status=409)
    if lesson.status == Lesson.Status.ANALYZED:
        return redirect("lessons:report", lesson_id=lesson.id)
    steps, progress, current = checkpoint_state(lesson)
    if step != current:
        return redirect("lessons:runner", lesson_id=lesson.id)
    _start(lesson)
    plan = lesson.plan or {}

    if step in ("listening", "reading"):
        task = plan.get(f"{step}_task") or {}
        answers = {}
        for q in task.get("questions", []):
            raw = request.POST.get(f"q{q['id']}")
            if raw is not None and raw.isdigit():
                answers[q["id"]] = int(raw)
        if any(q["id"] not in answers for q in task.get("questions", [])):
            return checkpoint_runner(request, lesson, error="Answer every question before continuing.", answers=answers)
        score, rows = grade_questions(task, answers)
        save_checkpoint_progress(lesson, step, {"score": score, "total": len(rows), "questions": rows, "listens": task.get("listens", 0)})
    elif step == "writing":
        text = (request.POST.get("text") or "").strip()
        if len(text.split()) < 40:
            return checkpoint_runner(request, lesson, error="Write at least 40 words.", text=text)
        Turn.objects.create(lesson=lesson, role=Turn.Role.LEARNER, text=text, phase="writing", sequence=next_sequence(lesson))
        save_checkpoint_progress(lesson, "writing", {"text": text, "word_count": len(text.split())})
    elif step == "speaking":
        turns = list(lesson.turns.filter(phase="practice").order_by("sequence"))
        if not any(t.role == Turn.Role.LEARNER for t in turns):
            return redirect("lessons:runner", lesson_id=lesson.id)
        save_checkpoint_progress(lesson, "speaking", {"turns": [{"role": t.role, "text": t.text} for t in turns]})
    return redirect("lessons:runner", lesson_id=lesson.id)


def checkpoint_materials(lesson):
    """What the assessor reads: per skill, the material plus what she did with it."""
    plan = lesson.plan or {}
    progress = (plan.get("checkpoint") or {}).get("progress", {})
    materials = {}
    for skill in ("listening", "reading"):
        task = plan.get(f"{skill}_task") or {}
        done = progress.get(skill)
        if not done:
            continue
        questions = [
            {"type": q["type"], "question": q["question"], "correct": q["options"][q["answer_index"]], "hers": q["options"][q["chosen"]] if q.get("chosen") is not None else None, "ok": q["ok"]}
            for q in done.get("questions", [])
        ]
        content = "\n".join(f"{l['speaker']}: {l['text']}" for l in task.get("lines", [])) if skill == "listening" else task.get("text", "")
        materials[skill] = {"headline": task.get("headline", ""), "content": content, "questions": questions, "score": done.get("score"), "total": done.get("total")}
    if progress.get("writing"):
        materials["writing"] = {"task": plan.get("writing_task") or {}, "text": progress["writing"]["text"]}
    if progress.get("speaking"):
        materials["speaking"] = {"role": plan.get("tutor_role", ""), "turns": progress["speaking"]["turns"]}
    return materials


@login_required
@require_POST
def checkpoint_finish(request, lesson_id):
    """All steps done: assess, store the Checkpoint, overwrite the learner's levels (spec 7c)."""
    lesson = own_lesson(request, lesson_id)
    if lesson.skill != "checkpoint":
        return JsonResponse({"error": "Not a checkpoint"}, status=409)
    if lesson.status == Lesson.Status.ANALYZED:
        return _hx_redirect(request, f"/lessons/{lesson.id}/report/")
    steps, progress, current = checkpoint_state(lesson)
    if current is not None:
        return redirect("lessons:runner", lesson_id=lesson.id)
    learner = lesson.learner
    previous = {s: getattr(learner, f"cefr_{s}") for s in level_assessor.SKILLS}
    try:
        assessment = level_assessor.assess(checkpoint_materials(lesson), target_level=learner.target_level, previous=previous, lesson_id=lesson.id)
    except client.AIUnavailable as exc:
        log.error("assessment failed for lesson %s: %s", lesson.id, exc)
        return render(request, "lessons/_checkpoint_error.html", {"lesson": lesson, "error": str(exc)}, status=503)

    checkpoint = Checkpoint.objects.create(learner=learner, lesson=lesson, results={**assessment.results, "overall": assessment.overall_estimate, "previous": previous}, report_es=assessment.report_es)
    changed = []
    for skill, result in assessment.results.items():
        if result["assessed"] and result["estimate"]:
            setattr(learner, f"cefr_{skill}", level_assessor.base_level(result["estimate"]))
            changed.append(f"cefr_{skill}")
    if not learner.placement_done:
        learner.placement_done = True
        changed.append("placement_done")
    if changed:
        learner.save(update_fields=changed)
    lesson.status = Lesson.Status.ANALYZED
    lesson.completed_at = timezone.now()
    if lesson.started_at:
        lesson.duration_seconds = int((lesson.completed_at - lesson.started_at).total_seconds())
    lesson.save(update_fields=["status", "completed_at", "duration_seconds"])
    LessonReport.objects.update_or_create(
        lesson=lesson,
        defaults={
            "summary_es": assessment.report_es,
            "strengths": [],
            "focus_next": [g for r in assessment.results.values() for g in r["gaps_to_target"]][:3],
            "raw_analysis": {"checkpoint_id": checkpoint.id, "assessment": assessment.raw, "_meta": {"cefr_signal": {"estimate": assessment.overall_estimate}, "new_error_ids": [], "recycled_error_ids": [], "avoided_error_ids": [], "prompt_tokens": assessment.prompt_tokens, "completion_tokens": assessment.completion_tokens}},
        },
    )
    return _hx_redirect(request, f"/lessons/{lesson.id}/report/")


# --------------------------------------------------------------------------- text mini-lesson (module 15)


def mini_lesson_card(lesson):
    return (lesson.plan or {}).get("mini_lesson_card") or None


def mini_lesson_context(lesson):
    card = mini_lesson_card(lesson)
    if not card or not card.get("exercises"):
        return {"mini": None}
    return {"mini": card, "mini_url": f"/lessons/{lesson.id}/mini-lesson/", "mini_result": card.get("result")}


@login_required
@require_POST
def mini_lesson_check(request, lesson_id):
    """Grade the three completion sentences; wrong ones go to the file with confidence high."""
    lesson = own_lesson(request, lesson_id)
    card = mini_lesson_card(lesson)
    if not card or not card.get("exercises"):
        return JsonResponse({"error": "This lesson has no mini-lesson card"}, status=409)
    if card.get("result"):
        return render(request, "lessons/_mini_lesson.html", {"lesson": lesson, **mini_lesson_context(lesson)})
    answers = {ex["id"]: (request.POST.get(f"ex{ex['id']}") or "").strip() for ex in card["exercises"]}
    if any(not a for a in answers.values()):
        return render(request, "lessons/_mini_lesson.html", {"lesson": lesson, **mini_lesson_context(lesson), "mini_answers": answers, "mini_error": "Fill in all three before checking."}, status=422)
    _start(lesson)
    grammar_topic = {"title": lesson.grammar_topic.title, "summary_es": lesson.grammar_topic.summary_es} if lesson.grammar_topic else None
    try:
        results, found = minilesson.check(card["exercises"], answers, grammar_topic=grammar_topic, lesson_id=lesson.id)
    except client.AIUnavailable as exc:
        return render(request, "lessons/_mini_lesson.html", {"lesson": lesson, **mini_lesson_context(lesson), "mini_answers": answers, "mini_error": f"Could not check right now ({exc}). Try again."}, status=503)
    new, recycled = postprocess.record_errors(lesson, found, confidence="high")
    result = {
        "results": results,
        "score": sum(1 for r in results if r["correct"]),
        "total": len(results),
        "new_error_ids": [e.id for e in new],
        "recycled_error_ids": [e.id for e in recycled],
    }
    lesson.plan = {**lesson.plan, "mini_lesson_card": {**card, "result": result}}
    lesson.save(update_fields=["plan"])
    return render(request, "lessons/_mini_lesson.html", {"lesson": lesson, **mini_lesson_context(lesson)})


def attach_mini_lesson(lesson, report):
    result = (mini_lesson_card(lesson) or {}).get("result")
    if result:
        postprocess.attach_extra_errors(report, new_ids=result.get("new_error_ids", []), recycled_ids=result.get("recycled_error_ids", []))


# --------------------------------------------------------------------------- listening


def listening_task(lesson):
    return (lesson.plan or {}).get("listening_task") or {}


def audio_ready(task):
    lines = task.get("lines", [])
    segments = task.get("segments", [])
    return bool(lines) and len(segments) == len(lines) and all(s.get("url") for s in segments)


def listening_context(request, lesson, *, answers=None, error=""):
    plan = lesson.plan or {}
    task = listening_task(lesson)
    return {
        "section": "listening",
        "title": plan.get("title") or lesson.title,
        "lesson": lesson,
        "plan": plan,
        "task": task,
        "answers": answers or {},
        "error": error,
        "listens_left": max(0, task.get("max_listens", 2) - task.get("listens", 0)),
        **mini_lesson_context(lesson),
        "config": {
            "segments": [s["url"] for s in task.get("segments", [])],
            "listens": task.get("listens", 0),
            "max_listens": task.get("max_listens", 2),
            "listened_url": f"/lessons/{lesson.id}/listened/",
        },
    }


def listening_runner(request, lesson):
    task = listening_task(lesson)
    if not audio_ready(task):
        return render(request, "lessons/listening_audio.html", {"section": "listening", "title": lesson.title, "lesson": lesson, "task": task})
    return render(request, "lessons/listening.html", listening_context(request, lesson))


@login_required
@require_POST
def listening_audio(request, lesson_id):
    """Voice every line of the script (two voices for dialogues), once. Called by HTMX on load."""
    lesson = own_lesson(request, lesson_id)
    if lesson.skill not in ("listening", "checkpoint"):
        return JsonResponse({"error": "Not a listening lesson"}, status=409)
    task = listening_task(lesson)
    runner_url = f"/lessons/{lesson.id}/"
    if audio_ready(task):
        return _hx_redirect(request, runner_url)
    segments = {s["line_id"]: s for s in task.get("segments", []) if s.get("url")}
    try:
        for line in task.get("lines", []):
            if line["id"] in segments:
                continue
            audio = client.speak(line["text"], voice=line["voice"], instructions=LISTENING_VOICE_INSTRUCTIONS, purpose="listening", lesson_id=lesson.id)
            turn = Turn.objects.create(lesson=lesson, role=Turn.Role.TUTOR, text=f"{line['speaker']}: {line['text']}", phase="listening", sequence=next_sequence(lesson))
            turn.audio_file.save(f"listening-{lesson.id}-{line['id']}.mp3", ContentFile(audio), save=True)
            segments[line["id"]] = {"line_id": line["id"], "url": turn.audio_file.url}
            # Save as we go so a failure halfway keeps what was voiced.
            task["segments"] = [segments[l["id"]] for l in task["lines"] if l["id"] in segments]
            lesson.plan = {**lesson.plan, "listening_task": task}
            lesson.save(update_fields=["plan"])
    except client.AIUnavailable as exc:
        log.error("listening audio failed for lesson %s: %s", lesson.id, exc)
        return render(request, "lessons/_audio_error.html", {"lesson": lesson, "error": str(exc)}, status=503)
    return _hx_redirect(request, runner_url)


LISTENING_VOICE_INSTRUCTIONS = (
    "Natural conversational English at a normal native pace, with the rhythm of real speech. "
    "Sound like a person in the situation, not a narrator."
)


@login_required
@require_POST
def listening_listened(request, lesson_id):
    """Count one full play-through; the interface stops at max_listens (spec 5.2)."""
    lesson = own_lesson(request, lesson_id)
    task = listening_task(lesson)
    listens = min(task.get("listens", 0) + 1, task.get("max_listens", 2))
    lesson.plan = {**lesson.plan, "listening_task": {**task, "listens": listens}}
    lesson.save(update_fields=["plan"])
    return JsonResponse({"listens": listens, "max_listens": task.get("max_listens", 2)})


@login_required
@require_POST
def listening_submit(request, lesson_id):
    """Grade in code, write a report, reveal the transcript. No learner output, so no errors to file."""
    lesson = own_lesson(request, lesson_id)
    if lesson.skill != "listening":
        return JsonResponse({"error": "Not a listening lesson"}, status=409)
    if lesson.status == Lesson.Status.ANALYZED:
        return redirect("lessons:report", lesson_id=lesson.id)
    task = listening_task(lesson)
    answers = {}
    for q in task.get("questions", []):
        raw = request.POST.get(f"q{q['id']}")
        if raw is not None and raw.isdigit():
            answers[q["id"]] = int(raw)
    if any(q["id"] not in answers for q in task.get("questions", [])):
        return render(request, "lessons/listening.html", listening_context(request, lesson, answers=answers, error="Answer every question before handing in."), status=422)

    _start(lesson)
    score, rows = grade_questions(task, answers)
    for row, q in zip(rows, task.get("questions", [])):
        row["evidence"] = q.get("evidence", "")
    total = len(rows)
    missed_types = sorted({r["type"] for r in rows if not r["ok"]})
    summary = f"Escuchaste \"{task.get('headline', '')}\" y acertaste {score} de {total}."
    if score == total:
        summary += " Todo correcto: el audio estaba a tu alcance. La próxima puede ir un paso más rápido."
    elif missed_types:
        names = {"gist": "idea general", "detail": "detalle", "inference": "inferencia"}
        summary += " Fallaste en " + ", ".join(names[t] for t in missed_types) + ". Mirá la transcripción: las respuestas están marcadas."
    focus = [f"Listening: {t} questions" for t in missed_types]
    lesson.status = Lesson.Status.ANALYZED
    lesson.completed_at = timezone.now()
    if lesson.started_at:
        lesson.duration_seconds = int((lesson.completed_at - lesson.started_at).total_seconds())
    lesson.save(update_fields=["status", "completed_at", "duration_seconds"])
    report, _ = LessonReport.objects.update_or_create(
        lesson=lesson,
        defaults={
            "summary_es": summary,
            "strengths": [f"{sum(1 for r in rows if r['ok'] and r['type'] == t)} of {sum(1 for r in rows if r['type'] == t)} {t} questions right" for t in ("gist", "detail", "inference") if any(r["type"] == t for r in rows)],
            "focus_next": focus,
            "raw_analysis": {
                "listening": {"score": score, "total": total, "questions": rows, "listens": task.get("listens", 0)},
                "_meta": {"cefr_signal": {}, "new_error_ids": [], "recycled_error_ids": [], "avoided_error_ids": []},
            },
        },
    )
    attach_mini_lesson(lesson, report)
    return redirect("lessons:report", lesson_id=lesson.id)


def transcript_with_highlights(task, rows):
    """Script lines as HTML with each question's evidence wrapped in <mark>."""
    out = []
    for line in task.get("lines", []):
        text = escape(line["text"])
        for row in rows:
            ev = escape((row.get("evidence") or "").strip())
            if ev and ev in text:
                cls = "ok" if row["ok"] else "miss"
                text = text.replace(ev, f'<mark class="{cls}" title="Question {row["id"]}">{ev}</mark>', 1)
        out.append({"speaker": line["speaker"], "html": mark_safe(text)})
    return out


def reading_context(request, lesson, *, answers=None, production="", error=""):
    plan = lesson.plan or {}
    task = plan.get("reading_task") or {}
    return {
        "section": "reading",
        "title": plan.get("title") or lesson.title,
        "lesson": lesson,
        "plan": plan,
        "task": task,
        "paragraphs": [p.strip() for p in (task.get("text") or "").split("\n\n") if p.strip()],
        "targeted": plan.get("targeted_errors", []),
        "answers": answers or {},
        "production": production,
        "error": error,
        "config": {"vocab_url": f"/lessons/{lesson.id}/vocab/", "glossary": task.get("glossary", [])},
        **mini_lesson_context(lesson),
    }


def reading_runner(request, lesson):
    return render(request, "lessons/reading.html", reading_context(request, lesson))


def grade_questions(task, answers):
    """Multiple choice is graded here, no model involved."""
    rows, score = [], 0
    for q in task.get("questions", []):
        chosen = answers.get(q["id"])
        ok = chosen is not None and chosen == q["answer_index"]
        score += ok
        rows.append({
            "id": q["id"], "type": q["type"], "question": q["question"], "options": q["options"],
            "chosen": chosen, "answer_index": q["answer_index"], "ok": ok, "explanation": q.get("explanation", ""),
        })
    return score, rows


@login_required
@require_POST
def reading_submit(request, lesson_id):
    """Grade the questions, correct the short production, write the file, show the report."""
    lesson = own_lesson(request, lesson_id)
    if lesson.skill != "reading":
        return JsonResponse({"error": "Not a reading lesson"}, status=409)
    if lesson.status == Lesson.Status.ANALYZED:
        return redirect("lessons:report", lesson_id=lesson.id)
    task = (lesson.plan or {}).get("reading_task") or {}
    answers = {}
    for q in task.get("questions", []):
        raw = request.POST.get(f"q{q['id']}")
        if raw is not None and raw.isdigit():
            answers[q["id"]] = int(raw)
    production = (request.POST.get("production") or "").strip()
    missing = [q["id"] for q in task.get("questions", []) if q["id"] not in answers]
    if missing or len(production.split()) < 15:
        error = "Answer every question and write at least 15 words before handing in." if missing else "Write at least 15 words in your answer."
        return render(request, "lessons/reading.html", reading_context(request, lesson, answers=answers, production=production, error=error), status=422)

    _start(lesson)
    turn = lesson.turns.filter(role=Turn.Role.LEARNER).order_by("-sequence").first()
    if turn is None or turn.text != production:
        turn = Turn.objects.create(lesson=lesson, role=Turn.Role.LEARNER, text=production, phase="practice", sequence=next_sequence(lesson))
    score, rows = grade_questions(task, answers)

    plan = lesson.plan or {}
    targeted = [
        {"id": e.id, "learner_produced": e.learner_produced, "correction": e.correction, "subcategory": e.subcategory}
        for e in ErrorItem.objects.filter(id__in=plan.get("targeted_error_ids", []))
    ]
    writing_task = {
        "format": "short written answer after reading",
        "prompt": task.get("production_prompt", ""),
        "context": task.get("headline", ""),
        "target_words_min": task.get("production_words_min", 60),
        "target_words_max": task.get("production_words_max", 120),
        "must_use_vocabulary": task.get("production_terms", []),
    }
    try:
        result = writing.correct_text(
            production, task=writing_task, cefr=lesson.learner.cefr_for("reading"), target_level=lesson.learner.target_level,
            targeted_errors=targeted, lesson_id=lesson.id,
        )
    except client.AIUnavailable as exc:
        log.error("reading correction failed for lesson %s: %s", lesson.id, exc)
        return render(request, "lessons/reading.html", reading_context(
            request, lesson, answers=answers, production=production,
            error=f"The corrector is unavailable right now ({exc}). Your answers are kept; try again in a moment.",
        ), status=503)

    lesson.status = Lesson.Status.COMPLETED
    lesson.completed_at = timezone.now()
    if lesson.started_at:
        lesson.duration_seconds = int((lesson.completed_at - lesson.started_at).total_seconds())
    lesson.save(update_fields=["status", "completed_at", "duration_seconds"])
    result.analysis.cefr_signal = {**result.analysis.cefr_signal, "skill": "reading"}
    result.analysis.raw = {
        **result.analysis.raw,
        "reading": {"score": score, "total": len(rows), "questions": rows, "glossary_terms": [g["term"] for g in task.get("glossary", [])]},
        "writing": {
            "original_text": production,
            "corrected_text": result.corrected_text,
            "upgraded_text": result.upgraded_text,
            "upgrade_notes_es": result.upgrade_notes_es,
        },
    }
    summary = postprocess.apply_analysis(lesson, result.analysis, confidence="high")
    attach_mini_lesson(lesson, summary.report)
    return redirect("lessons:report", lesson_id=lesson.id)


@login_required
@require_POST
def vocab_lookup(request, lesson_id):
    """A tapped word: glossary first, else the model; either way it lands in the file as a target (spec 5.3)."""
    lesson = own_lesson(request, lesson_id)
    word = (request.POST.get("word") or "").strip().lower().strip(".,;:!?\"'()[]")
    sentence = (request.POST.get("sentence") or "").strip()[:400]
    if not word or len(word) > 60:
        return JsonResponse({"error": "No word"}, status=400)
    task = (lesson.plan or {}).get("reading_task") or (lesson.plan or {}).get("listening_task") or {}
    entry = next((g for g in task.get("glossary", []) if g.get("term", "").lower() == word), None)
    if entry is None:
        # "rolled" should find "roll back": match on shared stems of at least four letters.
        def matches(term):
            return any(len(min(word, part, key=len)) >= 4 and (word.startswith(part) or part.startswith(word)) for part in term.lower().split())

        entry = next((g for g in task.get("glossary", []) if matches(g.get("term", ""))), None)
    if entry:
        data = {"term": entry["term"].lower(), "definition_en": entry.get("definition_en", ""), "example": entry.get("example", ""), "note_es": "", "source": "glossary"}
    else:
        try:
            data = {**vocab.define(word, sentence=sentence, cefr=lesson.learner.cefr_for("reading"), lesson_id=lesson.id), "source": "model"}
        except client.AIUnavailable as exc:
            return JsonResponse({"error": f"Could not look that up ({exc})."}, status=503)
    item, created = VocabItem.objects.get_or_create(
        learner=lesson.learner, term=data["term"],
        defaults={"definition_en": data["definition_en"], "example_sentence": data["example"], "track": lesson.track, "status": VocabItem.Status.TARGET},
    )
    if not created and not item.definition_en and data["definition_en"]:
        item.definition_en, item.example_sentence = data["definition_en"], data["example"]
        item.save(update_fields=["definition_en", "example_sentence"])
    return JsonResponse({**data, "status": item.status, "created": created})


def writing_runner(request, lesson):
    plan = lesson.plan or {}
    task = plan.get("writing_task") or {}
    draft = lesson.turns.filter(role=Turn.Role.LEARNER).order_by("-sequence").first()
    context = {
        "section": "writing",
        "title": plan.get("title") or lesson.title,
        "lesson": lesson,
        "plan": plan,
        "task": task,
        "targeted": plan.get("targeted_errors", []),
        "hints": plan.get("if_stuck_hints", []),
        "draft": draft.text if draft else "",
        "error": request.GET.get("error", ""),
        **mini_lesson_context(lesson),
    }
    return render(request, "lessons/writing.html", context)


@login_required
@require_POST
def writing_submit(request, lesson_id):
    """The learner hands in the text: correct it, write the file, show the report."""
    lesson = own_lesson(request, lesson_id)
    if lesson.skill != "writing":
        return JsonResponse({"error": "Not a writing lesson"}, status=409)
    if lesson.status == Lesson.Status.ANALYZED:
        return redirect("lessons:report", lesson_id=lesson.id)
    text = (request.POST.get("text") or "").strip()
    if len(text.split()) < 20:
        return render(request, "lessons/writing.html", {
            "section": "writing", "title": lesson.title, "lesson": lesson, "plan": lesson.plan or {},
            "task": (lesson.plan or {}).get("writing_task") or {}, "targeted": (lesson.plan or {}).get("targeted_errors", []),
            "hints": (lesson.plan or {}).get("if_stuck_hints", []), "draft": text,
            "error": "Write at least 20 words before handing it in.",
        }, status=422)
    _start(lesson)
    # Keep the submission even if the corrector fails; a retry reuses it.
    turn = lesson.turns.filter(role=Turn.Role.LEARNER).order_by("-sequence").first()
    if turn is None or turn.text != text:
        turn = Turn.objects.create(lesson=lesson, role=Turn.Role.LEARNER, text=text, phase="practice", sequence=next_sequence(lesson))
    try:
        result = writing.correct(lesson, text)
    except client.AIUnavailable as exc:
        log.error("writing correction failed for lesson %s: %s", lesson.id, exc)
        return render(request, "lessons/writing.html", {
            "section": "writing", "title": lesson.title, "lesson": lesson, "plan": lesson.plan or {},
            "task": (lesson.plan or {}).get("writing_task") or {}, "targeted": (lesson.plan or {}).get("targeted_errors", []),
            "hints": (lesson.plan or {}).get("if_stuck_hints", []), "draft": text,
            "error": f"The corrector is unavailable right now ({exc}). Your text is saved; try again in a moment.",
        }, status=503)
    lesson.status = Lesson.Status.COMPLETED
    lesson.completed_at = timezone.now()
    if lesson.started_at:
        lesson.duration_seconds = int((lesson.completed_at - lesson.started_at).total_seconds())
    lesson.save(update_fields=["status", "completed_at", "duration_seconds"])
    result.analysis.raw = {
        **result.analysis.raw,
        "writing": {
            "original_text": text,
            "corrected_text": result.corrected_text,
            "upgraded_text": result.upgraded_text,
            "upgrade_notes_es": result.upgrade_notes_es,
        },
    }
    summary = postprocess.apply_analysis(lesson, result.analysis, confidence="high")
    attach_mini_lesson(lesson, summary.report)
    return redirect("lessons:report", lesson_id=lesson.id)


def speaking_runner(request, lesson, checkpoint=False):
    plan = lesson.plan or {}
    elapsed = int((timezone.now() - lesson.started_at).total_seconds()) if lesson.started_at else 0
    phase_key, _ = current_phase_key(plan, elapsed)
    # Voiced listening lines and written answers are turns too, but not conversation.
    turns = list(lesson.turns.exclude(phase__in=NON_CONVERSATION_PHASES).order_by("sequence"))
    context = {
        "section": "speaking",
        "title": plan.get("title") or lesson.title,
        "lesson": lesson,
        "plan": plan,
        "phases": plan.get("phases", []),
        "turns": [{"role": t.role, "text": t.text, "html": tutor_line(t.text) if t.role == "tutor" else None} for t in turns],
        "targeted": plan.get("targeted_errors", []),
        "hints": plan.get("if_stuck_hints", []),
        "checkpoint": checkpoint,
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
def analyzing(request, lesson_id):
    """Spinner page shown while the analyzer runs (spec 4.4). It posts to `analyze` on load."""
    lesson = own_lesson(request, lesson_id)
    if lesson.status == Lesson.Status.ANALYZED:
        return redirect("lessons:report", lesson_id=lesson.id)
    if lesson.status != Lesson.Status.COMPLETED:
        return redirect("lessons:runner", lesson_id=lesson.id)
    return render(request, "lessons/analyzing.html", {"section": "speaking", "title": "Analyzing your lesson", "lesson": lesson})


@login_required
@require_POST
def analyze(request, lesson_id):
    """Run analyzer + postprocess synchronously, then send the browser to the report.

    Called by HTMX from the analyzing page; answers with HX-Redirect on success
    and an error partial (with a retry button) on failure. The lesson stays
    `completed` until analysis succeeds, so nothing is lost on a timeout (spec 12).
    """
    lesson = own_lesson(request, lesson_id)
    report_url = f"/lessons/{lesson.id}/report/"
    if lesson.status == Lesson.Status.ANALYZED:
        return _hx_redirect(request, report_url)
    if lesson.status != Lesson.Status.COMPLETED:
        return JsonResponse({"error": f"Lesson is {lesson.status}, not completed"}, status=409)
    if not lesson.turns.filter(role=Turn.Role.LEARNER).exists():
        # Nothing to analyze: close the lesson with an empty report.
        LessonReport.objects.update_or_create(
            lesson=lesson, defaults={"summary_es": "No hablaste en esta clase, así que no hay nada para analizar."}
        )
        lesson.status = Lesson.Status.ANALYZED
        lesson.save(update_fields=["status"])
        return _hx_redirect(request, report_url)
    try:
        result = analyzer.analyze(lesson)
        postprocess.apply_analysis(lesson, result)
    except client.AIUnavailable as exc:
        log.error("analysis failed for lesson %s: %s", lesson.id, exc)
        return render(request, "lessons/_analysis_error.html", {"lesson": lesson, "error": str(exc)}, status=503)
    return _hx_redirect(request, report_url)


def _hx_redirect(request, url):
    if request.headers.get("HX-Request"):
        response = JsonResponse({"redirect": url})
        response["HX-Redirect"] = url
        return response
    return redirect(url)


@login_required
def report(request, lesson_id):
    """The post-lesson report (spec 8.3): summary, strengths, new vs recycled errors, avoided, focus."""
    lesson = own_lesson(request, lesson_id)
    if lesson.status != Lesson.Status.ANALYZED:
        return redirect("lessons:runner", lesson_id=lesson.id)
    lesson_report = LessonReport.objects.filter(lesson=lesson).first()
    if lesson_report is None:
        return redirect("lessons:analyzing", lesson_id=lesson.id)
    if lesson.skill == "checkpoint":
        checkpoint = Checkpoint.objects.filter(lesson=lesson).first()
        results = (checkpoint.results if checkpoint else {})
        progress = ((lesson.plan or {}).get("checkpoint") or {}).get("progress", {})
        skills = [
            {"key": s, "title": CHECKPOINT_STEP_TITLES[s], **(results.get(s) or {}), "previous": (results.get("previous") or {}).get(s, ""), "score": (progress.get(s) or {}).get("score"), "total": (progress.get(s) or {}).get("total")}
            for s in level_assessor.SKILLS if (results.get(s) or {}).get("assessed")
        ]
        return render(request, "lessons/checkpoint_report.html", {
            "section": "today", "title": lesson.title, "lesson": lesson, "checkpoint": checkpoint, "report": lesson_report,
            "skills": skills, "overall": results.get("overall", ""), "target": lesson.learner.target_level,
        })
    meta = (lesson_report.raw_analysis or {}).get("_meta", {})
    ids = meta.get("new_error_ids", []) + meta.get("recycled_error_ids", []) + meta.get("avoided_error_ids", [])
    by_id = {e.id: e for e in ErrorItem.objects.filter(learner=lesson.learner, id__in=ids)}

    def pick(key):
        return [by_id[i] for i in meta.get(key, []) if i in by_id]

    learner_turns = lesson.turns.filter(role=Turn.Role.LEARNER)
    reading_data = (lesson_report.raw_analysis or {}).get("reading")
    listening_data = (lesson_report.raw_analysis or {}).get("listening")
    if listening_data:
        listening_data = {**listening_data, "transcript": transcript_with_highlights(listening_task(lesson), listening_data.get("questions", []))}
    writing_data = (lesson_report.raw_analysis or {}).get("writing")
    if writing_data:
        writing_data = {
            **writing_data,
            "diff_html": diff_html(writing_data.get("original_text", ""), writing_data.get("corrected_text", "")),
            "changes": change_count(writing_data.get("original_text", ""), writing_data.get("corrected_text", "")),
            "word_count": len(writing_data.get("original_text", "").split()),
        }
    context = {
        "section": lesson.skill if lesson.skill in ("speaking", "writing", "reading", "listening") else "speaking",
        "writing": writing_data,
        "reading": reading_data,
        "listening": listening_data,
        "mini": (mini_lesson_card(lesson) or {}).get("result"),
        "title": lesson.title,
        "lesson": lesson,
        "report": lesson_report,
        "new_errors": pick("new_error_ids"),
        "recycled_errors": pick("recycled_error_ids"),
        "avoided_errors": pick("avoided_error_ids"),
        "cefr_signal": meta.get("cefr_signal") or {},
        "targeted_count": len((lesson.plan or {}).get("targeted_error_ids", [])),
        "learner_words": sum(t.word_count for t in learner_turns),
        "learner_turns": learner_turns.count(),
    }
    return render(request, "lessons/report.html", context)


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
        audio = client.speak(tutor.for_speech(text), purpose="tutor", lesson_id=lesson.id)
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
            transcript = client.transcribe((audio.name or "turn.webm", audio.read()), duration_ms=duration_ms, purpose="tutor", lesson_id=lesson.id)
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
    return redirect("lessons:analyzing", lesson_id=lesson.id)
