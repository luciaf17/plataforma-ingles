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

from articles import models as articles
from learners.models import GrammarTopic, LearnerGrammarTopic, Topic, Track
from lessons.models import ErrorItem, Lesson, VocabItem

from . import choose_article, client
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
SPEAKING_PHASE_MINUTES = {"warm_up": 3, "mini_lesson": 4, "practice": 8, "drill": 2, "wrap_up": 3}
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
    available = Topic.objects.visible_to(learner).filter(track=track)
    candidates = list(available.exclude(id__in=recent_ids))
    if not candidates:
        candidates = list(available)
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
    request: str = ""
    article: object = None


def select(learner, *, on=None, skill=None, track=None, topic=None, duration=DEFAULT_DURATION, request="", rng=random):
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
        request=(request or "").strip()[:500],
        # Reading lessons quote a real article when the pool has one; with an
        # empty pool the planner writes its own text as before.
        article=choose_article.choose(learner, rng=rng) if skill == "reading" else None,
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
        "fluency_retell": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "rounds": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["prompt", "rounds"],
            "additionalProperties": False,
        },
    },
    "required": ["title", "summary", "tutor_role", "phases", "targeted_errors", "vocabulary", "if_stuck_hints", "fluency_retell"],
    "additionalProperties": False,
}

# Text skills teach the grammar point with a card instead of the tutor's voice (spec 4.1b, module 15).
MINI_LESSON_CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "explanation_en": {"type": "string"},
        "examples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"wrong": {"type": "string"}, "right": {"type": "string"}, "note_en": {"type": "string"}},
                "required": ["wrong", "right", "note_en"],
                "additionalProperties": False,
            },
        },
        "exercises": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sentence": {"type": "string"},
                    "cue": {"type": "string"},
                    "answer": {"type": "string"},
                    "accepted": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["sentence", "cue", "answer", "accepted"],
                "additionalProperties": False,
            },
        },
        "choices": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sentence": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "answer_index": {"type": "integer"},
                    "explanation_es": {"type": "string"},
                },
                "required": ["sentence", "options", "answer_index", "explanation_es"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["explanation_en", "examples", "exercises", "choices"],
    "additionalProperties": False,
}


def normalise_mini_lesson_card(card):
    card = dict(card or {})
    exercises = []
    for index, ex in enumerate(card.get("exercises", [])[:3]):
        sentence = (ex.get("sentence") or "").strip()
        answer = (ex.get("answer") or "").strip()
        if not sentence or not answer or "___" not in sentence:
            continue
        accepted = [a.strip() for a in ex.get("accepted", []) if a and a.strip()]
        exercises.append({"id": index + 1, "sentence": sentence, "cue": (ex.get("cue") or "").strip(), "answer": answer, "accepted": accepted})
    card["exercises"] = exercises

    choices = []
    for index, item in enumerate(card.get("choices", [])[:3]):
        sentence = (item.get("sentence") or "").strip()
        options = [o.strip() for o in item.get("options", []) if o and o.strip()][:4]
        if not sentence or len(options) < 2:
            continue
        answer = item.get("answer_index", 0)
        if not isinstance(answer, int) or not 0 <= answer < len(options):
            answer = 0
        choices.append({
            "id": index + 1, "sentence": sentence, "options": options,
            "answer_index": answer, "explanation_es": (item.get("explanation_es") or "").strip(),
        })
    card["choices"] = choices

    card["examples"] = [e for e in card.get("examples", [])[:3] if e.get("wrong") and e.get("right")]
    card["explanation_en"] = (card.get("explanation_en") or "").strip()
    card.setdefault("result", None)
    return card


WRITING_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        **SPEAKING_PLAN_SCHEMA["properties"],
        "mini_lesson_card": MINI_LESSON_CARD_SCHEMA,
        "writing_task": {
            "type": "object",
            "properties": {
                "format": {"type": "string"},
                "prompt": {"type": "string"},
                "context": {"type": "string"},
                "target_words_min": {"type": "integer"},
                "target_words_max": {"type": "integer"},
                "must_use_vocabulary": {"type": "array", "items": {"type": "string"}},
                "structure_hint": {"type": "string"},
            },
            "required": ["format", "prompt", "context", "target_words_min", "target_words_max", "must_use_vocabulary", "structure_hint"],
            "additionalProperties": False,
        },
    },
    "required": SPEAKING_PLAN_SCHEMA["required"] + ["mini_lesson_card", "writing_task"],
    "additionalProperties": False,
}

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["gist", "detail", "inference"]},
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "answer_index": {"type": "integer"},
        "explanation": {"type": "string"},
    },
    "required": ["type", "question", "options", "answer_index", "explanation"],
    "additionalProperties": False,
}

