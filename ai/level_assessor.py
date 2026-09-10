"""Checkpoint assessor (spec 7c): places the learner per skill with CEFR
can-do descriptors, returning estimate, evidence and gaps to the target.
It does not write errors to the file; it sets levels."""

import json
import logging
from dataclasses import dataclass, field

from . import client
from .prompts import load_prompt

log = logging.getLogger("ai.level_assessor")

BANDS = ["A2", "B1", "B1+", "B2", "B2+", "C1"]
SKILLS = ["listening", "reading", "writing", "speaking"]

SKILL_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "estimate": {"type": "string", "enum": BANDS},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "gaps_to_target": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
        "assessed": {"type": "boolean"},
    },
    "required": ["estimate", "evidence", "gaps_to_target", "reasoning", "assessed"],
    "additionalProperties": False,
}

ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        **{skill: SKILL_RESULT_SCHEMA for skill in SKILLS},
        "overall_estimate": {"type": "string", "enum": BANDS},
        "report_es": {"type": "string"},
    },
    "required": SKILLS + ["overall_estimate", "report_es"],
    "additionalProperties": False,
}


@dataclass
class Assessment:
    results: dict
    overall_estimate: str
    report_es: str
    raw: dict = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0


def cap_overall(overall, results):
    """Overall never exceeds the best production skill: comprehension alone does not make a B2."""
    production = [results[s]["estimate"] for s in ("speaking", "writing") if results[s]["assessed"] and results[s]["estimate"] in BANDS]
    if overall not in BANDS or not production:
        return overall
    ceiling = max(production, key=BANDS.index)
    return ceiling if BANDS.index(overall) > BANDS.index(ceiling) else overall


def base_level(band):
    """'B1+' -> 'B1' for the Learner's CEFR fields, which have no plus bands."""
    return band.rstrip("+")


def build_messages(materials, *, target_level, previous):
    context = {"target_level": target_level, "previous": previous, **materials}
    return [
        {"role": "system", "content": load_prompt("level_assessor")},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2)},
    ]


def validate(data, skills_present):
    results = {}
    for skill in SKILLS:
        raw = data.get(skill) or {}
        assessed = skill in skills_present and bool(raw.get("assessed", True))
        results[skill] = {
            "estimate": raw.get("estimate", "") if assessed else "",
            "evidence": [e.strip() for e in raw.get("evidence", []) if e and e.strip()][:3] if assessed else [],
            "gaps_to_target": [g.strip() for g in raw.get("gaps_to_target", []) if g and g.strip()] if assessed else [],
            "reasoning": (raw.get("reasoning") or "").strip() if assessed else "",
            "assessed": assessed,
        }
    return results


def assess(materials, *, target_level="B2", previous=None, lesson_id=None):
    """`materials` has one key per skill present (see views.checkpoint_materials)."""
    skills_present = [s for s in SKILLS if materials.get(s)]
    messages = build_messages(materials, target_level=target_level, previous=previous or {})
    chat = client.chat_json(messages, ASSESSMENT_SCHEMA, schema_name="level_assessment", temperature=0.2, purpose="assessor", lesson_id=lesson_id)
    data = chat.data
    results = validate(data, skills_present)
    assessment = Assessment(
        results=results,
        overall_estimate=cap_overall(data.get("overall_estimate", ""), results),
        report_es=(data.get("report_es") or "").strip(),
        raw=data,
        prompt_tokens=chat.prompt_tokens,
        completion_tokens=chat.completion_tokens,
    )
    log.info("assessment lesson=%s overall=%s %s", lesson_id, assessment.overall_estimate, {k: v["estimate"] for k, v in assessment.results.items() if v["assessed"]})
    return assessment
