"""Thin wrapper over the OpenAI SDK.

Three calls the rest of the project uses: `chat_json` (structured output),
`transcribe` (speech to text) and `speak` (text to speech). Everything here
is synchronous on purpose (spec 2: no workers in v1).

Retries and timeouts come from settings; token usage and latency are logged
under the `ai` logger so cost stays visible.
"""

import json
import logging
import time
from dataclasses import dataclass, field

from django.conf import settings
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

log = logging.getLogger("ai.client")

_client = None


class AIUnavailable(Exception):
    """Raised when the API could not answer after retries. Callers must degrade
    gracefully (spec 12): show text without audio, keep the lesson going."""


def get_client():
    global _client
    if _client is None:
        if not settings.OPENAI_API_KEY:
            raise AIUnavailable("OPENAI_API_KEY is not set")
        _client = OpenAI(
            api_key=settings.OPENAI_API_KEY,
            timeout=settings.OPENAI_TIMEOUT_SECONDS,
            max_retries=settings.OPENAI_MAX_RETRIES,
        )
    return _client


def reset_client():
    """For tests and for key rotation without a restart."""
    global _client
    _client = None


@dataclass
class ChatResult:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    data: dict | list | None = field(default=None)


def _call(label, fn, **kwargs):
    """Run one SDK call, log latency, and translate SDK failures into AIUnavailable."""
    started = time.monotonic()
    try:
        result = fn(**kwargs)
    except (APITimeoutError, APIConnectionError) as exc:
        log.error("%s failed after retries: %s", label, exc)
        raise AIUnavailable(f"{label}: {exc}") from exc
    except RateLimitError as exc:
        log.error("%s rate limited: %s", label, exc)
        raise AIUnavailable(f"{label}: rate limited") from exc
    except APIStatusError as exc:
        log.error("%s returned HTTP %s: %s", label, exc.status_code, exc.message)
        raise AIUnavailable(f"{label}: HTTP {exc.status_code}") from exc
    except APIError as exc:
        log.error("%s error: %s", label, exc)
        raise AIUnavailable(f"{label}: {exc}") from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return result, elapsed_ms


def _record(kind, model, **fields):
    """Persist one call for the cost screen. Never let bookkeeping break a lesson."""
    try:
        from .models import ApiCall

        ApiCall.record(kind, model, **fields)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not record api usage: %s", exc)


def chat(messages, *, model=None, temperature=0.7, max_tokens=None, response_format=None, purpose="", lesson_id=None):
    """Plain chat completion. Returns a ChatResult with the assistant text."""
    model = model or settings.OPENAI_CHAT_MODEL
    kwargs = {"model": model, "messages": messages, "temperature": temperature}
    if max_tokens:
        kwargs["max_completion_tokens"] = max_tokens
    if response_format:
        kwargs["response_format"] = response_format

    response, elapsed_ms = _call("chat", get_client().chat.completions.create, **kwargs)
    choice = response.choices[0]
    usage = response.usage
    result = ChatResult(
        content=choice.message.content or "",
        model=response.model,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )
    log.info(
        "chat model=%s tokens=%d+%d latency=%dms finish=%s",
        result.model, result.prompt_tokens, result.completion_tokens, elapsed_ms, choice.finish_reason,
    )
    _record("chat", model, purpose=purpose, lesson_id=lesson_id, prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens, latency_ms=elapsed_ms)
    refusal = getattr(choice.message, "refusal", None)
    if refusal:
        raise AIUnavailable(f"chat: model refused: {refusal}")
    return result


def chat_json(messages, schema, *, schema_name="result", model=None, temperature=0.2, max_tokens=None, purpose="", lesson_id=None):
    """Chat completion constrained to a JSON schema. Returns the parsed object.

    `schema` is a plain JSON-schema dict. Strict mode is on, so the schema must
    follow OpenAI's structured-output rules (all properties required, no
    additionalProperties). The caller owns semantic validation.
    """
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "schema": schema, "strict": True},
    }
    result = chat(messages, model=model, temperature=temperature, max_tokens=max_tokens, response_format=response_format,
                  purpose=purpose or schema_name, lesson_id=lesson_id)
    try:
        result.data = json.loads(result.content)
    except json.JSONDecodeError as exc:
        log.error("chat_json returned invalid JSON: %s", result.content[:200])
        raise AIUnavailable("chat_json: invalid JSON from model") from exc
    return result


# Asking for verbatim output matters: STT models clean up grammar by default,
# which erases exactly the signal the analyzer needs (spec 5.1).
VERBATIM_PROMPT = (
    "Transcribe exactly what is said, word for word, in English. Keep fillers "
    "(um, uh, eh, like), false starts, repetitions, self-corrections and "
    "grammatical mistakes. Do not fix grammar. Do not translate."
)


@dataclass
class TranscriptResult:
    text: str
    model: str
    word_count: int = 0


def transcribe(file, *, prompt=VERBATIM_PROMPT, language="en", model=None, duration_ms=None, purpose="", lesson_id=None):
    """Speech to text. `file` is an open binary file or a (name, bytes) tuple.
    `duration_ms` is only used to estimate cost."""
    model = model or settings.OPENAI_STT_MODEL
    response, elapsed_ms = _call(
        "transcribe",
        get_client().audio.transcriptions.create,
        model=model,
        file=file,
        prompt=prompt,
        language=language,
        response_format="json",
    )
    text = (response.text or "").strip()
    result = TranscriptResult(text=text, model=model, word_count=len(text.split()))
    log.info("transcribe model=%s words=%d latency=%dms", model, result.word_count, elapsed_ms)
    _record("transcribe", model, purpose=purpose, lesson_id=lesson_id, audio_seconds=(duration_ms or 0) / 1000, latency_ms=elapsed_ms)
    return result


DEFAULT_VOICE = "sage"
TUTOR_VOICE_INSTRUCTIONS = (
    "You are a warm, experienced English tutor talking to an adult professional. "
    "Speak clearly at a natural conversational pace, slightly slower than native "
    "small talk. Do not exaggerate."
)


def speak(text, *, voice=DEFAULT_VOICE, instructions=TUTOR_VOICE_INSTRUCTIONS, response_format="mp3", model=None, purpose="", lesson_id=None):
    """Text to speech. Returns the audio bytes."""
    model = model or settings.OPENAI_TTS_MODEL
    response, elapsed_ms = _call(
        "speak",
        get_client().audio.speech.create,
        model=model,
        voice=voice,
        input=text,
        instructions=instructions,
        response_format=response_format,
    )
    audio = response.content
    log.info("speak model=%s voice=%s chars=%d bytes=%d latency=%dms", model, voice, len(text), len(audio), elapsed_ms)
    _record("speak", model, purpose=purpose, lesson_id=lesson_id, characters=len(text), latency_ms=elapsed_ms)
    return audio
