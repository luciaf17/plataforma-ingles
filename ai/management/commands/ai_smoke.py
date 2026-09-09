"""Exercise the three OpenAI calls end to end: chat_json -> speak -> transcribe.

The TTS output is fed back into STT, so a sane transcript proves all three
work without needing a microphone.
"""

import io
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ai import client


class Command(BaseCommand):
    help = "Make one chat_json, one speak and one transcribe call against OpenAI"

    def add_arguments(self, parser):
        parser.add_argument("--keep", type=Path, help="Where to save the generated mp3")

    def handle(self, *args, **options):
        try:
            self.run_smoke(options.get("keep"))
        except client.AIUnavailable as exc:
            raise CommandError(str(exc)) from exc

    def run_smoke(self, keep):
        schema = {
            "type": "object",
            "properties": {
                "sentence": {"type": "string", "description": "One sentence a Spanish speaker often gets wrong in English"},
                "correction": {"type": "string"},
                "category": {"type": "string", "enum": ["grammar", "vocabulary"]},
            },
            "required": ["sentence", "correction", "category"],
            "additionalProperties": False,
        }
        result = client.chat_json(
            [
                {"role": "system", "content": "You are an English teacher for Spanish speakers."},
                {"role": "user", "content": "Give one typical mistake and its correction."},
            ],
            schema,
            schema_name="smoke",
        )
        self.stdout.write(self.style.SUCCESS("chat_json OK"))
        self.stdout.write(f"  {result.data}")
        self.stdout.write(f"  tokens={result.prompt_tokens}+{result.completion_tokens}")

        sentence = result.data["correction"]
        audio = client.speak(f"Let's try it again. {sentence}")
        self.stdout.write(self.style.SUCCESS(f"speak OK ({len(audio)} bytes of mp3)"))
        if keep:
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.write_bytes(audio)
            self.stdout.write(f"  saved to {keep}")

        transcript = client.transcribe(("smoke.mp3", io.BytesIO(audio)))
        self.stdout.write(self.style.SUCCESS("transcribe OK"))
        self.stdout.write(f"  heard: {transcript.text!r} ({transcript.word_count} words)")
