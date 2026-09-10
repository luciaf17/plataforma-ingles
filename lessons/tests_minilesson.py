from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from ai import client as ai_client
from ai import minilesson, planner
from lessons.tests_writing import TEXT, WRITING_PLAN
from learners.models import GrammarTopic, Learner, Track

from . import views
from .models import ErrorItem, Lesson, LessonReport

CARD = planner.normalise_mini_lesson_card({
    "explanation_en": "When something started in the past and is still true, use the present perfect.",
    "examples": [{"wrong": "I work here since 2021", "right": "I've worked here since 2021", "note_en": "since + present perfect"}],
    "exercises": [
        {"sentence": "I ___ at this company since 2021.", "cue": "(work)", "answer": "have worked", "accepted": ["'ve worked", "have been working", "'ve been working"]},
        {"sentence": "We ___ Postgres for three years.", "cue": "(use)", "answer": "have used", "accepted": ["'ve used", "have been using"]},
        {"sentence": "She ___ in Córdoba since March.", "cue": "(live)", "answer": "has lived", "accepted": ["'s lived", "has been living"]},
    ],
})
PLAN_WITH_CARD = {**WRITING_PLAN, "mini_lesson_card": CARD}


class NormaliseCardTests(SimpleTestCase):
    def test_exercises_need_a_gap_and_get_ids(self):
        card = planner.normalise_mini_lesson_card({"explanation_en": " x ", "examples": [], "exercises": [
            {"sentence": "No gap here.", "cue": "", "answer": "a", "accepted": []},
            {"sentence": "I ___ it.", "cue": "(do)", "answer": "did", "accepted": [" ", "have done"]},
        ]})
        self.assertEqual(len(card["exercises"]), 1)
        self.assertEqual((card["exercises"][0]["id"], card["exercises"][0]["accepted"]), (2, ["have done"]))
        self.assertEqual(card["explanation_en"], "x")
        self.assertIsNone(card["result"])


class CheckerTests(SimpleTestCase):
    def test_exact_and_accepted_matches_skip_the_model(self):
        with mock.patch.object(minilesson.client, "chat_json") as chat_json:
            results, errors = minilesson.check(CARD["exercises"], {1: "HAVE worked", 2: "have been using", 3: "has lived."})
        chat_json.assert_not_called()
        self.assertTrue(all(r["correct"] for r in results))
        self.assertEqual(errors, [])

    def test_mismatches_go_to_the_model_which_may_accept_them(self):
        data = {
            "results": [
                {"id": 1, "correct": True, "correction": "have worked", "explanation_es": ""},
                {"id": 3, "correct": False, "correction": "has lived", "explanation_es": "Desde + presente en español; present perfect en inglés."},
            ],
            "errors": [{
                "category": "grammar", "subcategory": "tense", "learner_produced": "She lives in Córdoba since March.",
                "correction": "She has lived in Córdoba since March.", "explanation_es": "x", "confidence": "medium", "is_recycled": False, "recycled_error_id": None,
            }],
        }
        with mock.patch.object(minilesson.client, "chat_json", return_value=SimpleNamespace(data=data)) as chat_json:
            results, errors = minilesson.check(CARD["exercises"], {1: "have worked here", 2: "have used", 3: "lives"}, grammar_topic={"title": "t"})
        context = chat_json.call_args.args[0][1]["content"]
        self.assertIn('"matched": true', context)
        self.assertEqual([r["correct"] for r in results], [True, True, False])
        self.assertEqual(results[2]["explanation_es"], "Desde + presente en español; present perfect en inglés.")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].confidence, "high")


