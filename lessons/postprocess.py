"""Turn an `AnalysisResult` into rows of the student file (spec 6, post-processing).

- New errors become ErrorItem rows in box 0, due tomorrow.
- Errors we already have (same taxonomy pair, similar text) get `occurrences += 1`
  and drop a box. No duplicate rows, ever.
- Targeted errors the learner avoided climb a box; box 5 means mastered.
- Vocabulary the learner produced moves target -> emerging -> acquired.
- The day's grammar topic is promoted through introduced / practicing / mastered.
- A LessonReport is written with the counts and the raw analysis.

Everything runs in one transaction. Running it twice on the same lesson is
safe: the second pass only bumps occurrences.
"""

import logging
import re
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from learners.models import LearnerGrammarTopic

from . import srs
from .models import ErrorItem, Lesson, LessonReport, VocabItem

log = logging.getLogger("lessons.postprocess")

# Token overlap above this makes two learner_produced strings "the same error".
SIMILARITY_THRESHOLD = 0.5
# Spontaneous uses needed to call a word acquired.
ACQUIRED_AFTER_USES = 3
# A grammar topic is mastered after being targeted this many times and avoided this many (spec 7b).
TOPIC_MASTERED_TARGETED = 4
TOPIC_MASTERED_AVOIDED = 3

FILLERS = {"um", "uh", "eh", "mmm", "mm", "hmm", "like", "ehm", "erm", "este", "eeh"}


@dataclass
class PostprocessSummary:
    new_errors: list = field(default_factory=list)
    recycled_errors: list = field(default_factory=list)
    avoided_errors: list = field(default_factory=list)
    vocab_touched: list = field(default_factory=list)
    grammar_topic_status: str = ""
    report: LessonReport | None = None

    @property
    def counts(self):
        return {
            "new": len(self.new_errors),
            "recycled": len(self.recycled_errors),
            "avoided": len(self.avoided_errors),
        }


_token_re = re.compile(r"[a-z0-9']+")


def tokens(text):
    return set(_token_re.findall(text.lower().replace("’", "'")))


def similarity(a, b):
    """Overlap coefficient: shared tokens over the smaller set. Short spans
    like "depends of" still match "it depends of the client"."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def find_existing(learner, found, recycled_id=None):
    """The active ErrorItem this finding repeats, or None."""
    candidates = ErrorItem.objects.filter(
        learner=learner,
        status=ErrorItem.Status.ACTIVE,
        category=found.category,
        subcategory=found.subcategory,
    )
    if recycled_id:
        match = candidates.filter(id=recycled_id).first()
        if match:
            return match
    best, best_score = None, 0.0
    for item in candidates:
        score = similarity(item.learner_produced, found.learner_produced)
        if score > best_score:
            best, best_score = item, score
    return best if best_score > SIMILARITY_THRESHOLD else None


def record_error(lesson, found, *, today, now, confidence=None):
    """Create or bump one ErrorItem. Returns (item, created)."""
    learner = lesson.learner
    existing = find_existing(learner, found, recycled_id=found.recycled_error_id if found.is_recycled else None)
    if existing:
        existing.occurrences += 1
        existing.srs_box = max(0, existing.srs_box - 1)
        existing.next_review_at = srs.next_review_date(0, today)
        existing.last_seen_at = now
        # Keep the file current: the latest wording is usually the clearest.
        if found.correction:
            existing.correction = found.correction
        if found.explanation_es:
            existing.explanation = found.explanation_es
        existing.save()
        return existing, False

    item = ErrorItem.objects.create(
        learner=learner,
        category=found.category,
        subcategory=found.subcategory,
        learner_produced=found.learner_produced,
        correction=found.correction,
        explanation=found.explanation_es,
        source_lesson=lesson,
        confidence=confidence or found.confidence,
        occurrences=1,
        srs_box=0,
        next_review_at=srs.next_review_date(0, today),
    )
    return item, True


def promote_avoided(learner, error_ids, *, today):
    """Targeted errors that did not come back climb one box (spec 6)."""
    promoted = []
    for item in ErrorItem.objects.filter(learner=learner, id__in=error_ids, status=ErrorItem.Status.ACTIVE):
        item.srs_box = srs.promote(item.srs_box)
        if item.srs_box >= srs.MASTERED_BOX:
            item.status = ErrorItem.Status.MASTERED
        item.next_review_at = srs.next_review_date(item.srs_box, today)
        item.save()
        promoted.append(item)
    return promoted


def record_vocabulary(lesson, produced, gaps, *, today):
    learner = lesson.learner
    touched = []
    for term in produced:
        item, created = VocabItem.objects.get_or_create(
            learner=learner,
            term=term.strip().lower(),
            defaults={"track": lesson.track, "status": VocabItem.Status.EMERGING, "times_produced": 0},
        )
        item.times_produced += 1
        if item.times_produced >= ACQUIRED_AFTER_USES:
            item.status = VocabItem.Status.ACQUIRED
        elif item.status == VocabItem.Status.TARGET:
            item.status = VocabItem.Status.EMERGING
        item.srs_box = srs.promote(item.srs_box)
        item.next_review_at = srs.next_review_date(item.srs_box, today)
        item.save()
        touched.append(item)
    for term in gaps:
        item, created = VocabItem.objects.get_or_create(
            learner=learner,
            term=term.strip().lower(),
            defaults={"track": lesson.track, "status": VocabItem.Status.TARGET, "next_review_at": today},
        )
        if not created and item.status != VocabItem.Status.ACQUIRED:
            item.srs_box = 0
            item.next_review_at = today
            item.save()
        touched.append(item)
    return touched


def update_grammar_topic(lesson, errors_this_lesson):
    """Promote the mini-lesson topic. Missed if any error this lesson maps to it."""
    topic = lesson.grammar_topic
    if not topic:
        return ""
    progress, _ = LearnerGrammarTopic.objects.get_or_create(learner=lesson.learner, topic=topic)
    progress.times_targeted += 1
    if progress.introduced_in_id is None:
        progress.introduced_in = lesson
    related = set(topic.related_subcategories or [])
    missed = any(e.subcategory in related for e in errors_this_lesson)
    if not missed:
        progress.times_avoided += 1

    if progress.times_targeted >= TOPIC_MASTERED_TARGETED and progress.times_avoided >= TOPIC_MASTERED_AVOIDED:
        progress.status = LearnerGrammarTopic.Status.MASTERED
    elif progress.times_targeted == 1:
        progress.status = LearnerGrammarTopic.Status.INTRODUCED
    else:
        progress.status = LearnerGrammarTopic.Status.PRACTICING
    progress.save()
    return progress.status


def fluency_metrics(lesson):
    """Words per minute and filler ratio from the learner's turns, when audio durations exist."""
    turns = list(lesson.turns.filter(role="learner"))
    words = sum(t.word_count for t in turns)
    if not words:
        return None, None
    filler_count = sum(1 for t in turns for tok in _token_re.findall(t.text.lower()) if tok in FILLERS)
    filler_ratio = round(filler_count / words, 3)
    duration_ms = sum(t.audio_duration_ms or 0 for t in turns)
    wpm = round(words / (duration_ms / 60000), 1) if duration_ms else None
    return wpm, filler_ratio


