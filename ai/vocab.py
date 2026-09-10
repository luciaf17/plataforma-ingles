"""Word lookups from the reading runner (spec 5.3): a tapped word that is not
in the glossary gets a definition from the model, in the sentence's sense."""

import json
import logging

from . import client
from .prompts import load_prompt

log = logging.getLogger("ai.vocab")

DEFINITION_SCHEMA = {
    "type": "object",
    "properties": {
        "term": {"type": "string"},
        "definition_en": {"type": "string"},
        "example": {"type": "string"},
        "note_es": {"type": "string"},
    },
    "required": ["term", "definition_en", "example", "note_es"],
    "additionalProperties": False,
}


def define(word, *, sentence="", cefr="B1", lesson_id=None):
    """Definition of `word` as used in `sentence`. Returns a dict; raises AIUnavailable."""
    context = {"word": word, "sentence": sentence, "cefr": cefr}
    messages = [
        {"role": "system", "content": load_prompt("define")},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]
    result = client.chat_json(messages, DEFINITION_SCHEMA, schema_name="definition", temperature=0.2, max_tokens=200, purpose="vocab", lesson_id=lesson_id)
    data = result.data
    data["term"] = (data.get("term") or word).strip().lower()
    return data
