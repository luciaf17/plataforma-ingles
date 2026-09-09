"""Run the analyzer against fixtures/transcript_sample.md and check that it
finds the planted errors. This is the module 5 acceptance check (spec 13)."""

import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ai import analyzer, client

SAMPLE = Path(settings.BASE_DIR) / "fixtures" / "transcript_sample.md"

# (fragment of what the learner produced, acceptable subcategories)
PLANTED = [
    ("work here since 2021", {"tense", "aspect", "l1_interference"}),
    ("depends of", {"prepositions", "collocation", "l1_interference"}),
    ("actually", {"false_friend", "wrong_word"}),
    ("client don't", {"agreement"}),
    ("explain you", {"word_order", "collocation", "l1_interference"}),
    ("three years working", {"l1_interference", "tense", "aspect"}),
]
MIN_FOUND = 4


def load_sample_transcript(path=SAMPLE):
    """Everything after the `---` separator, keeping [phase] and Tutor/Learner lines."""
    text = path.read_text(encoding="utf-8")
    body = text.split("\n---\n", 1)[1]
    lines = [line for line in body.splitlines() if line.startswith(("Tutor:", "Learner:", "["))]
    return "\n".join(lines).strip()


def match_planted(errors):
    found = {}
    for index, (fragment, subcategories) in enumerate(PLANTED):
        for error in errors:
            produced = error.learner_produced.lower()
            if fragment in produced and error.subcategory in subcategories:
                found[index] = error
                break
    return found


class Command(BaseCommand):
    help = "Analyze fixtures/transcript_sample.md and report which planted errors were found"

    def handle(self, *args, **options):
        transcript = load_sample_transcript()
        try:
            result = analyzer.analyze_transcript(
                transcript,
                skill="speaking",
                cefr="B1",
                target_level="B2",
                grammar_topic={"title": "Present perfect with since / for"},
                targeted_errors=[],
                plan_summary="Interview practice: walk the CTO through the ERP architecture.",
            )
        except client.AIUnavailable as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.MIGRATE_HEADING(f"Errors returned: {len(result.errors)}"))
        for error in result.errors:
            self.stdout.write(f"\n  [{error.category}/{error.subcategory} · {error.confidence}]")
            self.stdout.write(f"  produced:    {error.learner_produced}")
            self.stdout.write(f"  correction:  {error.correction}")
            self.stdout.write(f"  explanation: {error.explanation_es}")

        self.stdout.write(self.style.MIGRATE_HEADING("\nSummary"))
        self.stdout.write(f"  {result.summary_es}")
        self.stdout.write(f"  strengths: {result.strengths}")
        self.stdout.write(f"  focus_next: {result.focus_next}")
        self.stdout.write(f"  vocabulary_gaps: {result.vocabulary_gaps}")
        self.stdout.write(f"  new_vocabulary: {result.new_vocabulary_produced}")
        self.stdout.write(f"  cefr_signal: {result.cefr_signal}")
        self.stdout.write(f"  tokens: {result.prompt_tokens}+{result.completion_tokens}")

        found = match_planted(result.errors)
        self.stdout.write(self.style.MIGRATE_HEADING(f"\nPlanted errors found: {len(found)}/{len(PLANTED)}"))
        for index, (fragment, _) in enumerate(PLANTED):
            mark = self.style.SUCCESS("found  ") if index in found else self.style.ERROR("missing")
            self.stdout.write(f"  {mark} {fragment}")

        if len(found) < MIN_FOUND:
            raise CommandError(f"Only {len(found)} planted errors found; need at least {MIN_FOUND}")
        self.stdout.write(self.style.SUCCESS(f"\nOK: {len(found)} >= {MIN_FOUND}"))
