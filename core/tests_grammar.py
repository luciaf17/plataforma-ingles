from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from ai import analyzer, planner
from core import grammar
from core import views as core_views
from learners.models import GrammarTopic, Learner, LearnerGrammarTopic, Track
from lessons import postprocess
from lessons.models import ErrorItem, Lesson


class GrammarBase(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        self.track = Track.objects.get(slug="work")
        self.lesson = Lesson.objects.create(learner=self.learner, track=self.track, skill="speaking", status="analyzed")
        self.today = timezone.localdate()

    def error(self, produced, subcategory="prepositions", category="grammar", **kwargs):
        fields = {"correction": "fixed", "explanation": "explicación", "next_review_at": self.today}
        fields.update(kwargs)
        return ErrorItem.objects.create(learner=self.learner, source_lesson=self.lesson, category=category, subcategory=subcategory, learner_produced=produced, **fields)


class ProgramTests(GrammarBase):
    def test_program_groups_topics_by_level_with_statuses(self):
        LearnerGrammarTopic.objects.create(learner=self.learner, topic=GrammarTopic.objects.get(order=1), status="mastered", times_targeted=4, times_avoided=3)
        LearnerGrammarTopic.objects.create(learner=self.learner, topic=GrammarTopic.objects.get(order=2), status="practicing", times_targeted=2, times_avoided=1)
        levels = grammar.program(self.learner)
        self.assertEqual([l["level"] for l in levels], ["A2", "B1", "B2"])
        a2 = levels[0]
        self.assertEqual(a2["mastered"], 1)
        self.assertEqual([t["status"] for t in a2["topics"][:3]], ["mastered", "practicing", "not_started"])
        self.assertEqual((a2["topics"][1]["times_targeted"], a2["topics"][1]["times_avoided"]), (2, 1))

    def test_page_renders_program_and_topic_details(self):
        html = self.client.get("/grammar/").content.decode()
        self.assertIn("Your program to B2", html)
        self.assertIn("Present perfect with since / for", html)
        self.assertIn("trabajo acá desde 2021", html)
        self.assertIn("Drill this now", html)
        self.assertIn("0 of 34 mastered", html)
        self.assertIn("Drill what's due", html)


class RecurringErrorTests(GrammarBase):
    def test_errors_are_grouped_by_pattern_with_worst_box_and_due(self):
        self.error("it depends of the client", occurrences=3, srs_box=1)
        self.error("arrive to the office", occurrences=2, srs_box=3, next_review_at=self.today + timedelta(days=3))
        self.error("the client don't trust", subcategory="agreement", occurrences=4, srs_box=4, next_review_at=self.today + timedelta(days=8))
        self.error("I have 5 years", subcategory="tense", status="mastered", srs_box=5)
        cards = grammar.recurring_errors(self.learner)
        by = {c["subcategory"]: c for c in cards}
        self.assertEqual(by["prepositions"]["count"], 5)
        self.assertEqual(by["prepositions"]["example"].learner_produced, "it depends of the client")
        self.assertEqual((by["prepositions"]["box"], by["prepositions"]["state"], by["prepositions"]["due_label"]), (1, "needs_work", "due today"))
        self.assertEqual((by["agreement"]["state"], by["agreement"]["due_label"]), ("improving", "due in 8 days"))
        self.assertEqual(by["tense"]["state"], "mastered")
        self.assertEqual([c["subcategory"] for c in cards][-1], "tense")  # mastered last
        self.assertEqual(by["prepositions"]["topic"].slug, "prepositions-time-place")

    def test_filters(self):
        self.error("depends of", srs_box=0)
        self.error("client don't", subcategory="agreement", srs_box=3)
        self.assertEqual([c["subcategory"] for c in grammar.recurring_errors(self.learner, "needs_work")], ["prepositions"])
        self.assertEqual([c["subcategory"] for c in grammar.recurring_errors(self.learner, "improving")], ["agreement"])
        self.assertEqual(grammar.recurring_errors(self.learner, "mastered"), [])
        html = self.client.get("/grammar/?f=improving").content.decode()
        self.assertIn("Subject–verb agreement", html)
        self.assertNotIn("Prepositions after verbs", html)

    def test_page_shows_cards_like_the_prototype(self):
        self.error("it depends of the client", correction="it depends on the client", occurrences=3)
        html = self.client.get("/grammar/").content.decode()
        self.assertIn("Prepositions after verbs", html)
        self.assertIn("3 errors", html)
        self.assertIn("<s>it depends of the client</s>", html)
        self.assertIn("Box 0 · due today", html)
        self.assertIn('name="subcategory" value="prepositions"', html)


class DrillTests(GrammarBase):
    def drill_chat(self):
        data = {"title": "Drill: depend on", "summary": "Fast round.", "prompts": [f"Question {i}?" for i in range(1, 11)], "wrap_up": "Depend on, never of."}
        return SimpleNamespace(data=data, model="m", prompt_tokens=1, completion_tokens=1)

    def test_drill_this_now_creates_a_five_minute_lesson_on_the_topic(self):
        topic = GrammarTopic.objects.get(slug="prepositions-time-place")
        related = self.error("arrive to the office")
        with mock.patch.object(planner.client, "chat_json", return_value=self.drill_chat()) as chat_json:
            response = self.client.post("/grammar/drill/", {"topic": topic.id})
        lesson = Lesson.objects.exclude(id=self.lesson.id).get()
        self.assertRedirects(response, f"/lessons/{lesson.id}/", fetch_redirect_response=False)
        self.assertEqual((lesson.skill, lesson.grammar_topic, lesson.status, lesson.plan["kind"], lesson.plan["duration_min"]), ("speaking", topic, "planned", "drill", 5))
        self.assertEqual([p["key"] for p in lesson.plan["phases"]], ["drill", "wrap_up"])
        self.assertEqual(sum(p["minutes"] for p in lesson.plan["phases"]), 5)
        self.assertEqual(len(lesson.plan["phases"][0]["prompts"]), 10)
        self.assertEqual(lesson.plan["targeted_error_ids"], [related.id])
        context = chat_json.call_args.args[0][1]["content"]
        self.assertIn("Prepositions of time and place", context)
        self.assertIn("arrive to the office", context)

    def test_drill_from_an_error_card_uses_its_errors_and_matching_topic(self):
        e1 = self.error("depends of")
        e2 = self.error("arrive to")
        with mock.patch.object(planner.client, "chat_json", return_value=self.drill_chat()):
            self.client.post("/grammar/drill/", {"subcategory": "prepositions"})
        lesson = Lesson.objects.exclude(id=self.lesson.id).get()
        self.assertEqual(set(lesson.plan["targeted_error_ids"]), {e1.id, e2.id})
        self.assertEqual(lesson.grammar_topic.slug, "prepositions-time-place")

    def test_drill_whats_due_uses_due_errors_only(self):
        due = self.error("depends of")
        self.error("later", next_review_at=self.today + timedelta(days=2))
        with mock.patch.object(planner.client, "chat_json", return_value=self.drill_chat()):
            self.client.post("/grammar/drill/", {})
        lesson = Lesson.objects.exclude(id=self.lesson.id).get()
        self.assertEqual(lesson.plan["targeted_error_ids"], [due.id])
        self.assertIsNone(lesson.grammar_topic)

    def test_nothing_to_drill_is_a_clear_error(self):
        response = self.client.post("/grammar/drill/", {})
        self.assertEqual(response.status_code, 503)
        self.assertIn("Nothing to drill", response.content.decode())

    def test_drill_runner_opens_with_a_single_drill_phase(self):
        topic = GrammarTopic.objects.get(slug="prepositions-time-place")
        with mock.patch.object(planner.client, "chat_json", return_value=self.drill_chat()):
            lesson = planner.prepare_drill(self.learner, grammar_topic=topic)
        html = self.client.get(f"/lessons/{lesson.id}/").content.decode()
        self.assertIn('data-phase="drill"', html)
        self.assertNotIn('data-phase="warm_up"', html)
        self.assertIn("Drill: depend on", html)


class ErrorForcesMiniLessonTests(GrammarBase):
    """Spec 13, module 16: an ErrorItem with 3 occurrences changes tomorrow's mini-lesson."""

    def test_three_occurrences_force_the_grammar_topic_over_the_syllabus(self):
        def found():
            return analyzer.FoundError("grammar", "prepositions", "it depends of the client", "it depends on the client", "depend on", "high")

        for _ in range(3):
            lesson = Lesson.objects.create(learner=self.learner, track=self.track, skill="speaking", status="completed")
            postprocess.record_errors(lesson, [found()])
        item = ErrorItem.objects.get(subcategory="prepositions")
        self.assertEqual(item.occurrences, 3)

        topic, reason = planner.choose_grammar_topic(self.learner)
        self.assertEqual((reason, topic.slug), ("error", "prepositions-time-place"))

        html = self.client.get("/grammar/").content.decode()
        self.assertIn("3 errors", html)
