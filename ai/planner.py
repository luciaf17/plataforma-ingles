"""Lesson planner (spec 4.1, 4.2, 7b, 9.2).

Two jobs, kept separate:

1. Selection: which skill, track, topic and grammar topic today's lesson gets.
   Pure Python over the student file, no LLM, fully testable.
2. Generation: ask the model for the five-phase plan with the file as context,
   validate it, and store it in `Lesson.plan`. Generated once, never regenerated.

`prepare_next_lesson(learner)` runs both and is idempotent per day.
"""

import json
import logging
import random
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from learners.models import GrammarTopic, LearnerGrammarTopic, Topic, Track
from lessons.models import ErrorItem, Lesson, VocabItem

from . import client
from .prompts import load_prompt

log = logging.getLogger("ai.planner")

SKILLS = ["speaking", "listening", "reading", "writing"]
TRACKS = ["work", "general"]
DEFAULT_DURATION = 20
SPEAKING_PER_WEEK = 3
MAX_DUE_ERRORS = 8
MAX_DUE_VOCAB = 10
RECENT_TOPICS = 5
REVIEW_EVERY = 5  # every fifth lesson reviews a practicing topic instead of a new one (spec 7b)
ERROR_FORCES_TOPIC_AT = 3  # occurrences that make an error override the syllabus (spec 7b rule 1)

PHASES = ["warm_up", "mini_lesson", "practice", "drill", "wrap_up"]
# Minutes per phase for a 20-minute speaking class (spec 4.1b). Scaled for other durations.
SPEAKING_PHASE_MINUTES = {"warm_up": 3, "mini_lesson": 4, "practice": 9, "drill": 3, "wrap_up": 1}
# Text skills: warm-up shrinks to a minute, the rest goes to practice.
TEXT_PHASE_MINUTES = {"warm_up": 1, "mini_lesson": 4, "practice": 11, "drill": 3, "wrap_up": 1}


class NothingToPlan(Exception):
    """No tracks or topics seeded, so no lesson can be built."""


# --------------------------------------------------------------------------- selection


def phase_minutes(skill, duration):
    base = SPEAKING_PHASE_MINUTES if skill == "speaking" else TEXT_PHASE_MINUTES
    total = sum(base.values())
    minutes = {k: max(1, round(v * duration / total)) for k, v in base.items()}
    # Absorb rounding drift in the practice phase so the phases add up to the duration.
    minutes["practice"] += duration - sum(minutes.values())
    return minutes


def finished_lessons(learner):
    return learner.lessons.filter(status__in=[Lesson.Status.COMPLETED, Lesson.Status.ANALYZED])


def choose_skill(learner, on, available=None):
    """The skill with the most days without practice, with speaking at least
    three times a week (spec 4.1)."""
    available = [s for s in (available or settings.LESSON_SKILLS_ENABLED) if s in SKILLS]
    if not available:
        available = ["speaking"]
    if len(available) == 1:
        return available[0]

    last_by_skill = {}
    for skill in available:
        last = finished_lessons(learner).filter(skill=skill).order_by("-scheduled_for").values_list("scheduled_for", flat=True).first()
        last_by_skill[skill] = (on - last).days if last else 10_000

    if "speaking" in available:
        speaking_this_week = finished_lessons(learner).filter(skill="speaking", scheduled_for__gte=on - timedelta(days=6)).count()
        if speaking_this_week < SPEAKING_PER_WEEK and last_by_skill["speaking"] >= 2:
            return "speaking"

    # Ties go to the skill listed first, so speaking wins when nothing was practiced.
    return max(available, key=lambda s: (last_by_skill[s], -available.index(s)))