@transaction.atomic
def apply_analysis(lesson, result, *, confidence=None):
    """Write the whole analysis into the file. `confidence` forces a level for
    every new error (writing lessons pass "high", spec 5.4)."""
    today = srs.today()
    now = timezone.now()
    summary = PostprocessSummary()

    for found in result.errors:
        item, created = record_error(lesson, found, today=today, now=now, confidence=confidence)
        (summary.new_errors if created else summary.recycled_errors).append(item)

    summary.avoided_errors = promote_avoided(lesson.learner, result.recycled_error_ids_avoided, today=today)
    summary.vocab_touched = record_vocabulary(lesson, result.new_vocabulary_produced, result.vocabulary_gaps, today=today)
    summary.grammar_topic_status = update_grammar_topic(lesson, result.errors)

    wpm, filler_ratio = fluency_metrics(lesson)
    summary.report, _ = LessonReport.objects.update_or_create(
        lesson=lesson,
        defaults={
            "summary_es": result.summary_es,
            "strengths": result.strengths,
            "focus_next": result.focus_next,
            "fluency_wpm": wpm,
            "filler_ratio": filler_ratio,
            "new_errors_count": len(summary.new_errors),
            "recycled_errors_count": len(summary.recycled_errors),
            "errors_avoided_count": len(summary.avoided_errors),
            "raw_analysis": {
                **result.raw,
                "_meta": {
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "cefr_signal": result.cefr_signal,
                    # Which file rows this lesson touched, so the report can show them.
                    "new_error_ids": [e.id for e in summary.new_errors],
                    "recycled_error_ids": [e.id for e in summary.recycled_errors],
                    "avoided_error_ids": [e.id for e in summary.avoided_errors],
                    "vocab_ids": [v.id for v in summary.vocab_touched],
                    "grammar_topic_status": summary.grammar_topic_status,
                },
            },
        },
    )

    lesson.status = Lesson.Status.ANALYZED
    lesson.save(update_fields=["status"])
    log.info("lesson %s postprocessed: %s, grammar topic -> %s", lesson.id, summary.counts, summary.grammar_topic_status or "none")
    return summary
