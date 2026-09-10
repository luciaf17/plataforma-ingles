"""Writing corrector (spec 5.4).

One call returns the corrected text (minimal edits, for the diff), the
upgraded native version, and the errors in the analyzer's format, so the
same postprocess writes them into the file with confidence high.
"""

import json
import logging
from dataclasses import dataclass, field

from . import client
from .analyzer import ANALYSIS_SCHEMA, AnalysisResult, validate
from .prompts import load_prompt

log = logging.getLogger("ai.writing")

WRITING_SCHEMA = {
    "type": "object",
    "properties": {
        "corrected_text": {"type": "string"},
        "upgraded_text": {"type": "string"},
        "upgrade_notes_es": {"type": "array", "items": {"type": "string"}},
        **{k: v for k, v in ANALYSIS_SCHEMA["properties"].items() if k != "scan"},
    },
    "required": ["corrected_text", "upgraded_text", "upgrade_notes_es"] + [k for k in ANALYSIS_SCHEMA["required"] if k != "scan"],
    "additionalProperties": False,
}


@dataclass
class WritingResult:
    corrected_text: str
    upgraded_text: str
    upgrade_notes_es: list[str]
    analysis: AnalysisResult
    prompt_tokens: int = 0
    completion_tokens: int = 0


def build_messages(text, *, task, cefr, target_level="B2", grammar_topic=None, targeted_errors=()):
    context = {
        "task": task,
        "cefr": cefr,
        "target_level": target_level,
        "grammar_topic": grammar_topic or None,
        "targeted_errors": [
            {"id": e["id"], "learner_produced": e["learner_produced"], "correction": e["correction"], "subcategory": e.get("subcategory", "")}
            for e in targeted_errors
        ],
        "text": text,
    }
    return [
        {"role": "system", "content": load_prompt("writing")},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2)},
    ]


def correct_text(text, *, task, cefr, target_level="B2", grammar_topic=None, targeted_errors=(), lesson_id=None):
    messages = build_messages(text, task=task, cefr=cefr, target_level=target_level, grammar_topic=grammar_topic, targeted_errors=targeted_errors)
    chat = client.chat_json(messages, WRITING_SCHEMA, schema_name="writing_correction", temperature=0.2, purpose="writing", lesson_id=lesson_id)
    data = chat.data
    analysis = validate(data, targeted_ids=[e["id"] for e in targeted_errors], skill="writing")
    # Writing is the cleanest signal we have (spec 5.4).
    for error in analysis.errors:
        error.confidence = "high"
    analysis.prompt_tokens, analysis.completion_tokens = chat.prompt_tokens, chat.completion_tokens
    corrected = (data.get("corrected_text") or "").strip() or text
    upgraded = (data.get("upgraded_text") or "").strip()
    log.info("writing corrected lesson=%s errors=%d tokens=%d+%d", lesson_id, len(analysis.errors), chat.prompt_tokens, chat.completion_tokens)
    return WritingResult(
        corrected_text=corrected,
        upgraded_text=upgraded,
        upgrade_notes_es=[n.strip() for n in data.get("upgrade_notes_es", []) if n.strip()],
        analysis=analysis,
        prompt_tokens=chat.prompt_tokens,
        completion_tokens=chat.completion_tokens,
    )


def correct(lesson, text):
    """Correct the learner's text for a writing Lesson using its plan and file context."""
    from lessons.models import ErrorItem

    plan = lesson.plan or {}
    targeted = [
        {"id": e.id, "learner_produced": e.learner_produced, "correction": e.correction, "subcategory": e.subcategory}
        for e in ErrorItem.objects.filter(id__in=plan.get("targeted_error_ids", []))
    ]
    grammar_topic = None
    if lesson.grammar_topic:
        grammar_topic = {"title": lesson.grammar_topic.title, "summary_es": lesson.grammar_topic.summary_es}
    return correct_text(
        text,
        task=plan.get("writing_task", {}),
        cefr=lesson.learner.cefr_for("writing"),
        target_level=lesson.learner.target_level,
        grammar_topic=grammar_topic,
        targeted_errors=targeted,
        lesson_id=lesson.id,
    )