GLOSSARY_SCHEMA = {
    "type": "object",
    "properties": {
        "term": {"type": "string"},
        "definition_en": {"type": "string"},
        "example": {"type": "string"},
    },
    "required": ["term", "definition_en", "example"],
    "additionalProperties": False,
}

READING_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        **SPEAKING_PLAN_SCHEMA["properties"],
        "mini_lesson_card": MINI_LESSON_CARD_SCHEMA,
        "reading_task": {
            "type": "object",
            "properties": {
                "format": {"type": "string"},
                "headline": {"type": "string"},
                "text": {"type": "string"},
                "glossary": {"type": "array", "items": GLOSSARY_SCHEMA},
                "questions": {"type": "array", "items": QUESTION_SCHEMA},
                "production_prompt": {"type": "string"},
                "production_terms": {"type": "array", "items": {"type": "string"}},
                "production_words_min": {"type": "integer"},
                "production_words_max": {"type": "integer"},
            },
            "required": [
                "format", "headline", "text", "glossary", "questions", "production_prompt",
                "production_terms", "production_words_min", "production_words_max",
            ],
            "additionalProperties": False,
        },
    },
    "required": SPEAKING_PLAN_SCHEMA["required"] + ["mini_lesson_card", "reading_task"],
    "additionalProperties": False,
}

LISTENING_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        **QUESTION_SCHEMA["properties"],
        "evidence": {"type": "string"},
    },
    "required": QUESTION_SCHEMA["required"] + ["evidence"],
    "additionalProperties": False,
}

LISTENING_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        **SPEAKING_PLAN_SCHEMA["properties"],
        "mini_lesson_card": MINI_LESSON_CARD_SCHEMA,
        "listening_task": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["dialogue", "monologue"]},
                "headline": {"type": "string"},
                "setting": {"type": "string"},
                "speakers": {"type": "array", "items": {"type": "string"}},
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"speaker": {"type": "string"}, "text": {"type": "string"}},
                        "required": ["speaker", "text"],
                        "additionalProperties": False,
                    },
                },
                "glossary": {"type": "array", "items": GLOSSARY_SCHEMA},
                "questions": {"type": "array", "items": LISTENING_QUESTION_SCHEMA},
            },
            "required": ["format", "headline", "setting", "speakers", "lines", "glossary", "questions"],
            "additionalProperties": False,
        },
    },
    "required": SPEAKING_PLAN_SCHEMA["required"] + ["mini_lesson_card", "listening_task"],
    "additionalProperties": False,
}

# One schema per skill (spec 9.2).
PLAN_SCHEMAS = {
    "speaking": SPEAKING_PLAN_SCHEMA,
    "writing": WRITING_PLAN_SCHEMA,
    "reading": READING_PLAN_SCHEMA,
    "listening": LISTENING_PLAN_SCHEMA,
}

# Two clearly different voices for dialogues, one for monologues. The tutor keeps "sage".
DIALOGUE_VOICES = ["coral", "onyx"]
MONOLOGUE_VOICE = "verse"
MAX_LISTENS = 2


