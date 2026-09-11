"""Numbers for the Today screen (spec 8.1): streak, day count, levels, recent lessons."""

from datetime import timedelta

from django.conf import settings

from ai.level_assessor import BANDS
from learners.models import CEFR_ORDER, GrammarTopic, LearnerGrammarTopic
from lessons import leveling
from lessons.models import Lesson, LessonReport

FINISHED = (Lesson.Status.COMPLETED, Lesson.Status.ANALYZED)
OPEN = (Lesson.Status.PLANNED, Lesson.Status.IN_PROGRESS)
# Rough position of each band on a bar that reaches B2 at ~62% (prototype values).
LEVEL_PCT = {"A1": 12, "A2": 28, "B1": 45, "B2": 62, "C1": 82, "C2": 100}
SKILL_NAMES = [("speaking", "Speaking"), ("listening", "Listening"), ("reading", "Reading"), ("writing", "Writing")]


def finished_days(learner):
    return set(learner.lessons.filter(status__in=FINISHED).values_list("scheduled_for", flat=True))


def streak(learner, today):
    """Consecutive days with a finished lesson, ending today or yesterday."""
    days = finished_days(learner)
    cursor = today if today in days else today - timedelta(days=1)
    count = 0
    while cursor in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def day_number(learner, today):
    first = learner.lessons.filter(status__in=FINISHED).order_by("scheduled_for").values_list("scheduled_for", flat=True).first()
    return (today - first).days + 1 if first else 0


def last_days(learner, today, count=14):
    """Cells for the streak strip: done / today (pending) / empty."""
    days = finished_days(learner)
    cells = []
    for offset in range(count - 1, -1, -1):
        day = today - timedelta(days=offset)
        if day in days:
            state = "done"
        elif day == today:
            state = "today"
        else:
            state = "empty"
        cells.append({"date": day, "state": state})
    return cells


def yesterday_line(learner, today):
    report = (
        LessonReport.objects.filter(lesson__learner=learner, lesson__scheduled_for=today - timedelta(days=1))
        .order_by("-created_at")
        .first()
    )
    if report is None:
        if not learner.lessons.filter(status__in=FINISHED).exists():
            return "Your file is empty. The first lesson fills it."
        return "No lesson yesterday. Today counts double."
    targeted = len((report.lesson.plan or {}).get("targeted_error_ids", []))
    if targeted:
        return f"Yesterday you avoided {report.errors_avoided_count} of your {targeted} targeted errors."
    if report.new_errors_count:
        return f"Yesterday added {report.new_errors_count} new errors to your file."
    return "Yesterday's lesson is in your file."


def skill_levels(learner):
    signals = {row["skill"]: row for row in leveling.signal_overview(learner)}
    rows = []
    for key, name in SKILL_NAMES:
        level = getattr(learner, f"cefr_{key}")
        signal = signals.get(key, {})
        hint, signal_dir = "", ""
        if signal.get("moving"):
            signal_dir = "up" if BANDS.index(signal["band"]) > (BANDS.index(level) if level in BANDS else -1) else "down"
            hint = f"{signal['band']} in {signal['streak']} of the last {signal['needed']} lessons"
        rows.append({
            "key": key, "name": name, "level": level or "—",
            "pct": LEVEL_PCT.get(level[:2], 0) if level else 0,
            "is_target": level[:2] >= learner.target_level if level else False,
            "hint": hint, "direction": signal_dir,
        })
    return rows


def program_progress(learner):
    """Progress to B2 = mastered syllabus topics / all topics up to the target (spec 7b)."""
    total = GrammarTopic.objects.filter(cefr_level__in=CEFR_ORDER[: CEFR_ORDER.index(learner.target_level) + 1]).count()
    mastered = LearnerGrammarTopic.objects.filter(learner=learner, status=LearnerGrammarTopic.Status.MASTERED).count()
    return {"mastered": mastered, "total": total, "pct": round(mastered * 100 / total) if total else 0}


