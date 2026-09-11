"""Grow the list of class subjects out of one learner's file.

The seeded topics are a starting point written before anybody used the app.
This reads what she has actually been doing — her goal, the things she asked
for in her own words, the words she keeps reaching for, the errors that keep
coming back — and writes new topics for her, so the rotation follows her
instead of a list decided in advance.

Proposed topics belong to the learner they were written for. Seeded ones,
with no learner, stay shared.
"""

import json
import logging

from learners.models import Topic, Track
from lessons.models import ErrorItem, VocabItem

from . import client
from .prompts import load_prompt

log = logging.getLogger("ai.propose_topics")

# Enough new ground to last a couple of weeks without flooding the picker.
HOW_MANY = 3
# Below this many unused topics in a track, she is about to start repeating.
LOW_WATER = 4

TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "track": {"type": "string", "enum": ["work", "general"]},
                    "seed_vocabulary": {"type": "array", "items": {"type": "string"}},
                    "reason_es": {"type": "string"},
                },
                "required": ["title", "description", "track", "seed_vocabulary", "reason_es"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}


def unused_count(learner, track):
    """Topics in this track she has never had a lesson on."""
    used = set(learner.lessons.values_list("topic_id", flat=True))
    return Topic.objects.visible_to(learner).filter(track=track).exclude(id__in=used).count()


def available_to(learner):
    """Every topic she can be given: the shared ones plus her own."""
    return Topic.objects.visible_to(learner)


def is_running_low(learner):
    return any(unused_count(learner, track) < LOW_WATER for track in Track.objects.all())


def context(learner):
    requests = [
        (lesson.plan or {}).get("learner_request", "").strip()
        for lesson in learner.lessons.order_by("-scheduled_for")[:20]
    ]
    return {
        "goal": learner.goal_statement or "technical interviews and daily standups",
        "cefr_speaking": learner.cefr_for("speaking"),
        "target_level": learner.target_level,
        "she_asked_for": [r for r in requests if r][:6],
        "recent_class_topics": list(
            learner.lessons.exclude(topic=None).order_by("-scheduled_for").values_list("topic__title", flat=True)[:10]
        ),
        "existing_topics": list(available_to(learner).values_list("title", flat=True)),
        "stubborn_errors": [
            {"produced": e.learner_produced, "correction": e.correction, "times": e.occurrences}
            for e in ErrorItem.objects.filter(learner=learner, status=ErrorItem.Status.ACTIVE).order_by("-occurrences")[:8]
        ],
        "words_she_reached_for": list(
            VocabItem.objects.filter(learner=learner, status=VocabItem.Status.TARGET)
            .order_by("-created_at")
            .values_list("term", flat=True)[:15]
        ),
    }


def propose(learner, *, how_many=HOW_MANY):
    """Write and store new topics for this learner. Returns the created rows."""
    chat = client.chat_json(
        [
            {"role": "system", "content": load_prompt("propose_topics")},
            {"role": "user", "content": json.dumps({**context(learner), "how_many": how_many}, ensure_ascii=False, indent=2)},
        ],
        TOPIC_SCHEMA,
        schema_name="proposed_topics",
        temperature=0.8,
        purpose="propose_topics",
    )

    tracks = {track.slug: track for track in Track.objects.all()}
    existing = {title.lower() for title in available_to(learner).values_list("title", flat=True)}
    created = []
    for row in chat.data.get("topics", [])[:how_many]:
        title = (row.get("title") or "").strip()
        track = tracks.get(row.get("track"))
        if not title or track is None or title.lower() in existing:
            log.info("skipping proposed topic %r", title)
            continue
        created.append(
            Topic.objects.create(
                track=track,
                title=title[:160],
                description=(row.get("description") or "").strip(),
                seed_vocabulary=[t.strip() for t in row.get("seed_vocabulary", []) if t and t.strip()][:10],
                learner=learner,
                proposed_reason=(row.get("reason_es") or "").strip(),
            )
        )
        existing.add(title.lower())
    log.info("proposed %d topic(s) for %s", len(created), learner)
    return created


def propose_if_needed(learner, *, how_many=HOW_MANY):
    """Only when she is close to repeating herself. Returns the created rows."""
    if not is_running_low(learner):
        return []
    log.info("%s is running low on unused topics; proposing more", learner)
    return propose(learner, how_many=how_many)