def normalise_listening_task(task):
    """Speakers get voices, lines get ids and a voice, questions get ids."""
    task = dict(task or {})
    speakers = [s.strip() for s in task.get("speakers", []) if s and s.strip()]
    if task.get("format") == "dialogue":
        speakers = speakers[:2] or ["A", "B"]
        voices = {name: DIALOGUE_VOICES[i % 2] for i, name in enumerate(speakers)}
    else:
        speakers = speakers[:1] or ["Speaker"]
        voices = {speakers[0]: MONOLOGUE_VOICE}
    lines = []
    for index, line in enumerate(task.get("lines", [])):
        text = (line.get("text") or "").strip()
        if not text:
            continue
        speaker = (line.get("speaker") or "").strip()
        if speaker not in voices:
            speaker = speakers[index % len(speakers)] if task.get("format") == "dialogue" else speakers[0]
        lines.append({"id": index + 1, "speaker": speaker, "text": text, "voice": voices[speaker]})
    task["speakers"] = [{"name": name, "voice": voice} for name, voice in voices.items()]
    task["lines"] = lines
    task["questions"] = normalise_reading_task({"questions": task.get("questions", [])})["questions"]
    task["glossary"] = normalise_reading_task({"glossary": task.get("glossary", [])})["glossary"]
    task["word_count"] = sum(len(line["text"].split()) for line in lines)
    task.setdefault("segments", [])
    task.setdefault("listens", 0)
    task["max_listens"] = MAX_LISTENS
    return task


# Spoken English runs at roughly 150 words a minute; the audio should last 2 to 4 minutes.
LISTENING_MIN_WORDS = {10: 200, 15: 260, 20: 320, 30: 420}


def normalise_reading_task(task, article=None, duration=None):
    """Questions get ids, options are trimmed to four, answer_index is kept in range.

    With a real article, the credit and the link travel with the task so the
    screen can show them and the learner can open the original.
    """
    task = dict(task or {})
    if article is not None:
        # The prompt asks the model to copy the article, but only this is
        # guaranteed to be the published wording, so it wins.
        task["text"] = article.excerpt(reading_words_expected(duration or 20))
        task["headline"] = article.title
        task["source_name"] = article.source_name
        task["source_url"] = article.url
        task["article_id"] = article.id
    questions = []
    for index, q in enumerate(task.get("questions", [])):
        options = [o.strip() for o in q.get("options", []) if o and o.strip()][:4]
        if len(options) < 2:
            continue
        answer = q.get("answer_index", 0)
        if not isinstance(answer, int) or not 0 <= answer < len(options):
            answer = 0
        questions.append({**q, "id": index + 1, "options": options, "answer_index": answer})
    task["questions"] = questions
    glossary = []
    for g in task.get("glossary", []):
        term = (g.get("term") or "").strip()
        if term.lower().startswith("to "):
            term = term[3:].strip()
        if term:
            glossary.append({**g, "term": term})
    task["glossary"] = glossary
    task["word_count"] = len((task.get("text") or "").split())
    return task


# Seconds for each round of the fluency retell (4/3/2, scaled to a 20-minute
# class). The drop is the point: the second round leaves no time to translate.
RETELL_ROUNDS = [60, 40]


def normalise_retell(task):
    """The model chooses what she retells; the clock is ours."""
    task = dict(task or {})
    prompt = (task.get("prompt") or "").strip()
    if not prompt:
        return None
    return {"prompt": prompt, "rounds": RETELL_ROUNDS}


# Reading texts below this share of the expected length get one regeneration.
READING_MIN_WORDS = {10: 180, 15: 240, 20: 300, 30: 400}


