"""Post-lesson analyzer (spec 6, 9.3).

Reads the whole transcript once the lesson ends and returns a validated
`AnalysisResult`. The JSON schema is built from the closed taxonomy, and
anything the model returns outside it is dropped here, never stored.
"""

import json
import logging
from dataclasses import dataclass, field

from lessons.taxonomy import CATEGORIES, SUBCATEGORIES, TAXONOMY

from . import client
from .prompts import load_prompt

log = logging.getLogger("ai.analyzer")

MAX_ERRORS = 8
CEFR_ESTIMATES = ["A1", "A2", "A2+", "B1", "B1+", "B2", "B2+", "C1", "C2"]
CONFIDENCES = ["high", "medium", "low"]

ERROR_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "subcategory": {"type": "string", "enum": SUBCATEGORIES},
        "learner_produced": {"type": "string"},
        "correction": {"type": "string"},
        "explanation_es": {"type": "string"},
        "confidence": {"type": "string", "enum": CONFIDENCES},
        "is_recycled": {"type": "boolean"},
        "recycled_error_id": {"type": ["integer", "null"]},
    },
    "required": [
        "category", "subcategory", "learner_produced", "correction",
        "explanation_es", "confidence", "is_recycled", "recycled_error_id",
    ],
    "additionalProperties": False,
}

# `scan` comes first on purpose: the model fills fields in schema order, so an
# exhaustive pass over the learner turns happens before it decides what to keep.
SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "learner_quote": {"type": "string"},
        "problem": {"type": "string"},
        "spontaneous": {"type": "boolean"},
    },
    "required": ["learner_quote", "problem", "spontaneous"],
    "additionalProperties": False,
}

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "scan": {"type": "array", "items": SCAN_SCHEMA},
        "summary_es": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "errors": {"type": "array", "items": ERROR_SCHEMA},
        "recycled_error_ids_avoided": {"type": "array", "items": {"type": "integer"}},
        "new_vocabulary_produced": {"type": "array", "items": {"type": "string"}},
        "vocabulary_gaps": {"type": "array", "items": {"type": "string"}},
        "focus_next": {"type": "array", "items": {"type": "string"}},
        "cefr_signal": {
            "type": "object",
            "properties": {
                "skill": {"type": "string"},
                "estimate": {"type": "string", "enum": CEFR_ESTIMATES},
                "reasoning": {"type": "string"},
            },
            "required": ["skill", "estimate", "reasoning"],
            "additionalProperties": False,
        },
    },
    "required": [
        "scan", "summary_es", "strengths", "errors", "recycled_error_ids_avoided",
        "new_vocabulary_produced", "vocabulary_gaps", "focus_next", "cefr_signal",
    ],
    "additionalProperties": False,
}


@dataclass
class FoundError:
    category: str
    subcategory: str
    learner_produced: str
    correction: str
    explanation_es: str
    confidence: str
    is_recycled: bool = False
    recycled_error_id: int | None = None


@dataclass
class AnalysisResult:
    summary_es: str
    strengths: list[str]
    errors: list[FoundError]
    recycled_error_ids_avoided: list[int]
    new_vocabulary_produced: list[str]
    vocabulary_gaps: list[str]
    focus_next: list[str]
    cefr_signal: dict
    raw: dict = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0


def format_transcript(turns):
    """`turns` is an iterable of (role, text) or objects with .role/.text/.phase."""
    lines = []
    current_phase = None
    for turn in turns:
        role = turn[0] if isinstance(turn, tuple) else turn.role
        text = turn[1] if isinstance(turn, tuple) else turn.text
        phase = None if isinstance(turn, tuple) else getattr(turn, "phase", None)
        if phase and phase != current_phase:
            lines.append(f"\n[{phase}]")
            current_phase = phase
        label = "Tutor" if role == "tutor" else "Learner"
        lines.append(f"{label}: {text.strip()}")
    return "\n".join(lines).strip()


def build_messages(transcript, *, skill, cefr, target_level="B2", grammar_topic=None, targeted_errors=(), plan_summary=""):
    context = {
        "skill": skill,
        "cefr": cefr,
        "target_level": target_level,
        "grammar_topic": grammar_topic or None,
        "targeted_errors": [
            {
                "id": e["id"],
                "learner_produced": e["learner_produced"],
                "correction": e["correction"],
                "category": e.get("category", ""),
                "subcategory": e.get("subcategory", ""),
            }
            for e in targeted_errors
        ],
        "plan": plan_summary,
        "transcript": transcript,
    }
    return [
        {"role": "system", "content": load_prompt("analyzer")},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2)},
    ]


