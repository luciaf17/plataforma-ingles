"""Prompt files live in ai/prompts/*.md and are versioned with the code.

Set `<NAME>_PROMPT_PATH` (e.g. ANALYZER_PROMPT_PATH) to an absolute path to
override one while iterating, without touching the repo.
"""

import os
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@lru_cache(maxsize=None)
def load_prompt(name):
    override = os.environ.get(f"{name.upper()}_PROMPT_PATH")
    path = Path(override) if override else PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def clear_prompt_cache():
    load_prompt.cache_clear()
