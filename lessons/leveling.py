"""Continuous level signal (spec 7c.2).

Every analyzed lesson leaves a `cefr_signal` in its report. One lesson is
noise: a bad day should not lower the level and a good day should not raise
it. So the stored `cefr_<skill>` moves only when **three consecutive lessons
of that skill agree** on a band different from the stored one.

Checkpoints are the official measurement and overwrite levels directly
(lessons/views.checkpoint_finish), so the streak only counts lessons taken
after the last checkpoint.
"""

import logging

from ai.level_assessor import BANDS, base_level

from .models import Lesson, LessonReport

log = logging.getLogger("lessons.leveling")

CONSECUTIVE_NEEDED = 3
SKILLS = ["speaking", "listening", "reading", "writing"]


def signal_of(report):
    """The band this report's analyzer suggested, or '' when there is none."""
    meta = (report.raw_analysis or {}).get("_meta", {})
    estimate = (meta.get("cefr_signal") or {}).get("estimate", "")
    return estimate if estimate in BANDS else ""


def recent_signals(learner, skill, limit=CONSECUTIVE_NEEDED):
    """Bands from the last analyzed lessons of one skill, newest first, only
    counting lessons after the learner's last checkpoint."""
    lessons = Lesson.objects.filter(learner=learner, skill=skill, status=Lesson.Status.ANALYZED)
    last_checkpoint = (
        learner.lessons.filter(skill="checkpoint", status=Lesson.Status.ANALYZED)
        .order_by("-scheduled_for", "-created_at")
        .values_list("completed_at", flat=True)
        .first()
    )
    if last_checkpoint:
        lessons = lessons.filter(completed_at__gt=last_checkpoint)
    reports = LessonReport.objects.filter(lesson__in=lessons).order_by("-lesson__scheduled_for", "-created_at")[:limit]
    return [s for s in (signal_of(r) for r in reports) if s]


def pending_change(learner, skill):
    """What the signal says right now: the agreed band and how many lessons in a row agree."""
    signals = recent_signals(learner, skill)
    if not signals:
        return {"band": "", "streak": 0, "stored": getattr(learner, f"cefr_{skill}")}
    band = signals[0]
    streak = 0
    for s in signals:
        if base_level(s) != base_level(band):
            break
        streak += 1
    return {"band": band, "streak": streak, "stored": getattr(learner, f"cefr_{skill}")}


def apply_signal(learner, skill):
    """Move `cefr_<skill>` when three consecutive lessons agree on a different band.

    Returns (changed, detail) where detail describes what the signal says.
    """
    if skill not in SKILLS:
        return False, {"band": "", "streak": 0, "stored": ""}
    detail = pending_change(learner, skill)
    stored = detail["stored"]
    if detail["streak"] < CONSECUTIVE_NEEDED or not detail["band"]:
        return False, detail
    new_level = base_level(detail["band"])
    if new_level == stored:
        return False, detail
    setattr(learner, f"cefr_{skill}", new_level)
    learner.save(update_fields=[f"cefr_{skill}"])
    log.info("level moved for %s: %s %s -> %s (3 consecutive signals)", learner, skill, stored or "unset", new_level)
    return True, {**detail, "previous": stored, "new": new_level}


def signal_overview(learner):
    """One row per skill for the dashboard: stored level and what the signal says."""
    rows = []
    for skill in SKILLS:
        detail = pending_change(learner, skill)
        agrees = bool(detail["band"]) and base_level(detail["band"]) == detail["stored"]
        rows.append({
            "skill": skill,
            "stored": detail["stored"],
            "band": detail["band"],
            "streak": detail["streak"],
            "needed": CONSECUTIVE_NEEDED,
            "moving": bool(detail["band"]) and not agrees,
        })
    return rows