def choose_track(learner):
    """Alternate work / general, never more than two in a row (spec 4.1)."""
    recent = list(learner.lessons.exclude(skill="checkpoint").order_by("-scheduled_for", "-created_at").values_list("track__slug", flat=True)[:2])
    tracks = {t.slug: t for t in Track.objects.filter(slug__in=TRACKS)}
    if not tracks:
        raise NothingToPlan("No tracks seeded; run `loaddata seed`")
    if len(tracks) == 1:
        return next(iter(tracks.values()))
    if not recent:
        return tracks["work"]
    other = "general" if recent[0] == "work" else "work"
    return tracks[other]


def recent_topic_titles(learner, limit=RECENT_TOPICS):
    return [t for t in learner.lessons.order_by("-scheduled_for", "-created_at").values_list("topic__title", flat=True)[:limit] if t]


def choose_topic(learner, track, rng=random):
    recent_ids = set(learner.lessons.order_by("-scheduled_for", "-created_at").values_list("topic_id", flat=True)[:RECENT_TOPICS])
    candidates = list(Topic.objects.filter(track=track, is_active=True).exclude(id__in=recent_ids))
    if not candidates:
        candidates = list(Topic.objects.filter(track=track, is_active=True))
    if not candidates:
        raise NothingToPlan(f"No active topics for track {track.slug}; run `loaddata seed`")
    return rng.choice(candidates)


def mastered_topic_ids(learner):
    return set(
        LearnerGrammarTopic.objects.filter(learner=learner, status=LearnerGrammarTopic.Status.MASTERED).values_list("topic_id", flat=True)
    )


def topic_for_error(error, learner):
    """The lowest-order unmastered syllabus topic that covers this error's subcategory."""
    mastered = mastered_topic_ids(learner)
    for topic in GrammarTopic.objects.order_by("order"):
        if error.subcategory in (topic.related_subcategories or []) and topic.id not in mastered:
            return topic
    return None


def choose_grammar_topic(learner):
    """Spec 7b, in order: a stubborn error forces its topic; every fifth lesson
    reviews a practicing topic; otherwise the next not-started topic."""
    stubborn = (
        ErrorItem.objects.filter(learner=learner, status=ErrorItem.Status.ACTIVE, occurrences__gte=ERROR_FORCES_TOPIC_AT)
        .order_by("-occurrences", "next_review_at")
    )
    for error in stubborn:
        topic = topic_for_error(error, learner)
        if topic:
            log.info("grammar topic forced by error %s (%dx): %s", error.id, error.occurrences, topic.slug)
            return topic, "error"

    lesson_count = finished_lessons(learner).count()
    if lesson_count and (lesson_count + 1) % REVIEW_EVERY == 0:
        practicing = LearnerGrammarTopic.objects.filter(learner=learner, status=LearnerGrammarTopic.Status.PRACTICING)
        stalest, stalest_date = None, None
        for progress in practicing:
            last = learner.lessons.filter(grammar_topic=progress.topic).order_by("-scheduled_for").values_list("scheduled_for", flat=True).first()
            if stalest is None or (last or timezone.localdate() - timedelta(days=10_000)) < stalest_date:
                stalest, stalest_date = progress.topic, last or timezone.localdate() - timedelta(days=10_000)
        if stalest:
            return stalest, "review"

    started = set(LearnerGrammarTopic.objects.filter(learner=learner).exclude(status=LearnerGrammarTopic.Status.NOT_STARTED).values_list("topic_id", flat=True))
    next_topic = GrammarTopic.objects.exclude(id__in=started).order_by("order").first()
    if next_topic:
        return next_topic, "syllabus"
    # Whole syllabus done: keep cycling through what is still practicing.
    fallback = GrammarTopic.objects.exclude(id__in=mastered_topic_ids(learner)).order_by("order").first()
    return fallback, "syllabus"


@dataclass
class Selection:
    skill: str
    track: Track
    topic: Topic
    grammar_topic: GrammarTopic | None
    grammar_reason: str
    duration: int
    due_errors: list = field(default_factory=list)
    due_vocab: list = field(default_factory=list)
    recent_topics: list = field(default_factory=list)


