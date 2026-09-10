import random
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from learners.models import GrammarTopic, Learner, LearnerGrammarTopic, Topic, Track
from lessons.models import ErrorItem, Lesson, VocabItem

from . import planner

TODAY = date(2026, 9, 10)


def fake_plan(**overrides):
    base = {
        "title": "Walking an interviewer through your ERP",
        "summary": "Interview role play about the ERP architecture.",
        "tutor_role": "CTO interviewing you",
        "phases": [
            {"key": key, "title": key, "tutor_goal": "goal", "prompts": ["q1", "q2"]}
            for key in ["mini_lesson", "warm_up", "practice", "drill", "wrap_up"]
        ],
        "targeted_errors": [],
        "vocabulary": [{"term": "trade-off", "how_to_plant": "Ask about a trade-off."}],
        "if_stuck_hints": ["The way it works is…"],
    }
    base.update(overrides)
    return base


class PlannerBase(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.learner = Learner.for_user(get_user_model().objects.create_user("lu"))
        self.work = Track.objects.get(slug="work")
        self.general = Track.objects.get(slug="general")

    def lesson(self, days_ago=1, skill="speaking", track=None, status="analyzed", topic=None, grammar_topic=None):
        return Lesson.objects.create(
            learner=self.learner,
            track=track or self.work,
            skill=skill,
            status=status,
            topic=topic,
            grammar_topic=grammar_topic,
            scheduled_for=TODAY - timedelta(days=days_ago),
        )

    def error(self, produced="it depends of the client", subcategory="prepositions", category="grammar", occurrences=1, due=TODAY, **kwargs):
        lesson = kwargs.pop("lesson", None) or self.lesson()
        return ErrorItem.objects.create(
            learner=self.learner, source_lesson=lesson, category=category, subcategory=subcategory,
            learner_produced=produced, correction="fixed", explanation="x", occurrences=occurrences,
            next_review_at=due, **kwargs,
        )


class SkillSelectionTests(PlannerBase):
    def test_only_enabled_skill_is_chosen(self):
        self.assertEqual(planner.choose_skill(self.learner, TODAY, available=["speaking"]), "speaking")

    def test_speaking_first_when_nothing_practiced(self):
        self.assertEqual(planner.choose_skill(self.learner, TODAY, available=planner.SKILLS), "speaking")

    def test_most_neglected_skill_wins_once_speaking_quota_is_met(self):
        for days in (1, 2, 3):
            self.lesson(days_ago=days, skill="speaking")
        self.lesson(days_ago=2, skill="reading")
        self.lesson(days_ago=5, skill="listening")
        self.lesson(days_ago=1, skill="writing")
        self.assertEqual(planner.choose_skill(self.learner, TODAY, available=planner.SKILLS), "listening")

    def test_speaking_is_forced_when_below_three_per_week(self):
        self.lesson(days_ago=2, skill="speaking")
        self.lesson(days_ago=1, skill="reading")
        self.lesson(days_ago=4, skill="listening")
        self.lesson(days_ago=3, skill="writing")
        self.assertEqual(planner.choose_skill(self.learner, TODAY, available=planner.SKILLS), "speaking")

    def test_speaking_is_not_repeated_on_consecutive_days_just_for_quota(self):
        self.lesson(days_ago=1, skill="speaking")
        self.lesson(days_ago=3, skill="reading")
        self.assertEqual(planner.choose_skill(self.learner, TODAY, available=["speaking", "reading"]), "reading")


class TrackSelectionTests(PlannerBase):
    def test_starts_with_work(self):
        self.assertEqual(planner.choose_track(self.learner), self.work)

    def test_alternates(self):
        self.lesson(days_ago=1, track=self.work)
        self.assertEqual(planner.choose_track(self.learner), self.general)
        self.lesson(days_ago=0, track=self.general)
        self.assertEqual(planner.choose_track(self.learner), self.work)

    def test_never_three_in_a_row(self):
        self.lesson(days_ago=2, track=self.work)
        self.lesson(days_ago=1, track=self.work)
        self.assertEqual(planner.choose_track(self.learner), self.general)


class TopicSelectionTests(PlannerBase):
    def test_avoids_the_last_five_topics(self):
        topics = list(Topic.objects.filter(track=self.work))
        for i, topic in enumerate(topics[:5]):
            self.lesson(days_ago=i + 1, topic=topic)
        for _ in range(20):
            chosen = planner.choose_topic(self.learner, self.work)
            self.assertNotIn(chosen, topics[:5])
            self.assertEqual(chosen.track, self.work)

    def test_recent_titles(self):
        for i, topic in enumerate(Topic.objects.filter(track=self.work)[:7]):
            self.lesson(days_ago=i + 1, topic=topic)
        titles = planner.recent_topic_titles(self.learner)
        self.assertEqual(len(titles), 5)


class GrammarTopicSelectionTests(PlannerBase):
    def test_first_lesson_gets_the_first_syllabus_topic(self):
        topic, reason = planner.choose_grammar_topic(self.learner)
        self.assertEqual((topic.order, reason), (1, "syllabus"))

    def test_next_not_started_topic_after_progress(self):
        for order in (1, 2, 3):
            LearnerGrammarTopic.objects.create(learner=self.learner, topic=GrammarTopic.objects.get(order=order), status="introduced")
        topic, reason = planner.choose_grammar_topic(self.learner)
        self.assertEqual((topic.order, reason), (4, "syllabus"))

    def test_error_with_three_occurrences_overrides_the_syllabus(self):
        self.error(produced="I work here since 2021", subcategory="tense", occurrences=3)
        topic, reason = planner.choose_grammar_topic(self.learner)
        self.assertEqual(reason, "error")
        self.assertIn("tense", topic.related_subcategories)
        self.assertEqual(topic.slug, "present-simple-vs-continuous")  # lowest-order topic covering `tense`

    def test_error_skips_topics_already_mastered(self):
        self.error(produced="I work here since 2021", subcategory="tense", occurrences=4)
        first = GrammarTopic.objects.get(slug="present-simple-vs-continuous")
        LearnerGrammarTopic.objects.create(learner=self.learner, topic=first, status="mastered")
        topic, reason = planner.choose_grammar_topic(self.learner)
        self.assertEqual((reason, topic.slug), ("error", "past-simple"))

    def test_two_occurrences_do_not_override(self):
        self.error(subcategory="prepositions", occurrences=2)
        _, reason = planner.choose_grammar_topic(self.learner)
        self.assertEqual(reason, "syllabus")

    def test_every_fifth_lesson_reviews_the_stalest_practicing_topic(self):
        fresh = GrammarTopic.objects.get(order=1)
        stale = GrammarTopic.objects.get(order=2)
        LearnerGrammarTopic.objects.create(learner=self.learner, topic=fresh, status="practicing")
        LearnerGrammarTopic.objects.create(learner=self.learner, topic=stale, status="practicing")
        self.lesson(days_ago=4, grammar_topic=stale)
        self.lesson(days_ago=3, grammar_topic=fresh)
        self.lesson(days_ago=2, grammar_topic=fresh)
        self.lesson(days_ago=1, grammar_topic=fresh)
        # Four finished lessons: the fifth is a review.
        topic, reason = planner.choose_grammar_topic(self.learner)
        self.assertEqual((reason, topic), ("review", stale))


class PhaseMinutesTests(TestCase):
    def test_speaking_twenty_minutes_matches_the_spec_table(self):
        self.assertEqual(planner.phase_minutes("speaking", 20), {"warm_up": 3, "mini_lesson": 4, "practice": 9, "drill": 3, "wrap_up": 1})

    def test_other_durations_still_add_up(self):
        for duration in (10, 15, 25, 30):
            self.assertEqual(sum(planner.phase_minutes("speaking", duration).values()), duration)
            self.assertEqual(sum(planner.phase_minutes("reading", duration).values()), duration)

    def test_text_skills_shrink_the_warm_up(self):
        self.assertEqual(planner.phase_minutes("writing", 20)["warm_up"], 1)


@override_settings(LESSON_SKILLS_ENABLED=["speaking"])
class PrepareNextLessonTests(PlannerBase):
    def prepare(self, data=None, **kwargs):
        chat = SimpleNamespace(data=data or fake_plan(), model="gpt-4o-test", prompt_tokens=10, completion_tokens=20)
        with mock.patch.object(planner.client, "chat_json", return_value=chat) as chat_json:
            lesson, created = planner.prepare_next_lesson(self.learner, on=TODAY, rng=random.Random(1), **kwargs)
        return lesson, created, chat_json

    def test_creates_a_planned_lesson_with_five_ordered_phases(self):
        lesson, created, chat_json = self.prepare()
        self.assertTrue(created)
        self.assertEqual((lesson.status, lesson.skill, lesson.scheduled_for), ("planned", "speaking", TODAY))
        self.assertEqual(lesson.track, self.work)
        self.assertEqual(lesson.grammar_topic.order, 1)
        self.assertEqual([p["key"] for p in lesson.plan["phases"]], planner.PHASES)
        self.assertEqual([p["minutes"] for p in lesson.plan["phases"]], [3, 4, 9, 3, 1])
        self.assertEqual(lesson.plan["title"], "Walking an interviewer through your ERP")
        self.assertEqual(lesson.plan["_meta"]["prompt_tokens"], 10)
        self.assertEqual(lesson.title, "Walking an interviewer through your ERP")

    def test_context_carries_the_file(self):
        error = self.error(occurrences=3, due=TODAY)
        self.error(produced="future", due=TODAY + timedelta(days=1))
        VocabItem.objects.create(learner=self.learner, term="bottleneck", next_review_at=TODAY)
        _, _, chat_json = self.prepare()
        messages, schema = chat_json.call_args.args
        self.assertIn("The one rule that matters", messages[0]["content"])
        context = messages[1]["content"]
        self.assertIn("it depends of the client", context)
        self.assertNotIn('"future"', context)
        self.assertIn("bottleneck", context)
        self.assertIn('"why_today": "error"', context)
        self.assertIs(schema, planner.SPEAKING_PLAN_SCHEMA)

    def test_targeted_errors_are_limited_to_due_ones(self):
        due = self.error(due=TODAY)
        plan = fake_plan(targeted_errors=[
            {"id": due.id, "learner_produced": "x", "correction": "y", "how_to_elicit": "ask"},
            {"id": 999, "learner_produced": "x", "correction": "y", "how_to_elicit": "ask"},
        ])
        lesson, _, _ = self.prepare(plan)
        self.assertEqual(lesson.plan["targeted_error_ids"], [due.id])
        self.assertEqual(lesson.plan["targeted_errors"][0]["learner_produced"], "it depends of the client")

    def test_second_call_returns_the_same_lesson_without_calling_the_model(self):
        first, _, _ = self.prepare()
        second, created, chat_json = self.prepare()
        self.assertFalse(created)
        self.assertEqual(first, second)
        chat_json.assert_not_called()

    def test_force_replaces_an_unstarted_plan(self):
        first, _, _ = self.prepare()
        second, created, _ = self.prepare(force=True, track="general")
        self.assertTrue(created)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.track, self.general)
        self.assertFalse(Lesson.objects.filter(id=first.id).exists())

    def test_force_refuses_to_replace_a_lesson_in_progress(self):
        first, _, _ = self.prepare()
        first.status = "in_progress"
        first.save()
        with self.assertRaises(ValueError):
            self.prepare(force=True)

    def test_manual_picker_overrides(self):
        topic = Topic.objects.filter(track=self.general).first()
        lesson, _, _ = self.prepare(track="general", topic=topic.id, duration=15)
        self.assertEqual((lesson.track, lesson.topic, lesson.plan["duration_min"]), (self.general, topic, 15))
        self.assertEqual(sum(p["minutes"] for p in lesson.plan["phases"]), 15)

    def test_without_seed_data_it_fails_clearly(self):
        Topic.objects.all().delete()
        with self.assertRaises(planner.NothingToPlan):
            self.prepare()