def reading_words_expected(duration):
    return READING_MIN_WORDS.get(duration, 300)


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
        "learner_request": selection.request or None,
        "article": (
            {
                "title": selection.article.title,
                "source": selection.article.source_name,
                "url": selection.article.url,
                "text": selection.article.excerpt(reading_words_expected(selection.duration)),
            }
            if selection.article
            else None
        ),
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
        "fluency_retell": normalise_retell(data.get("fluency_retell")) if selection.skill == "speaking" else None,
        "writing_task": data.get("writing_task") if selection.skill == "writing" else None,
        "reading_task": (
            normalise_reading_task(data.get("reading_task"), article=selection.article, duration=selection.duration)
            if selection.skill == "reading"
            else None
        ),
        "listening_task": normalise_listening_task(data.get("listening_task")) if selection.skill == "listening" else None,
        "mini_lesson_card": normalise_mini_lesson_card(data.get("mini_lesson_card")) if selection.skill != "speaking" and selection.grammar_topic else None,
        "learner_request": selection.request or "",
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
    if selection.skill == "reading" and selection.article is None:
        expected = reading_words_expected(selection.duration)
        got = (plan.get("reading_task") or {}).get("word_count", 0)
        if got < expected * 0.8:
            log.warning("reading text too short (%d words, expected %d); regenerating once", got, expected)
            messages.append({"role": "assistant", "content": chat.content})
            messages.append({"role": "user", "content": f"The text has {got} words; it must have at least {expected}. Rewrite the whole plan with a full-length text, keeping the same format and topic."})
            retry = client.chat_json(messages, schema, schema_name=f"{selection.skill}_plan", temperature=0.7, purpose="planner")
            retry_plan = normalise_plan(retry.data, learner, selection)
            if (retry_plan.get("reading_task") or {}).get("word_count", 0) > got:
                plan, chat = retry_plan, retry
    if selection.skill == "listening":
        expected = LISTENING_MIN_WORDS.get(selection.duration, 320)
        got = (plan.get("listening_task") or {}).get("word_count", 0)
        if got < expected * 0.8:
            log.warning("listening script too short (%d words, expected %d); regenerating once", got, expected)
            messages.append({"role": "assistant", "content": chat.content})
            messages.append({"role": "user", "content": f"The script has {got} words; it must have at least {expected}. Rewrite the whole plan with a full-length script, same format and topic."})
            retry = client.chat_json(messages, schema, schema_name=f"{selection.skill}_plan", temperature=0.7, purpose="planner")
            retry_plan = normalise_plan(retry.data, learner, selection)
            if (retry_plan.get("listening_task") or {}).get("word_count", 0) > got:
                plan, chat = retry_plan, retry
    plan["_meta"] = {"model": chat.model, "prompt_tokens": chat.prompt_tokens, "completion_tokens": chat.completion_tokens}
    log.info(
        "plan generated skill=%s topic=%r grammar=%s targeted=%d tokens=%d+%d",
        selection.skill, plan["title"], selection.grammar_topic.slug if selection.grammar_topic else None,
        len(plan["targeted_error_ids"]), chat.prompt_tokens, chat.completion_tokens,
    )
    return plan


# --------------------------------------------------------------------------- checkpoint (spec 7c)

CHECKPOINT_EVERY_DAYS = 28
CHECKPOINT_MINUTES = 30
CHECKPOINT_MIN_WORDS = {"listening": 200, "reading": 200}

CHECKPOINT_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "listening_task": LISTENING_PLAN_SCHEMA["properties"]["listening_task"],
        "reading_task": READING_PLAN_SCHEMA["properties"]["reading_task"],
        "writing_task": WRITING_PLAN_SCHEMA["properties"]["writing_task"],
        "speaking": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "opening": {"type": "string"},
                "prompts": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["role", "opening", "prompts"],
            "additionalProperties": False,
        },
    },
    "required": ["title", "summary", "listening_task", "reading_task", "writing_task", "speaking"],
    "additionalProperties": False,
}


def checkpoint_due(learner, on=None):
    """A checkpoint is due 28 days after the last one, or 28 days after the first
    lesson when there has never been one (spec 7c)."""
    from lessons.models import Checkpoint

    on = on or timezone.localdate()
    if learner.lessons.filter(skill="checkpoint", status__in=[Lesson.Status.PLANNED, Lesson.Status.IN_PROGRESS, Lesson.Status.COMPLETED]).exists():
        return False
    last = Checkpoint.objects.filter(learner=learner).order_by("-taken_at").first()
    if last:
        return (on - timezone.localtime(last.taken_at).date()).days >= CHECKPOINT_EVERY_DAYS
    first = finished_lessons(learner).order_by("scheduled_for").values_list("scheduled_for", flat=True).first()
    return bool(first) and (on - first).days >= CHECKPOINT_EVERY_DAYS