def unfinished_lessons(learner, today):
    """Lessons from earlier days that were planned or started and never handed in."""
    return list(
        learner.lessons.filter(scheduled_for__lt=today, status__in=[Lesson.Status.PLANNED, Lesson.Status.IN_PROGRESS])
        .select_related("track")
        .order_by("-scheduled_for")
    )


# The sidebar entry for a skill prepares today's lesson for it and opens it,
# which is exactly what "start this now" needs to do.
SKILL_URLS = {"speaking": "/speaking/", "listening": "/listening/", "reading": "/reading/", "writing": "/writing/"}


def todays_class(learner, today):
    """The day across the four skills, with the totals so far.

    One lesson a day is what the planner prepares; a full class in the
    traditional sense is all four, so the day is measured against them and the
    three she has not taken on are one tap away.
    """
    lessons = list(learner.lessons.filter(scheduled_for=today).select_related("track").order_by("created_at"))
    reports = {r.lesson_id: r for r in LessonReport.objects.filter(lesson__in=lessons)}
    enabled = [(key, name) for key, name in SKILL_NAMES if key in settings.LESSON_SKILLS_ENABLED]

    rows = []
    for key, name in enabled:
        mine = [lesson for lesson in lessons if lesson.skill == key]
        done = next((lesson for lesson in mine if lesson.status in FINISHED), None)
        open_one = next((lesson for lesson in mine if lesson.status in OPEN), None)
        report = reports.get(done.id) if done else None
        rows.append({
            "key": key,
            "name": name,
            # An open lesson wins the row even when the skill is already done:
            # it is the one that still needs resuming or dropping.
            "lesson": open_one or done,
            "state": "open" if open_one else ("done" if done else "todo"),
            "done": done,
            "also_done": bool(done and open_one),
            "minutes": round((done.duration_seconds or 0) / 60) if done and done.duration_seconds else None,
            "report": report,
            "start_url": SKILL_URLS[key],
        })

    # The bar counts skills practised today, whether or not something else is open.
    done_count = sum(1 for row in rows if row["done"])
    # A checkpoint is not one of the four skills, so it is counted on its own.
    checkpoint = next((lesson for lesson in lessons if lesson.skill == "checkpoint"), None)
    return {
        "rows": rows,
        "done": done_count,
        "total": len(rows),
        "pct": round(done_count * 100 / len(rows)) if rows else 0,
        "checkpoint": checkpoint,
        "minutes": sum(round((lesson.duration_seconds or 0) / 60) for lesson in lessons if lesson.status in FINISHED),
        "errors_avoided": sum(r.errors_avoided_count for r in reports.values()),
        "new_errors": sum(r.new_errors_count for r in reports.values()),
        "recycled_errors": sum(r.recycled_errors_count for r in reports.values()),
        "all_done": bool(rows) and done_count == len(rows),
    }


def recent_lessons(learner, today, limit=5):
    rows = []
    lessons = learner.lessons.filter(status__in=FINISHED).select_related("track", "topic").order_by("-scheduled_for", "-created_at")[:limit]
    for lesson in lessons:
        report = LessonReport.objects.filter(lesson=lesson).first()
        targeted = len((lesson.plan or {}).get("targeted_error_ids", []))
        if report is None:
            result = "not analyzed"
        elif targeted:
            result = f"{report.errors_avoided_count} of {targeted} avoided · {report.new_errors_count} new"
        else:
            result = f"{report.new_errors_count} new"
        delta = (today - lesson.scheduled_for).days
        when = "Today" if delta == 0 else "Yesterday" if delta == 1 else lesson.scheduled_for.strftime("%a %d").replace(" 0", " ")
        minutes = round((lesson.duration_seconds or 0) / 60) or (lesson.plan or {}).get("duration_min", "")
        rows.append({"lesson": lesson, "when": when, "result": result, "minutes": minutes, "report": report})
    return rows