def select(learner, *, on=None, skill=None, track=None, topic=None, duration=DEFAULT_DURATION, rng=random):
    on = on or timezone.localdate()
    skill = skill or choose_skill(learner, on)
    if isinstance(track, str):
        track = Track.objects.get(slug=track)
    track = track or choose_track(learner)
    if isinstance(topic, int):
        topic = Topic.objects.get(id=topic)
    topic = topic or choose_topic(learner, track, rng=rng)
    grammar_topic, reason = choose_grammar_topic(learner)
    return Selection(
        skill=skill,
        track=track,
        topic=topic,
        grammar_topic=grammar_topic,
        grammar_reason=reason,
        duration=duration,
        due_errors=list(ErrorItem.objects.due_for(learner, on=on)[:MAX_DUE_ERRORS]),
        due_vocab=list(VocabItem.objects.due_for(learner, on=on)[:MAX_DUE_VOCAB]),
        recent_topics=recent_topic_titles(learner),
    )


# --------------------------------------------------------------------------- generation

PHASE_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "enum": PHASES},
        "title": {"type": "string"},
        "tutor_goal": {"type": "string"},
        "prompts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["key", "title", "tutor_goal", "prompts"],
    "additionalProperties": False,
}

SPEAKING_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "tutor_role": {"type": "string"},
        "phases": {"type": "array", "items": PHASE_SCHEMA},
        "targeted_errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "learner_produced": {"type": "string"},
                    "correction": {"type": "string"},
                    "how_to_elicit": {"type": "string"},
                },
                "required": ["id", "learner_produced", "correction", "how_to_elicit"],
                "additionalProperties": False,
            },
        },
        "vocabulary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"term": {"type": "string"}, "how_to_plant": {"type": "string"}},
                "required": ["term", "how_to_plant"],
                "additionalProperties": False,
            },
        },
        "if_stuck_hints": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "tutor_role", "phases", "targeted_errors", "vocabulary", "if_stuck_hints"],
    "additionalProperties": False,
}

# One schema per skill (spec 9.2). Listening / reading / writing land with their runners.
PLAN_SCHEMAS = {"speaking": SPEAKING_PLAN_SCHEMA}


def build_context(learner, selection):
    return {
        "skill": selection.skill,
        "track": selection.track.slug,
        "duration_min": selection.duration,
        "cefr": learner.cefr_for(selection.skill),
        "target_level": learner.target_level,
        "goal": learner.goal_statement or "technical interviews and daily standups",
        "topic": {
            "title": selection.topic.title,
            "description": selection.topic.description,
            "seed_vocabulary": selection.topic.seed_vocabulary,
        },
        "grammar_topic": (
            {
                "title": selection.grammar_topic.title,
                "cefr_level": selection.grammar_topic.cefr_level,
                "summary_es": selection.grammar_topic.summary_es,
                "examples": selection.grammar_topic.examples,
                "why_today": selection.grammar_reason,
            }
            if selection.grammar_topic
            else None
        ),
        "due_errors": [
            {
                "id": e.id,
                "learner_produced": e.learner_produced,
                "correction": e.correction,
                "category": e.category,
                "subcategory": e.subcategory,
                "occurrences": e.occurrences,
            }
            for e in selection.due_errors
        ],
        "due_vocab": [{"term": v.term, "definition_en": v.definition_en, "status": v.status} for v in selection.due_vocab],
        "recent_topics": selection.recent_topics,
        "phases": [{"key": key, "minutes": minutes} for key, minutes in phase_minutes(selection.skill, selection.duration).items()],
    }