@transaction.atomic
def prepare_checkpoint(learner, *, on=None, short=False):
    """A 30-minute checkpoint lesson with four mini-tests. `short` is the
    onboarding version (speaking and writing only)."""
    on = on or timezone.localdate()
    track = Track.objects.filter(slug="work").first() or choose_track(learner)
    recent = recent_topic_titles(learner)
    context = {
        "cefr": {skill: learner.cefr_for(skill) for skill in SKILLS},
        "target_level": learner.target_level,
        "goal": learner.goal_statement or "technical interviews and daily standups",
        "recent_topics": recent,
        "short": short,
    }
    messages = [
        {"role": "system", "content": load_prompt("checkpoint")},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2)},
    ]
    chat = client.chat_json(messages, CHECKPOINT_PLAN_SCHEMA, schema_name="checkpoint_plan", temperature=0.6, purpose="checkpoint")
    data = chat.data
    steps = ["speaking", "writing"] if short else ["listening", "reading", "writing", "speaking"]
    if not short:
        l_words = sum(len((l.get("text") or "").split()) for l in (data.get("listening_task") or {}).get("lines", []))
        r_words = len(((data.get("reading_task") or {}).get("text") or "").split())
        if l_words < CHECKPOINT_MIN_WORDS["listening"] or r_words < CHECKPOINT_MIN_WORDS["reading"]:
            log.warning("checkpoint texts too short (listening %d, reading %d); regenerating once", l_words, r_words)
            messages.append({"role": "assistant", "content": chat.content})
            messages.append({"role": "user", "content": (
                f"The listening script has {l_words} words and the reading text {r_words}. The script needs at least "
                f"{CHECKPOINT_MIN_WORDS['listening']} words and the text at least {CHECKPOINT_MIN_WORDS['reading']}. "
                "Rewrite the whole plan with full-length material, same theme and structure."
            )})
            retry = client.chat_json(messages, CHECKPOINT_PLAN_SCHEMA, schema_name="checkpoint_plan", temperature=0.6, purpose="checkpoint")
            l2 = sum(len((l.get("text") or "").split()) for l in (retry.data.get("listening_task") or {}).get("lines", []))
            r2 = len(((retry.data.get("reading_task") or {}).get("text") or "").split())
            if l2 + r2 > l_words + r_words:
                data, chat = retry.data, retry
    speaking = data.get("speaking") or {}
    plan = {
        "kind": "checkpoint",
        "title": (data.get("title") or "").strip() or ("Placement checkpoint" if short else "Monthly checkpoint"),
        "summary": (data.get("summary") or "").strip(),
        "tutor_role": (speaking.get("role") or "an interviewer").strip(),
        "phases": [
            {"key": "practice", "title": "Interview", "minutes": 6, "tutor_goal": "Find the ceiling: raise the difficulty until she struggles.", "prompts": [p.strip() for p in speaking.get("prompts", []) if p.strip()]},
        ],
        "speaking_opening": (speaking.get("opening") or "").strip(),
        "listening_task": normalise_listening_task(data.get("listening_task")) if "listening" in steps else None,
        "reading_task": normalise_reading_task(data.get("reading_task")) if "reading" in steps else None,
        "writing_task": data.get("writing_task"),
        "mini_lesson_card": None,
        "checkpoint": {"steps": steps, "progress": {}, "short": short},
        "targeted_errors": [],
        "targeted_error_ids": [],
        "vocabulary": [],
        "due_vocab_ids": [],
        "if_stuck_hints": [],
        "skill": "checkpoint",
        "track": track.slug,
        "topic_id": None,
        "grammar_topic_id": None,
        "grammar_reason": "",
        "duration_min": 12 if short else CHECKPOINT_MINUTES,
        "generated_at": timezone.now().isoformat(),
        "_meta": {"model": chat.model, "prompt_tokens": chat.prompt_tokens, "completion_tokens": chat.completion_tokens},
    }
    return Lesson.objects.create(
        learner=learner, track=track, topic=None, grammar_topic=None, skill="checkpoint",
        status=Lesson.Status.PLANNED, plan=plan, scheduled_for=on,
    )


# --------------------------------------------------------------------------- drills (spec 7b: "Drill this now")

DRILL_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "prompts": {"type": "array", "items": {"type": "string"}},
        "wrap_up": {"type": "string"},
    },
    "required": ["title", "summary", "prompts", "wrap_up"],
    "additionalProperties": False,
}
DRILL_MINUTES = 5


