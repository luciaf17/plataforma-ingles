"""Pick the reading of the day for one learner instead of drawing it at random.

The pool holds whatever the feeds published; most of it is not interesting to
any given person. This asks the model to choose from the headlines, using her
goal, what she has been doing in class, and — the strongest signal — the
articles she has read and the ones she pressed "read something else" on.

Cheap on purpose: titles and sources only, never the article bodies. Any
failure falls back to the random pick, so a reading lesson is never blocked.
"""

import json
import logging

from articles import models as articles

from . import client
from .prompts import load_prompt

log = logging.getLogger("ai.choose_article")

# Enough to give the model a real choice without paying for a long prompt.
CANDIDATES = 20

CHOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "article_id": {"type": "integer"},
        "why": {"type": "string"},
    },
    "required": ["article_id", "why"],
    "additionalProperties": False,
}


def context(learner, candidates):
    from lessons.models import VocabItem

    return {
        "goal": learner.goal_statement or "technical interviews and daily standups",
        "cefr_reading": learner.cefr_for("reading"),
        "recent_class_topics": list(
            learner.lessons.exclude(topic=None).order_by("-scheduled_for").values_list("topic__title", flat=True)[:6]
        ),
        "words_she_is_learning": list(
            VocabItem.objects.filter(learner=learner).order_by("-created_at").values_list("term", flat=True)[:15]
        ),
        "taste": articles.taste(learner),
        "candidates": [
            {"id": a.id, "title": a.title, "source": a.source_name, "words": a.word_count}
            for a in candidates
        ],
    }


def choose(learner, *, rng=None):
    """The article she should read today, or None when the pool is empty."""
    pool = list(articles.Article.objects.unused_by(learner)[:CANDIDATES])
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]

    by_id = {a.id: a for a in pool}
    try:
        chat = client.chat_json(
            [
                {"role": "system", "content": load_prompt("choose_article")},
                {"role": "user", "content": json.dumps(context(learner, pool), ensure_ascii=False, indent=2)},
            ],
            CHOICE_SCHEMA,
            schema_name="article_choice",
            temperature=0.4,
            purpose="choose_article",
        )
    except client.AIUnavailable as exc:
        log.warning("could not choose an article (%s); falling back to a random one", exc)
        return articles.pick_for(learner) if rng is None else articles.pick_for(learner, rng=rng)

    chosen = by_id.get(chat.data.get("article_id"))
    if chosen is None:
        log.warning("model chose article %r, which was not on offer", chat.data.get("article_id"))
        return articles.pick_for(learner) if rng is None else articles.pick_for(learner, rng=rng)
    log.info("article chosen for %s: %r (%s)", learner, chosen.title, chat.data.get("why", "")[:120])
    return chosen
