"""Progress review: one call that reads the last weeks of lessons and the
student file, and answers "how am I doing and what should I reinforce".

This is the Progress screen's "Review my recent lessons" button. It is
generated on demand and stored, so opening the screen costs nothing.
"""

import json
import logging

from django.utils import timezone

from . import client
from .prompts import load_prompt

log = logging.getLogger("ai.review")

MAX_LESSONS = 12
MAX_ERRORS = 20

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_es": {"type": "string"},
        "improving": {"type": "array", "items": {"type": "string"}},
        "stuck": {"type": "array", "items": {"type": "string"}},
        "focus": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary_es", "improving", "stuck", "focus"],
    "additionalProperties": False,
}


def build_context(learner, lessons, errors, mastered, vocabulary, last_checkpoint=None):
    return {
        "target_level": learner.target_level,
        "levels": {skill: getattr(learner, f"cefr_{skill}") or "unknown" for skill in ("speaking", "listening", "reading", "writing")},
        "last_checkpoint": last_checkpoint,
        "period": {
            "from": str(lessons[-1]["date"]) if lessons else None,
            "to": str(lessons[0]["date"]) if lessons else None,
            "lessons": len(lessons),
        },
        "lessons": lessons,
        "errors": errors[:MAX_ERRORS],
        "mastered": mastered,
        "vocabulary": vocabulary,
    }


def review(learner, context):
    messages = [
        {"role": "system", "content": load_prompt("progress_review")},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2, default=str)},
    ]
    chat = client.chat_json(messages, REVIEW_SCHEMA, schema_name="progress_review", temperature=0.4, purpose="review")
    data = chat.data
    clean = lambda key: [line.strip() for line in data.get(key, []) if line and line.strip()]
    log.info("progress review for %s over %d lessons, tokens=%d+%d", learner, context["period"]["lessons"], chat.prompt_tokens, chat.completion_tokens)
    return {
        "summary_es": (data.get("summary_es") or "").strip(),
        "improving": clean("improving"),
        "stuck": clean("stuck"),
        "focus": clean("focus"),
        "raw": {**data, "_meta": {"prompt_tokens": chat.prompt_tokens, "completion_tokens": chat.completion_tokens, "generated_at": timezone.now().isoformat()}},
    }
