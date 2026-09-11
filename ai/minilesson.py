"""Checks the three completion sentences of a text mini-lesson (module 15).

Exact matches are graded in code. Anything else goes to the model once,
which also returns the mistakes in the analyzer's error format so they
land in the file with confidence high.
"""

import json
import logging
import re

from . import client
from .analyzer import ERROR_SCHEMA, FoundError, validate
from .prompts import load_prompt

log = logging.getLogger("ai.minilesson")

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "correct": {"type": "boolean"},
                    "correction": {"type": "string"},
                    "explanation_es": {"type": "string"},
                },
                "required": ["id", "correct", "correction", "explanation_es"],
                "additionalProperties": False,
            },
        },
        "errors": {"type": "array", "items": ERROR_SCHEMA},
    },
    "required": ["results", "errors"],
    "additionalProperties": False,
}

_norm_re = re.compile(r"[^a-z0-9' ]+")


def normalise(text):
    text = (text or "").lower().replace("’", "'")
    text = _norm_re.sub(" ", text)
    return " ".join(text.split())


def matches(exercise, answer):
    given = normalise(answer)
    if not given:
        return False
    options = [exercise["answer"]] + list(exercise.get("accepted", []))
    return any(given == normalise(o) for o in options)


def filled(exercise, answer):
    return exercise["sentence"].replace("___", answer.strip() or "___", 1)


def check_choices(choices, picked):
    """Grade the multiple choice. `picked` maps id -> option index.

    No model: one right answer means code can say so, immediately and for free.
    """
    results = []
    for item in choices:
        chosen = picked.get(item["id"])
        correct = chosen is not None and chosen == item["answer_index"]
        results.append({
            "id": item["id"],
            "sentence": item["sentence"],
            "chosen": chosen,
            "chosen_text": item["options"][chosen] if chosen is not None and 0 <= chosen < len(item["options"]) else "",
            "answer_text": item["options"][item["answer_index"]],
            "correct": correct,
            "explanation_es": "" if correct else item.get("explanation_es", ""),
        })
    return results


def check(exercises, answers, *, grammar_topic=None, lesson_id=None):
    """`answers` maps exercise id -> text. Returns (results, found_errors)."""
    results = []
    pending = []
    for ex in exercises:
        answer = (answers.get(ex["id"]) or "").strip()
        ok = matches(ex, answer)
        results.append({"id": ex["id"], "answer": answer, "correct": ok, "correction": ex["answer"], "explanation_es": "", "matched": ok})
        if not ok:
            pending.append(ex)
    if not pending:
        return results, []

    context = {
        "grammar_topic": grammar_topic,
        "exercises": [
            {"id": ex["id"], "sentence": ex["sentence"], "cue": ex.get("cue", ""), "answer": ex["answer"], "accepted": ex.get("accepted", [])}
            for ex in exercises
        ],
        "answers": [{"id": r["id"], "answer": r["answer"], "matched": r["matched"]} for r in results],
    }
    messages = [
        {"role": "system", "content": load_prompt("minilesson")},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2)},
    ]
    chat = client.chat_json(messages, CHECK_SCHEMA, schema_name="mini_lesson_check", temperature=0.1, purpose="minilesson", lesson_id=lesson_id)
    by_id = {r.get("id"): r for r in chat.data.get("results", [])}
    for r in results:
        judged = by_id.get(r["id"])
        if r["matched"] or not judged:
            continue
        r["correct"] = bool(judged.get("correct"))
        r["correction"] = (judged.get("correction") or r["correction"]).strip()
        r["explanation_es"] = (judged.get("explanation_es") or "").strip() if not r["correct"] else ""
    analysis = validate({"errors": chat.data.get("errors", [])}, skill="")
    errors = analysis.errors
    for e in errors:
        e.confidence = "high"
    log.info("mini-lesson checked lesson=%s judged=%d errors=%d", lesson_id, len(pending), len(errors))
    return results, errors