@override_settings(LESSON_SKILLS_ENABLED=["speaking", "writing", "reading", "listening"])
class MiniLessonViewTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        self.lesson = Lesson.objects.create(
            learner=self.learner, track=Track.objects.get(slug="work"), skill="writing",
            grammar_topic=GrammarTopic.objects.get(slug="present-perfect-since-for"), plan=PLAN_WITH_CARD,
        )
        self.url = f"/lessons/{self.lesson.id}/mini-lesson/"

    def test_runner_shows_the_card_with_three_gaps(self):
        html = self.client.get(f"/lessons/{self.lesson.id}/").content.decode()
        self.assertIn("Mini-lesson · 4 min", html)
        self.assertIn("use the present perfect", html)
        self.assertIn("I ___ at this company since 2021.", html)
        self.assertIn('name="ex3"', html)
        self.assertIn(f'hx-post="{self.url}"', html)

    def test_all_correct_records_nothing_and_locks_the_card(self):
        with mock.patch.object(minilesson.client, "chat_json") as chat_json:
            response = self.client.post(self.url, {"ex1": "have worked", "ex2": "'ve used", "ex3": "has been living"}, HTTP_HX_REQUEST="true")
        chat_json.assert_not_called()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("3 of 3 right", html)
        self.assertNotIn("Check my sentences", html)
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.plan["mini_lesson_card"]["result"]["score"], 3)
        self.assertEqual(ErrorItem.objects.count(), 0)

    def test_wrong_answers_are_filed_with_high_confidence_and_shown_on_the_report(self):
        data = {
            "results": [{"id": 3, "correct": False, "correction": "has lived", "explanation_es": "Present perfect con since."}],
            "errors": [{
                "category": "grammar", "subcategory": "tense", "learner_produced": "She lives in Córdoba since March.",
                "correction": "She has lived in Córdoba since March.", "explanation_es": "Present perfect con since.", "confidence": "medium", "is_recycled": False, "recycled_error_id": None,
            }],
        }
        with mock.patch.object(minilesson.client, "chat_json", return_value=SimpleNamespace(data=data)):
            response = self.client.post(self.url, {"ex1": "have worked", "ex2": "have used", "ex3": "lives"}, HTTP_HX_REQUEST="true")
        html = response.content.decode()
        self.assertIn("2 of 3 right", html)
        self.assertIn("<em>has lived</em>", html)
        self.assertIn("Present perfect con since.", html)
        error = ErrorItem.objects.get()
        self.assertEqual((error.confidence, error.source_lesson), ("high", self.lesson))
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.plan["mini_lesson_card"]["result"]["new_error_ids"], [error.id])

        # Handing in the writing task merges the mini-lesson error into the report.
        from lessons.tests_writing import WritingRunnerTests
        fake = WritingRunnerTests.fake_result(self)
        with mock.patch.object(views.writing, "correct", return_value=fake):
            self.client.post(f"/lessons/{self.lesson.id}/write/", {"text": TEXT})
        report = LessonReport.objects.get(lesson=self.lesson)
        self.assertIn(error.id, report.raw_analysis["_meta"]["new_error_ids"])
        self.assertEqual(report.new_errors_count, 6)
        report_html = self.client.get(f"/lessons/{self.lesson.id}/report/").content.decode()
        self.assertIn("Mini-lesson", report_html)
        self.assertIn("2 of 3 right", report_html)

    def test_missing_answers_are_rejected(self):
        response = self.client.post(self.url, {"ex1": "have worked", "ex2": "", "ex3": "has lived"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 422)
        self.assertIn("Fill in all three", response.content.decode())
        self.assertIn('value="have worked"', response.content.decode())

    def test_second_check_is_ignored(self):
        with mock.patch.object(minilesson.client, "chat_json"):
            self.client.post(self.url, {"ex1": "have worked", "ex2": "have used", "ex3": "has lived"}, HTTP_HX_REQUEST="true")
            response = self.client.post(self.url, {"ex1": "x", "ex2": "y", "ex3": "z"}, HTTP_HX_REQUEST="true")
        self.assertIn("3 of 3 right", response.content.decode())

    def test_model_failure_keeps_answers(self):
        with mock.patch.object(minilesson.client, "chat_json", side_effect=ai_client.AIUnavailable("down")):
            response = self.client.post(self.url, {"ex1": "worked", "ex2": "have used", "ex3": "has lived"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Could not check", response.content.decode())
        self.assertIn('value="worked"', response.content.decode())

    def test_speaking_lessons_have_no_card(self):
        speaking = Lesson.objects.create(learner=self.learner, track=self.lesson.track, skill="speaking", plan={**WRITING_PLAN, "mini_lesson_card": None})
        self.assertEqual(self.client.post(f"/lessons/{speaking.id}/mini-lesson/", {}, HTTP_HX_REQUEST="true").status_code, 409)
