"""Live speaking tutor (spec 5.1, 9.1).

One call per learner turn or phase change. The tutor gets the plan, the
phase we are in, the targeted errors and the conversation so far, and
answers in three or four spoken sentences.
"""

import json
import logging
import re

from . import client
from .prompts import load_prompt

log = logging.getLogger("ai.tutor")

MAX_HISTORY_TURNS = 40
MAX_REPLY_TOKENS = 220
EVENTS = ("lesson_start", "phase_start", "turn")

_recast_re = re.compile(r"\*(.+?)\*")

CHECKPOINT_RULE = (
    "CHECKPOINT MODE. You are the examiner in the role given, not a teacher. Do not correct, do not recast, "
    "do not repeat her sentences in a fixed form, do not use asterisks. React to the content only, ask the next "
    "question from the prompts (harder each time), one follow-up at most. Two sentences per turn."
)


def phase_info(plan, phase_key, *, elapsed_in_phase_s=0):
    phases = plan.get("phases", [])
    phase = next((p for p in phases if p["key"] == phase_key), None)
    if phase is None:
        phase = phases[0] if phases else {"key": phase_key, "title": phase_key, "tutor_goal": "", "prompts": [], "minutes": 5}
    minutes = int(phase.get("minutes") or 5)
    remaining = max(0, minutes * 60 - int(elapsed_in_phase_s))
    return {
        "key": phase["key"],
        "title": phase.get("title", ""),
        "tutor_goal": phase.get("tutor_goal", ""),
        "prompts": phase.get("prompts", []),
        "minutes": minutes,
        "remaining_seconds": remaining,
        "almost_over": remaining <= 45,
    }


def build_messages(lesson, *, phase_key, event="turn", elapsed_in_phase_s=0, history=None, phase_changed=False):
    learner = lesson.learner
    plan = lesson.plan or {}
    grammar_topic = lesson.grammar_topic
    context = {
        "student": {
            "name": learner.user.first_name or learner.user.get_username(),
            "cefr_speaking": learner.cefr_for("speaking"),
            "target_level": learner.target_level,
            "goal": learner.goal_statement or "technical interviews and daily standups",
        },
        "plan": {
            "kind": plan.get("kind", "lesson"),
            "opening": plan.get("speaking_opening", ""),
            "title": plan.get("title", ""),
            "summary": plan.get("summary", ""),
            "tutor_role": plan.get("tutor_role", ""),
            "phases": [{"key": p["key"], "title": p.get("title", ""), "minutes": p.get("minutes")} for p in plan.get("phases", [])],
            "targeted_errors": plan.get("targeted_errors", []),
            "vocabulary": plan.get("vocabulary", []),
            "if_stuck_hints": plan.get("if_stuck_hints", []),
        },
        "phase": phase_info(plan, phase_key, elapsed_in_phase_s=elapsed_in_phase_s),
        "event": event,
        # True when the clock moved into this phase while she was speaking:
        # bring her into it in this same reply, do not start a separate turn.
        "phase_just_changed": bool(phase_changed),
        "grammar_topic": (
            {"title": grammar_topic.title, "summary_es": grammar_topic.summary_es, "examples": grammar_topic.examples}
            if grammar_topic
            else None
        ),
    }
    messages = [
        {"role": "system", "content": load_prompt("tutor")},
        {"role": "system", "content": "Class context (JSON):\n" + json.dumps(context, ensure_ascii=False, indent=2)},
    ]
    if plan.get("kind") == "checkpoint":
        messages.append({"role": "system", "content": CHECKPOINT_RULE})
    if history is None:
        # Voiced listening lines and written answers are stored as turns but are not conversation.
        history = lesson.turns.exclude(phase__in=["listening", "writing"]).order_by("sequence")
    turns = list(history)[-MAX_HISTORY_TURNS:]
    for turn in turns:
        role = "assistant" if turn.role == "tutor" else "user"
        messages.append({"role": role, "content": turn.text})
    if event != "turn":
        messages.append({"role": "user", "content": f"[{event}: phase {phase_key}]"})
    return messages


def respond(lesson, *, phase_key, event="turn", elapsed_in_phase_s=0, history=None, phase_changed=False):
    """The tutor's next spoken line. Raises AIUnavailable if the model is down."""
    messages = build_messages(lesson, phase_key=phase_key, event=event, elapsed_in_phase_s=elapsed_in_phase_s, history=history, phase_changed=phase_changed)
    result = client.chat(messages, temperature=0.8, max_tokens=MAX_REPLY_TOKENS, purpose="tutor", lesson_id=lesson.id)
    text = result.content.strip()
    log.info("tutor lesson=%s phase=%s event=%s chars=%d", lesson.id, phase_key, event, len(text))
    return text


def for_speech(text):
    """What the TTS voice reads: recast markers removed."""
    return _recast_re.sub(r"\1", text).strip()


def to_html_parts(text):
    """Split a tutor line into [(is_recast, fragment)] for the transcript."""
    parts, last = [], 0
    for match in _recast_re.finditer(text):
        if match.start() > last:
            parts.append((False, text[last:match.start()]))
        parts.append((True, match.group(1)))
        last = match.end()
    if last < len(text):
        parts.append((False, text[last:]))
    return parts
