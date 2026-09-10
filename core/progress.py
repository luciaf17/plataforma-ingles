"""Numbers for the Progress screen (spec 8.7).

The headline number is the share of targeted errors she avoided: that is
the platform's real measure of progress, not how many lessons she did.
"""

from datetime import timedelta

from django.db.models import Sum

from ai.models import ApiCall
from lessons.models import Checkpoint, ErrorItem, Lesson, LessonReport, Turn, VocabItem

FINISHED = (Lesson.Status.COMPLETED, Lesson.Status.ANALYZED)
CHART_LESSONS = 12
RECENT_DAYS = 30
AVOIDED_WINDOW_DAYS = 7


def _targeted(lesson):
    return len((lesson.plan or {}).get("targeted_error_ids", []))


def headline(learner, today, days=RECENT_DAYS):
    """Lessons in the period, errors mastered, and the share of targeted errors avoided."""
    since = today - timedelta(days=days)
    lessons = learner.lessons.filter(status__in=FINISHED, scheduled_for__gte=since)
    mastered = ErrorItem.objects.filter(learner=learner, status=ErrorItem.Status.MASTERED).count()

    window = today - timedelta(days=AVOIDED_WINDOW_DAYS)
    avoided = targeted = 0
    for report in LessonReport.objects.filter(lesson__learner=learner, lesson__scheduled_for__gte=window).select_related("lesson"):
        n = _targeted(report.lesson)
        if n:
            targeted += n
            avoided += report.errors_avoided_count
    return {
        "lessons": lessons.count(),
        "days": days,
        "mastered": mastered,
        "avoided": avoided,
        "targeted": targeted,
        "avoided_pct": round(avoided * 100 / targeted) if targeted else None,
        "window_days": AVOIDED_WINDOW_DAYS,
    }


def avoided_chart(learner, limit=CHART_LESSONS):
    """One bar per recent lesson that targeted errors: how many it avoided."""
    reports = (
        LessonReport.objects.filter(lesson__learner=learner)
        .select_related("lesson")
        .order_by("-lesson__scheduled_for", "-created_at")
    )
    bars = []
    for report in reports:
        targeted = _targeted(report.lesson)
        if not targeted:
            continue
        ratio = report.errors_avoided_count / targeted
        bars.append({
            "date": report.lesson.scheduled_for,
            "skill": report.lesson.skill,
            "avoided": report.errors_avoided_count,
            "targeted": targeted,
            "height": max(6, round(ratio * 100)),
            "good": ratio >= 0.5,
            "title": f"{report.lesson.scheduled_for:%d %b}: {report.errors_avoided_count} of {targeted} avoided",
        })
        if len(bars) == limit:
            break
    return list(reversed(bars))


def pace(learner, today, days=RECENT_DAYS):
    """Words per minute, filler ratio and how much of the talking was hers."""
    since = today - timedelta(days=days)
    reports = LessonReport.objects.filter(lesson__learner=learner, lesson__skill="speaking", lesson__scheduled_for__gte=since)
    wpm = [r.fluency_wpm for r in reports if r.fluency_wpm]
    fillers = [r.filler_ratio for r in reports if r.filler_ratio is not None]
    turns = Turn.objects.filter(lesson__learner=learner, lesson__skill="speaking", lesson__scheduled_for__gte=since)
    learner_words = turns.filter(role=Turn.Role.LEARNER).aggregate(n=Sum("word_count"))["n"] or 0
    all_words = turns.aggregate(n=Sum("word_count"))["n"] or 0
    return {
        "wpm": round(sum(wpm) / len(wpm)) if wpm else None,
        "filler_pct": round(sum(fillers) / len(fillers) * 100) if fillers else None,
        "share_pct": round(learner_words * 100 / all_words) if all_words else None,
        "learner_words": learner_words,
        "lessons": reports.count(),
    }


def file_counts(learner):
    errors = ErrorItem.objects.filter(learner=learner)
    vocab = VocabItem.objects.filter(learner=learner)
    return {
        "active": errors.filter(status=ErrorItem.Status.ACTIVE).count(),
        "mastered": errors.filter(status=ErrorItem.Status.MASTERED).count(),
        "due": ErrorItem.objects.due_for(learner).count(),
        "vocab_target": vocab.filter(status=VocabItem.Status.TARGET).count(),
        "vocab_emerging": vocab.filter(status=VocabItem.Status.EMERGING).count(),
        "vocab_acquired": vocab.filter(status=VocabItem.Status.ACQUIRED).count(),
    }


def last_checkpoint(learner):
    return Checkpoint.objects.filter(learner=learner).order_by("-taken_at").first()


def review_context(learner, today, limit=None):
    """The material the AI review reads: recent lessons, the open file, vocabulary."""
    from ai.review import MAX_LESSONS

    limit = limit or MAX_LESSONS
    lessons = []
    reports = (
        LessonReport.objects.filter(lesson__learner=learner)
        .select_related("lesson", "lesson__grammar_topic")
        .order_by("-lesson__scheduled_for", "-created_at")[:limit]
    )
    for report in reports:
        lesson = report.lesson
        lessons.append({
            "date": lesson.scheduled_for,
            "skill": lesson.skill,
            "title": lesson.title,
            "grammar_topic": lesson.grammar_topic.title if lesson.grammar_topic else None,
            "teacher_note_es": report.summary_es,
            "strengths": report.strengths,
            "focus_next": report.focus_next,
            "new_errors": report.new_errors_count,
            "recycled_errors": report.recycled_errors_count,
            "avoided": report.errors_avoided_count,
            "targeted": _targeted(lesson),
            "wpm": report.fluency_wpm,
        })
    errors = [
        {
            "learner_produced": e.learner_produced,
            "correction": e.correction,
            "category": f"{e.category}/{e.subcategory}",
            "occurrences": e.occurrences,
            "box": e.srs_box,
            "due": e.is_due,
        }
        for e in ErrorItem.objects.filter(learner=learner, status=ErrorItem.Status.ACTIVE).order_by("-occurrences", "srs_box")
    ]
    mastered = [
        {"learner_produced": e.learner_produced, "correction": e.correction}
        for e in ErrorItem.objects.filter(learner=learner, status=ErrorItem.Status.MASTERED).order_by("-last_seen_at")[:10]
    ]
    counts = file_counts(learner)
    vocabulary = {
        "target": counts["vocab_target"],
        "emerging": counts["vocab_emerging"],
        "acquired": counts["vocab_acquired"],
        "using_on_her_own": list(
            VocabItem.objects.filter(learner=learner, status__in=[VocabItem.Status.EMERGING, VocabItem.Status.ACQUIRED])
            .order_by("-times_produced")
            .values_list("term", flat=True)[:12]
        ),
    }
    checkpoint = last_checkpoint(learner)
    checkpoint_data = None
    if checkpoint:
        checkpoint_data = {
            "taken_at": checkpoint.taken_at.date(),
            "levels": {s: (checkpoint.results.get(s) or {}).get("estimate", "") for s in ("speaking", "listening", "reading", "writing")},
            "report_es": checkpoint.report_es,
        }
    return lessons, errors, mastered, vocabulary, checkpoint_data


def screen(learner, today):
    return {
        "headline": headline(learner, today),
        "chart": avoided_chart(learner),
        "pace": pace(learner, today),
        "counts": file_counts(learner),
        "checkpoint": last_checkpoint(learner),
        "usage": ApiCall.totals(),
    }
