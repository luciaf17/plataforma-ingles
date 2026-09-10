import json
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from ai import analyzer
from learners.models import GrammarTopic, Learner, LearnerGrammarTopic, Track

from . import postprocess, srs
from .models import ErrorItem, Lesson, LessonReport, Turn, VocabItem

SAMPLE = Path(settings.BASE_DIR) / "fixtures" / "analysis_sample.json"


def load_sample(**overrides):
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    data.update(overrides)
    return data


def found(**overrides):
    base = {
        "category": "grammar",
        "subcategory": "prepositions",
        "learner_produced": "it depends of the client",
        "correction": "it depends on the client",
        "explanation_es": "depend on",
        "confidence": "high",
    }
    base.update(overrides)
    return analyzer.FoundError(**base)


def result_with(errors=(), _targeted=(), **overrides):
    data = load_sample(**{"errors": [], "new_vocabulary_produced": [], "vocabulary_gaps": [], **overrides})
    result = analyzer.validate(data, targeted_ids=_targeted)
    result.errors = list(errors)
    return result


class PostprocessBase(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.learner = Learner.for_user(get_user_model().objects.create_user("lu"))
        self.track = Track.objects.get(slug="work")
        self.topic = GrammarTopic.objects.get(slug="present-perfect-since-for")
        self.today = srs.today()

    def make_lesson(self, **kwargs):
        fields = {"learner": self.learner, "track": self.track, "skill": "speaking", "grammar_topic": self.topic}
        fields.update(kwargs)
        return Lesson.objects.create(**fields)


class SampleFixtureTests(PostprocessBase):
    """The integration test the spec asks for (spec 12): dedup + SRS from a JSON fixture."""

    def test_running_the_same_analysis_twice_bumps_occurrences_without_duplicates(self):
        lesson = self.make_lesson()
        result = analyzer.validate(load_sample())

        first = postprocess.apply_analysis(lesson, result)
        self.assertEqual(first.counts, {"new": 5, "recycled": 0, "avoided": 0})
        self.assertEqual(ErrorItem.objects.filter(learner=self.learner).count(), 5)

        second = postprocess.apply_analysis(self.make_lesson(), analyzer.validate(load_sample()))
        self.assertEqual(second.counts, {"new": 0, "recycled": 5, "avoided": 0})
        self.assertEqual(ErrorItem.objects.filter(learner=self.learner).count(), 5)

        item = ErrorItem.objects.get(learner=self.learner, subcategory="prepositions")
        self.assertEqual(item.occurrences, 2)
        self.assertEqual(item.srs_box, 0)
        self.assertEqual(item.next_review_at, self.today + timedelta(days=1))

    def test_new_errors_start_in_box_zero_due_tomorrow_with_the_analysis_text(self):
        lesson = self.make_lesson()
        postprocess.apply_analysis(lesson, analyzer.validate(load_sample()))

        item = ErrorItem.objects.get(learner=self.learner, subcategory="false_friend")
        self.assertEqual(item.learner_produced, "Actually I'm working in a ERP")
        self.assertEqual(item.correction, "Currently I'm working in an ERP")
        self.assertIn("actualmente", item.explanation)
        self.assertEqual((item.srs_box, item.occurrences, item.status), (0, 1, "active"))
        self.assertEqual(item.next_review_at, self.today + timedelta(days=1))
        self.assertEqual(item.source_lesson, lesson)
        self.assertEqual(item.confidence, "high")

    def test_report_and_lesson_status(self):
        lesson = self.make_lesson()
        Turn.objects.create(lesson=lesson, role="tutor", text="How are you?", sequence=1)
        Turn.objects.create(lesson=lesson, role="learner", text="eh I am fine, like, busy um busy", sequence=2, audio_duration_ms=4000)
        Turn.objects.create(lesson=lesson, role="learner", text="we deploy on Thursday", sequence=3, audio_duration_ms=2000)

        summary = postprocess.apply_analysis(lesson, analyzer.validate(load_sample()))

        lesson.refresh_from_db()
        self.assertEqual(lesson.status, "analyzed")
        report = LessonReport.objects.get(lesson=lesson)
        self.assertEqual(summary.report, report)
        self.assertEqual(report.new_errors_count, 5)
        self.assertEqual(report.recycled_errors_count, 0)
        self.assertIn("since", report.summary_es)
        self.assertEqual(report.strengths[0], "Clear and confident explanation of the deterministic pricing engine.")
        self.assertEqual(report.raw_analysis["cefr_signal"]["estimate"], "B1+")
        self.assertEqual(report.raw_analysis["_meta"]["cefr_signal"]["skill"], "speaking")
        # 12 learner words over 6 seconds = 120 wpm; 3 fillers (eh, like, um) / 12 words.
        self.assertEqual(report.fluency_wpm, 120.0)
        self.assertEqual(report.filler_ratio, 0.25)

    def test_vocabulary_from_sample(self):
        postprocess.apply_analysis(self.make_lesson(), analyzer.validate(load_sample()))

        produced = VocabItem.objects.get(learner=self.learner, term="deterministic")
        self.assertEqual((produced.status, produced.times_produced, produced.track), ("emerging", 1, self.track))
        gap = VocabItem.objects.get(learner=self.learner, term="rq")
        self.assertEqual((gap.status, gap.times_produced, gap.next_review_at), ("target", 0, self.today))


class DeduplicationTests(PostprocessBase):
    def test_similar_text_in_same_subcategory_is_the_same_error(self):
        postprocess.apply_analysis(self.make_lesson(), result_with([found(learner_produced="it depends of the client")]))
        postprocess.apply_analysis(self.make_lesson(), result_with([found(learner_produced="depends of the region")]))

        self.assertEqual(ErrorItem.objects.count(), 1)
        self.assertEqual(ErrorItem.objects.get().occurrences, 2)

    def test_different_subcategory_is_a_different_error(self):
        postprocess.apply_analysis(self.make_lesson(), result_with([found()]))
        postprocess.apply_analysis(self.make_lesson(), result_with([found(subcategory="collocation", category="vocabulary")]))

        self.assertEqual(ErrorItem.objects.count(), 2)

    def test_unrelated_text_in_same_subcategory_is_a_different_error(self):
        postprocess.apply_analysis(self.make_lesson(), result_with([found(learner_produced="it depends of the client")]))
        postprocess.apply_analysis(self.make_lesson(), result_with([found(learner_produced="I arrived to the office", correction="I arrived at the office")]))

        self.assertEqual(ErrorItem.objects.count(), 2)

    def test_recycled_id_wins_over_text_similarity(self):
        original = postprocess.apply_analysis(self.make_lesson(), result_with([found()])).new_errors[0]
        repeat = found(learner_produced="depends of", is_recycled=True, recycled_error_id=original.id)
        summary = postprocess.apply_analysis(self.make_lesson(), result_with([repeat], _targeted=[original.id]))

        self.assertEqual(summary.recycled_errors, [original])
        self.assertEqual(ErrorItem.objects.count(), 1)

    def test_mastered_errors_are_not_matched_but_created_again(self):
        item = postprocess.apply_analysis(self.make_lesson(), result_with([found()])).new_errors[0]
        item.status = "mastered"
        item.save()

        postprocess.apply_analysis(self.make_lesson(), result_with([found()]))
        self.assertEqual(ErrorItem.objects.filter(status="active").count(), 1)
        self.assertEqual(ErrorItem.objects.count(), 2)

    def test_repeat_drops_a_box(self):
        item = postprocess.apply_analysis(self.make_lesson(), result_with([found()])).new_errors[0]
        item.srs_box = 3
        item.save()

        postprocess.apply_analysis(self.make_lesson(), result_with([found()]))
        item.refresh_from_db()
        self.assertEqual(item.srs_box, 2)
        self.assertEqual(item.next_review_at, self.today + timedelta(days=1))

    def test_forced_confidence_for_writing(self):
        lesson = self.make_lesson(skill="writing")
        item = postprocess.apply_analysis(lesson, result_with([found(confidence="medium")]), confidence="high").new_errors[0]
        self.assertEqual(item.confidence, "high")

    def test_similarity_scores(self):
        self.assertGreater(postprocess.similarity("it depends of the client", "depends of"), 0.5)
        self.assertGreater(postprocess.similarity("I work here since 2021", "I work here since 2021 in the company"), 0.5)
        self.assertLessEqual(postprocess.similarity("the people is ready", "the client don't trust"), 0.5)
        self.assertEqual(postprocess.similarity("", "x"), 0.0)


class AvoidedErrorTests(PostprocessBase):
    def test_avoided_errors_climb_a_box_and_review_moves_out(self):
        item = postprocess.apply_analysis(self.make_lesson(), result_with([found()])).new_errors[0]
        item.srs_box, item.next_review_at = 1, self.today
        item.save()

        summary = postprocess.apply_analysis(self.make_lesson(), result_with(recycled_error_ids_avoided=[item.id], _targeted=[item.id]))

        item.refresh_from_db()
        self.assertEqual(summary.avoided_errors, [item])
        self.assertEqual(item.srs_box, 2)
        self.assertEqual(item.next_review_at, self.today + timedelta(days=4))
        self.assertEqual(item.status, "active")
        self.assertEqual(summary.report.errors_avoided_count, 1)

    def test_box_five_is_mastered(self):
        item = postprocess.apply_analysis(self.make_lesson(), result_with([found()])).new_errors[0]
        item.srs_box = 4
        item.save()

        postprocess.apply_analysis(self.make_lesson(), result_with(recycled_error_ids_avoided=[item.id], _targeted=[item.id]))

        item.refresh_from_db()
        self.assertEqual((item.srs_box, item.status), (5, "mastered"))
        self.assertNotIn(item, ErrorItem.objects.due_for(self.learner, on=self.today + timedelta(days=30)))

    def test_avoided_ids_must_belong_to_the_learner(self):
        other = Learner.for_user(get_user_model().objects.create_user("other"))
        other_lesson = Lesson.objects.create(learner=other, track=self.track, skill="speaking")
        foreign = postprocess.apply_analysis(other_lesson, result_with([found()])).new_errors[0]

        summary = postprocess.apply_analysis(self.make_lesson(), result_with(recycled_error_ids_avoided=[foreign.id], _targeted=[foreign.id]))

        foreign.refresh_from_db()
        self.assertEqual(summary.avoided_errors, [])
        self.assertEqual(foreign.srs_box, 0)


class VocabularyTests(PostprocessBase):
    def test_target_word_becomes_emerging_then_acquired(self):
        VocabItem.objects.create(learner=self.learner, term="trade-off", status="target")
        for expected in ("emerging", "emerging", "acquired"):
            postprocess.apply_analysis(self.make_lesson(), result_with(new_vocabulary_produced=["Trade-off"]))
            self.assertEqual(VocabItem.objects.get(term="trade-off").status, expected)
        self.assertEqual(VocabItem.objects.get(term="trade-off").times_produced, 3)

    def test_gap_of_an_acquired_word_is_left_alone(self):
        VocabItem.objects.create(learner=self.learner, term="queue", status="acquired", srs_box=5)
        postprocess.apply_analysis(self.make_lesson(), result_with(vocabulary_gaps=["queue"]))
        item = VocabItem.objects.get(term="queue")
        self.assertEqual((item.status, item.srs_box), ("acquired", 5))


class GrammarTopicTests(PostprocessBase):
    def progress(self):
        return LearnerGrammarTopic.objects.get(learner=self.learner, topic=self.topic)

    def test_first_lesson_introduces_the_topic(self):
        lesson = self.make_lesson()
        status = postprocess.apply_analysis(lesson, result_with()).grammar_topic_status
        progress = self.progress()
        self.assertEqual(status, "introduced")
        self.assertEqual((progress.times_targeted, progress.times_avoided, progress.introduced_in), (1, 1, lesson))

    def test_error_in_related_subcategory_counts_as_missed(self):
        postprocess.apply_analysis(self.make_lesson(), result_with([found(subcategory="tense", learner_produced="I work here since 2021", correction="I've worked here since 2021")]))
        progress = self.progress()
        self.assertEqual((progress.times_targeted, progress.times_avoided), (1, 0))

    def test_mastered_after_four_targeted_and_three_avoided(self):
        miss = found(subcategory="tense", learner_produced="I work here since 2021", correction="I've worked here since 2021")
        postprocess.apply_analysis(self.make_lesson(), result_with([miss]))
        self.assertEqual(self.progress().status, "introduced")
        postprocess.apply_analysis(self.make_lesson(), result_with())
        self.assertEqual(self.progress().status, "practicing")
        postprocess.apply_analysis(self.make_lesson(), result_with())
        self.assertEqual(self.progress().status, "practicing")
        postprocess.apply_analysis(self.make_lesson(), result_with())
        progress = self.progress()
        self.assertEqual((progress.status, progress.times_targeted, progress.times_avoided), ("mastered", 4, 3))

    def test_lesson_without_grammar_topic_changes_nothing(self):
        status = postprocess.apply_analysis(self.make_lesson(grammar_topic=None), result_with()).grammar_topic_status
        self.assertEqual(status, "")
        self.assertEqual(LearnerGrammarTopic.objects.count(), 0)