def validate(data, *, targeted_ids=(), skill=""):
    """Turn raw model output into an AnalysisResult, dropping anything off-taxonomy."""
    targeted_ids = set(targeted_ids)
    errors = []
    for raw in data.get("errors", []):
        category, subcategory = raw.get("category"), raw.get("subcategory")
        if subcategory not in TAXONOMY.get(category, ()):
            log.warning("dropping error with invalid taxonomy %s/%s: %r", category, subcategory, raw.get("learner_produced"))
            continue
        produced, correction = raw.get("learner_produced", "").strip(), raw.get("correction", "").strip()
        if not produced or not correction or produced.lower() == correction.lower():
            log.warning("dropping error without a real correction: %r", produced)
            continue
        confidence = raw.get("confidence", "medium")
        if category == "pronunciation":
            confidence = "low"  # inferred from text only (spec 6)
        recycled_id = raw.get("recycled_error_id")
        is_recycled = bool(raw.get("is_recycled")) and recycled_id in targeted_ids
        errors.append(
            FoundError(
                category=category,
                subcategory=subcategory,
                learner_produced=produced,
                correction=correction,
                explanation_es=raw.get("explanation_es", "").strip(),
                confidence=confidence,
                is_recycled=is_recycled,
                recycled_error_id=recycled_id if is_recycled else None,
            )
        )
    if len(errors) > MAX_ERRORS:
        log.warning("model returned %d errors, keeping the first %d", len(errors), MAX_ERRORS)
        errors = errors[:MAX_ERRORS]

    recycled_in_errors = {e.recycled_error_id for e in errors if e.is_recycled}
    avoided = [
        i for i in dict.fromkeys(data.get("recycled_error_ids_avoided", []))
        if i in targeted_ids and i not in recycled_in_errors
    ]

    signal = data.get("cefr_signal") or {}
    if skill:
        signal = {**signal, "skill": skill}

    return AnalysisResult(
        summary_es=data.get("summary_es", "").strip(),
        strengths=[s.strip() for s in data.get("strengths", []) if s.strip()],
        errors=errors,
        recycled_error_ids_avoided=avoided,
        new_vocabulary_produced=_clean_terms(data.get("new_vocabulary_produced", [])),
        vocabulary_gaps=_clean_terms(data.get("vocabulary_gaps", [])),
        focus_next=[s.strip() for s in data.get("focus_next", []) if s.strip()],
        cefr_signal=signal,
        raw=data,
    )


def _clean_terms(terms):
    seen = {}
    for term in terms:
        key = term.strip().lower()
        if key and key not in seen:
            seen[key] = True
    return list(seen)


def analyze_transcript(transcript, *, skill, cefr, target_level="B2", grammar_topic=None, targeted_errors=(), plan_summary="", lesson_id=None):
    """Analyze a plain transcript string. Used by `analyze()` and by the sample fixture check."""
    messages = build_messages(
        transcript,
        skill=skill,
        cefr=cefr,
        target_level=target_level,
        grammar_topic=grammar_topic,
        targeted_errors=targeted_errors,
        plan_summary=plan_summary,
    )
    chat = client.chat_json(messages, ANALYSIS_SCHEMA, schema_name="lesson_analysis", temperature=0.2, purpose="analyzer", lesson_id=lesson_id)
    result = validate(chat.data, targeted_ids=[e["id"] for e in targeted_errors], skill=skill)
    result.prompt_tokens, result.completion_tokens = chat.prompt_tokens, chat.completion_tokens
    log.info(
        "analysis skill=%s errors=%d avoided=%d estimate=%s tokens=%d+%d",
        skill, len(result.errors), len(result.recycled_error_ids_avoided),
        result.cefr_signal.get("estimate"), chat.prompt_tokens, chat.completion_tokens,
    )
    return result


def analyze(lesson):
    """Analyze a finished Lesson from its turns and plan."""
    from lessons.models import ErrorItem

    turns = list(lesson.turns.order_by("sequence"))
    transcript = format_transcript(turns)
    plan = lesson.plan or {}
    targeted_ids = plan.get("targeted_error_ids", [])
    targeted = [
        {
            "id": e.id,
            "learner_produced": e.learner_produced,
            "correction": e.correction,
            "category": e.category,
            "subcategory": e.subcategory,
        }
        for e in ErrorItem.objects.filter(id__in=targeted_ids)
    ]
    grammar_topic = None
    if lesson.grammar_topic:
        grammar_topic = {"title": lesson.grammar_topic.title, "summary_es": lesson.grammar_topic.summary_es}
    return analyze_transcript(
        transcript,
        skill=lesson.skill,
        cefr=lesson.learner.cefr_for(lesson.skill) if lesson.skill != "checkpoint" else lesson.learner.target_level,
        target_level=lesson.learner.target_level,
        grammar_topic=grammar_topic,
        targeted_errors=targeted,
        plan_summary=plan.get("summary", "") or plan.get("title", ""),
        lesson_id=lesson.id,
    )