@transaction.atomic
def prepare_drill(learner, *, grammar_topic=None, errors=(), on=None, duration=DRILL_MINUTES):
    """A five-minute speaking lesson with a single drill phase on one structure
    and/or a set of errors. Always creates a new lesson; never replaces today's."""
    on = on or timezone.localdate()
    errors = list(errors)
    if grammar_topic is None and not errors:
        raise NothingToPlan("Nothing to drill: no grammar topic and no errors")
    track = choose_track(learner)
    context = {
        "grammar_topic": (
            {"title": grammar_topic.title, "summary_es": grammar_topic.summary_es, "examples": grammar_topic.examples}
            if grammar_topic else None
        ),
        "errors": [{"id": e.id, "learner_produced": e.learner_produced, "correction": e.correction, "subcategory": e.subcategory} for e in errors],
        "cefr": learner.cefr_for("speaking"),
        "goal": learner.goal_statement or "technical interviews and daily standups",
        "duration_min": duration,
    }
    messages = [
        {"role": "system", "content": load_prompt("drill")},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2)},
    ]
    chat = client.chat_json(messages, DRILL_SCHEMA, schema_name="drill_plan", temperature=0.6, purpose="drill")
    data = chat.data
    title = (data.get("title") or "").strip() or f"Drill: {grammar_topic.title if grammar_topic else 'your errors'}"
    plan = {
        "kind": "drill",
        "title": title,
        "summary": (data.get("summary") or "").strip(),
        "tutor_role": "drill coach",
        "phases": [
            {"key": "drill", "title": title, "minutes": max(1, duration - 1), "tutor_goal": "One question, one answer, one correction. Keep the pace.", "prompts": [p.strip() for p in data.get("prompts", []) if p.strip()]},
            {"key": "wrap_up", "title": "Wrap-up", "minutes": 1, "tutor_goal": "Name the structure and what to watch for.", "prompts": [(data.get("wrap_up") or "").strip()]},
        ],
        "targeted_errors": [{"id": e.id, "learner_produced": e.learner_produced, "correction": e.correction, "how_to_elicit": "Ask a question whose answer needs the corrected form."} for e in errors],
        "targeted_error_ids": [e.id for e in errors],
        "vocabulary": [],
        "due_vocab_ids": [],
        "if_stuck_hints": [],
        "skill": "speaking",
        "track": track.slug,
        "topic_id": None,
        "grammar_topic_id": grammar_topic.id if grammar_topic else None,
        "grammar_reason": "drill",
        "duration_min": duration,
        "generated_at": timezone.now().isoformat(),
        "_meta": {"model": chat.model, "prompt_tokens": chat.prompt_tokens, "completion_tokens": chat.completion_tokens},
    }
    return Lesson.objects.create(
        learner=learner, track=track, topic=None, grammar_topic=grammar_topic, skill="speaking",
        status=Lesson.Status.PLANNED, plan=plan, scheduled_for=on,
    )


# --------------------------------------------------------------------------- entry point


def lesson_for(learner, on, skill=None):
    """Today's open lesson (planned or in progress), optionally for one skill."""
    qs = learner.lessons.filter(scheduled_for=on, status__in=[Lesson.Status.PLANNED, Lesson.Status.IN_PROGRESS])
    if skill:
        qs = qs.filter(skill=skill)
    return qs.order_by("-created_at").first()


@transaction.atomic
def prepare_next_lesson(learner, *, on=None, skill=None, track=None, topic=None, duration=DEFAULT_DURATION, force=False, request="", rng=random):
    """Leave today's lesson in state `planned`. Idempotent: a second call
    returns the same lesson. `force` replaces an unstarted plan (the manual picker)."""
    on = on or timezone.localdate()
    existing = lesson_for(learner, on, skill=skill)
    if existing and not force:
        return existing, False
    if existing and existing.status != Lesson.Status.PLANNED:
        raise ValueError("Today's lesson is already in progress; it cannot be replaced")
    if skill is None and not force and checkpoint_due(learner, on):
        log.info("checkpoint due for %s; preparing it instead of a regular lesson", learner)
        return prepare_checkpoint(learner, on=on), True

    selection = select(learner, on=on, skill=skill, track=track, topic=topic, duration=duration, request=request, rng=rng)
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
    if selection.article is not None:
        articles.mark_used(selection.article, learner, lesson)
    return lesson, True
