from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from ai import client as ai_client
from ai import planner
from core import views
from learners.models import Learner, Track
from lessons.models import Lesson
from lessons.tests_checkpoint import RAW_PLAN


class OnboardingTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")

    def test_today_redirects_new_learners_to_onboarding(self):
        self.assertRedirects(self.client.get("/"), "/onboarding/", fetch_redirect_response=False)
        self.learner.goal_statement = "interviews"
        self.learner.save()
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_form_renders_with_efset_link_and_levels(self):
        html = self.client.get("/onboarding/").content.decode()
        self.assertIn("efset.org", html)
        self.assertIn('name="cefr_listening"', html)
        self.assertIn('<option value="B2" selected>', html)  # default target
        self.assertIn("Save and start the checkpoint", html)

    def test_save_and_skip(self):
        response = self.client.post("/onboarding/", {
            "cefr_listening": "B2", "cefr_reading": "B2", "placement_notes": "EF SET 58", "first_name": "Lu",
            "target_level": "B2", "goal_statement": "Entrevistas técnicas y dailies.", "next": "skip",
        })
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.learner.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual((self.learner.cefr_listening, self.learner.cefr_reading, self.learner.placement_notes), ("B2", "B2", "EF SET 58"))
        self.assertEqual((self.learner.target_level, self.learner.goal_statement, self.user.first_name), ("B2", "Entrevistas técnicas y dailies.", "Lu"))
        self.assertFalse(self.learner.placement_done)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_save_and_start_the_short_checkpoint(self):
        chat = SimpleNamespace(data=RAW_PLAN, content="{}", model="m", prompt_tokens=1, completion_tokens=1)
        with mock.patch.object(planner.client, "chat_json", return_value=chat) as chat_json:
            response = self.client.post("/onboarding/", {"target_level": "C1", "goal_statement": "Move to a US team.", "next": "checkpoint"})
        lesson = Lesson.objects.get(learner=self.learner)
        self.assertRedirects(response, f"/lessons/{lesson.id}/", fetch_redirect_response=False)
        self.assertEqual((lesson.skill, lesson.plan["checkpoint"]["short"], lesson.plan["checkpoint"]["steps"]), ("checkpoint", True, ["speaking", "writing"]))
        self.assertIn('"short": true', chat_json.call_args.args[0][1]["content"])
        self.learner.refresh_from_db()
        self.assertEqual(self.learner.target_level, "C1")
        # Posting again reuses the open checkpoint instead of creating another.
        with mock.patch.object(planner.client, "chat_json", return_value=chat):
            response = self.client.post("/onboarding/", {"target_level": "C1", "goal_statement": "Move to a US team.", "next": "checkpoint"})
        self.assertRedirects(response, f"/lessons/{lesson.id}/", fetch_redirect_response=False)
        self.assertEqual(Lesson.objects.count(), 1)

    def test_validation(self):
        response = self.client.post("/onboarding/", {"cefr_listening": "Z9", "target_level": "B2", "goal_statement": "", "next": "skip"})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Pick a CEFR level", html)
        self.assertIn("Say why you are learning", html)
        self.learner.refresh_from_db()
        self.assertEqual(self.learner.goal_statement, "")

    def test_planner_failure_is_a_clear_page(self):
        with mock.patch.object(planner.client, "chat_json", side_effect=ai_client.AIUnavailable("down")):
            response = self.client.post("/onboarding/", {"target_level": "B2", "goal_statement": "x", "next": "checkpoint"})
        self.assertEqual(response.status_code, 503)
        self.learner.refresh_from_db()
        self.assertEqual(self.learner.goal_statement, "x")  # the form was saved before the failure

    def test_short_checkpoint_marks_placement_done(self):
        from ai import level_assessor
        from lessons.tests_checkpoint import fake_assessment

        chat = SimpleNamespace(data=RAW_PLAN, content="{}", model="m", prompt_tokens=1, completion_tokens=1)
        with mock.patch.object(planner.client, "chat_json", return_value=chat):
            lesson = planner.prepare_checkpoint(self.learner, short=True)
        from lessons.models import Turn
        Turn.objects.create(lesson=lesson, role="tutor", text="Tell me what you do.", phase="practice", sequence=1)
        Turn.objects.create(lesson=lesson, role="learner", text="I am developer.", phase="practice", sequence=2)
        self.client.post(f"/lessons/{lesson.id}/checkpoint/speaking/")
        self.client.post(f"/lessons/{lesson.id}/checkpoint/writing/", {"text": " ".join(["word"] * 45)})
        assessment = fake_assessment(listening={"estimate": "B2", "evidence": [], "gaps_to_target": [], "reasoning": "", "assessed": False}, reading={"estimate": "B2", "evidence": [], "gaps_to_target": [], "reasoning": "", "assessed": False})
        with mock.patch.object(level_assessor.client, "chat_json", return_value=SimpleNamespace(data=assessment, prompt_tokens=1, completion_tokens=1)):
            response = self.client.post(f"/lessons/{lesson.id}/checkpoint-finish/", HTTP_HX_REQUEST="true")
        self.assertEqual(response["HX-Redirect"], f"/lessons/{lesson.id}/report/")
        self.learner.refresh_from_db()
        self.assertTrue(self.learner.placement_done)
        self.assertEqual((self.learner.cefr_speaking, self.learner.cefr_writing), ("B1", "B1"))
        self.assertEqual(self.learner.cefr_listening, "")  # not assessed in the short version
        html = self.client.get(f"/lessons/{lesson.id}/report/").content.decode()
        self.assertIn("Speaking", html)
        self.assertNotIn(">Listening</h3>", html)