def normalise_plan(data, learner, selection):
    """Make the model's plan safe to store: fixed phase order and minutes,
    targeted ids limited to what was actually due, plus bookkeeping."""
    minutes = phase_minutes(selection.skill, selection.duration)
    by_key = {p["key"]: p for p in data.get("phases", [])}
    phases = []
    for key in PHASES:
        phase = by_key.get(key, {"key": key, "title": key.replace("_", " ").title(), "tutor_goal": "", "prompts": []})
        phases.append({**phase, "key": key, "minutes": minutes[key]})

    due_ids = {e.id: e for e in selection.due_errors}
    targeted = []
    for entry in data.get("targeted_errors", []):
        error = due_ids.get(entry.get("id"))
        if not error:
            log.warning("planner targeted unknown error id %s, dropping", entry.get("id"))
            continue
        targeted.append({**entry, "learner_produced": error.learner_produced, "correction": error.correction})

    return {
        "title": data.get("title", "").strip() or selection.topic.title,
        "summary": data.get("summary", "").strip(),
        "tutor_role": data.get("tutor_role", "").strip(),
        "phases": phases,
        "targeted_errors": targeted,
        "targeted_error_ids": [t["id"] for t in targeted],
        "vocabulary": data.get("vocabulary", []),
        "due_vocab_ids": [v.id for v in selection.due_vocab],
        "if_stuck_hints": data.get("if_stuck_hints", []),
        "skill": selection.skill,
        "track": selection.track.slug,
        "topic_id": selection.topic.id,
        "grammar_topic_id": selection.grammar_topic.id if selection.grammar_topic else None,
        "grammar_reason": selection.grammar_reason,
        "duration_min": selection.duration,
        "generated_at": timezone.now().isoformat(),
    }


def generate_plan(learner, selection):
    schema = PLAN_SCHEMAS.get(selection.skill)
    if schema is None:
        raise NotImplementedError(f"No plan schema for skill {selection.skill!r} yet")
    messages = [
        {"role": "system", "content": load_prompt("planner")},
        {"role": "user", "content": json.dumps(build_context(learner, selection), ensure_ascii=False, indent=2)},
    ]
    chat = client.chat_json(messages, schema, schema_name=f"{selection.skill}_plan", temperature=0.7, purpose="planner")
    plan = normalise_plan(chat.data, learner, selection)
    plan["_meta"] = {"model": chat.model, "prompt_tokens": chat.prompt_tokens, "completion_tokens": chat.completion_tokens}
    log.info(
        "plan generated skill=%s topic=%r grammar=%s targeted=%d tokens=%d+%d",
        selection.skill, plan["title"], selection.grammar_topic.slug if selection.grammar_topic else None,
        len(plan["targeted_error_ids"]), chat.prompt_tokens, chat.completion_tokens,
    )
    return plan


# --------------------------------------------------------------------------- entry point


def lesson_for(learner, on):
    """Today's lesson if it exists and has not been finished."""
    return (
        learner.lessons.filter(scheduled_for=on, status__in=[Lesson.Status.PLANNED, Lesson.Status.IN_PROGRESS])
        .order_by("-created_at")
        .first()
    )


@transaction.atomic
def prepare_next_lesson(learner, *, on=None, skill=None, track=None, topic=None, duration=DEFAULT_DURATION, force=False, rng=random):
    """Leave today's lesson in state `planned`. Idempotent: a second call
    returns the same lesson. `force` replaces an unstarted plan (the manual picker)."""
    on = on or timezone.localdate()
    existing = lesson_for(learner, on)
    if existing and not force:
        return existing, False
    if existing and existing.status != Lesson.Status.PLANNED:
        raise ValueError("Today's lesson is already in progress; it cannot be replaced")

    selection = select(learner, on=on, skill=skill, track=track, topic=topic, duration=duration, rng=rng)
    plan = generate_plan(learner, selection)

    if existing:
        existing.delete()
    lesson = Lesson.objects.create(
        learner=learner,
        track=selection.track,
        topic=selection.topic,
        grammar_topic=selection.grammar_topic,
        skill=selection.skill,
        status=Lesson.Status.PLANNED,
        plan=plan,
        scheduled_for=on,
    )
    return lesson, True
