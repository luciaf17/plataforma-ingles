from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from ai import client as ai_client
from ai import level_assessor, planner
from ai.tests_tutor import PLAN
from learners.models import Learner, Track
from lessons.tests_listening import RAW_TASK as LISTENING_RAW
from lessons.tests_reading import TASK as READING_RAW
from lessons.tests_writing import TASK as WRITING_TASK

from . import views
from .models import Checkpoint, Lesson, LessonReport, Turn

RAW_PLAN = {
    "title": "Checkpoint: September",
    "summary": "Four short tests.",
    "listening_task": LISTENING_RAW,
    "reading_task": READING_RAW,
    "writing_task": WRITING_TASK,
    "speaking": {"role": "a hiring manager", "opening": "Hi Lu, thanks for coming. Tell me what you do.", "prompts": [f"Question {i}?" for i in range(1, 11)]},
}


def fake_assessment(**overrides):
    def skill(estimate, gaps):
        return {"estimate": estimate, "evidence": ["quote one", "quote two", "quote three"], "gaps_to_target": gaps, "reasoning": "r", "assessed": True}
    data = {
        "listening": skill("B2", []),
        "reading": skill("B2+", []),
        "writing": skill("B1+", ["use present perfect for experience", "connect ideas with 'so that'"]),
        "speaking": skill("B1", ["explain decisions with reasons", "handle follow-ups without stopping", "self-correct"]),
        "overall_estimate": "B1+",
        "report_es": "Estás en B1+ en speaking. Te separan dos cosas de B2.",
    }
    data.update(overrides)
    return data


class CheckpointDueTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.learner = Learner.for_user(get_user_model().objects.create_user("lu"))
        self.track = Track.objects.get(slug="work")
        self.today = timezone.localdate()

    def lesson(self, days_ago, **kwargs):
        return Lesson.objects.create(learner=self.learner, track=self.track, skill="speaking", status="analyzed", scheduled_for=self.today - timedelta(days=days_ago), **kwargs)

    def test_not_due_without_history(self):
        self.assertFalse(planner.checkpoint_due(self.learner, self.today))

    def test_due_28_days_after_the_first_lesson_when_never_taken(self):
        self.lesson(27)
        self.assertFalse(planner.checkpoint_due(self.learner, self.today))
        self.lesson(28)
        self.assertTrue(planner.checkpoint_due(self.learner, self.today))

    def test_due_28_days_after_the_last_checkpoint(self):
        self.lesson(60)
        cp = Checkpoint.objects.create(learner=self.learner, results={})
        Checkpoint.objects.filter(id=cp.id).update(taken_at=timezone.now() - timedelta(days=10))
        self.assertFalse(planner.checkpoint_due(self.learner, self.today))
        Checkpoint.objects.filter(id=cp.id).update(taken_at=timezone.now() - timedelta(days=28))
        self.assertTrue(planner.checkpoint_due(self.learner, self.today))

    def test_not_due_while_one_is_open(self):
        self.lesson(40)
        Lesson.objects.create(learner=self.learner, track=self.track, skill="checkpoint", status="planned", scheduled_for=self.today)
        self.assertFalse(planner.checkpoint_due(self.learner, self.today))

    def test_prepare_next_lesson_prepares_a_checkpoint_when_due(self):
        self.lesson(30)
        chat = SimpleNamespace(data=RAW_PLAN, content="{}", model="m", prompt_tokens=1, completion_tokens=1)
        with mock.patch.object(planner.client, "chat_json", return_value=chat) as chat_json:
            lesson, created = planner.prepare_next_lesson(self.learner, on=self.today)
        self.assertTrue(created)
        self.assertEqual((lesson.skill, lesson.plan["kind"], lesson.plan["duration_min"]), ("checkpoint", "checkpoint", 30))
        self.assertEqual(lesson.plan["checkpoint"]["steps"], ["listening", "reading", "writing", "speaking"])
        self.assertEqual(len(lesson.plan["listening_task"]["lines"]), 3)
        self.assertEqual(lesson.plan["reading_task"]["questions"][0]["id"], 1)
        self.assertEqual(lesson.plan["phases"][0]["key"], "practice")
        self.assertEqual(chat_json.call_args.kwargs["schema_name"], "checkpoint_plan")
        # A second call returns the same open checkpoint.
        with mock.patch.object(planner.client, "chat_json", return_value=chat):
            again, created = planner.prepare_next_lesson(self.learner, on=self.today)
        self.assertFalse(created)
        self.assertEqual(again, lesson)

    def test_short_checkpoint_has_two_steps(self):
        chat = SimpleNamespace(data=RAW_PLAN, content="{}", model="m", prompt_tokens=1, completion_tokens=1)
        with mock.patch.object(planner.client, "chat_json", return_value=chat):
            lesson = planner.prepare_checkpoint(self.learner, short=True)
        self.assertEqual(lesson.plan["checkpoint"]["steps"], ["speaking", "writing"])
        self.assertIsNone(lesson.plan["listening_task"])


@override_settings(MEDIA_ROOT="media/test")
class CheckpointRunnerTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        chat = SimpleNamespace(data=RAW_PLAN, content="{}", model="m", prompt_tokens=1, completion_tokens=1)
        with mock.patch.object(planner.client, "chat_json", return_value=chat):
            self.lesson = planner.prepare_checkpoint(self.learner)
        self.url = f"/lessons/{self.lesson.id}/"

    def step_url(self, step):
        return f"/lessons/{self.lesson.id}/checkpoint/{step}/"

    def voice(self):
        with mock.patch.object(ai_client, "speak", return_value=b"mp3"):
            self.client.post(f"/lessons/{self.lesson.id}/audio/", HTTP_HX_REQUEST="true")

    def do_listening(self, answers=("0", "3", "1")):
        self.voice()
        return self.client.post(self.step_url("listening"), {f"q{i + 1}": a for i, a in enumerate(answers)})

    def do_reading(self, answers=("0", "1", "0")):
        return self.client.post(self.step_url("reading"), {f"q{i + 1}": a for i, a in enumerate(answers)})

    def do_writing(self, text=None):
        text = text or " ".join(["word"] * 50)
        return self.client.post(self.step_url("writing"), {"text": text})

    def do_speaking(self):
        Turn.objects.create(lesson=self.lesson, role="tutor", text="Tell me what you do.", phase="practice", sequence=views.next_sequence(self.lesson))
        Turn.objects.create(lesson=self.lesson, role="learner", text="I am backend developer since 2021.", phase="practice", sequence=views.next_sequence(self.lesson))
        return self.client.post(self.step_url("speaking"))

    def test_runner_walks_the_four_steps_in_order(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("voicing the", html)  # listening audio first
        self.voice()
        html = self.client.get(self.url).content.decode()
        self.assertIn('id="play"', html)
        self.assertNotIn("What can we cut?", html)
        self.assertIn('class="step now"', html)

        self.assertRedirects(self.do_listening(), self.url, fetch_redirect_response=False)
        html = self.client.get(self.url).content.decode()
        self.assertIn("Race condition when two webhooks", html)
        self.assertIn('class="step done"', html)

        self.assertRedirects(self.do_reading(), self.url, fetch_redirect_response=False)
        html = self.client.get(self.url).content.decode()
        self.assertIn("Reply to your tech lead", html)

        self.assertRedirects(self.do_writing(), self.url, fetch_redirect_response=False)
        html = self.client.get(self.url).content.decode()
        self.assertIn("Finish interview", html)
        self.assertIn("the interviewer will not correct you", html)
        self.assertIn('"current_phase": "practice"', html)

        self.assertRedirects(self.do_speaking(), self.url, fetch_redirect_response=False)
        html = self.client.get(self.url).content.decode()
        self.assertIn("All four parts are in", html)
        self.assertIn(f'hx-post="/lessons/{self.lesson.id}/checkpoint-finish/"', html)

        progress = Lesson.objects.get(id=self.lesson.id).plan["checkpoint"]["progress"]
        self.assertEqual((progress["listening"]["score"], progress["reading"]["score"]), (2, 3))
        self.assertEqual(progress["writing"]["word_count"], 50)
        self.assertEqual(len(progress["speaking"]["turns"]), 2)

    def test_steps_out_of_order_are_redirected(self):
        self.assertRedirects(self.do_reading(), self.url, fetch_redirect_response=False)
        self.assertEqual(Lesson.objects.get(id=self.lesson.id).plan["checkpoint"]["progress"], {})

    def test_incomplete_answers_and_short_text_are_rejected(self):
        self.voice()
        response = self.client.post(self.step_url("listening"), {"q1": "0"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Answer every question", response.content.decode())
        self.do_listening()
        self.do_reading()
        response = self.do_writing("too short")
        self.assertIn("at least 40 words", response.content.decode())

    def test_finish_assesses_stores_the_checkpoint_and_sets_levels(self):
        self.do_listening()
        self.do_reading()
        self.do_writing()
        self.do_speaking()
        chat = SimpleNamespace(data=fake_assessment(), prompt_tokens=300, completion_tokens=200)
        with mock.patch.object(level_assessor.client, "chat_json", return_value=chat) as chat_json:
            response = self.client.post(f"/lessons/{self.lesson.id}/checkpoint-finish/", HTTP_HX_REQUEST="true")
        self.assertEqual(response["HX-Redirect"], f"/lessons/{self.lesson.id}/report/")

        context = chat_json.call_args.args[0][1]["content"]
        self.assertIn("I am backend developer since 2021.", context)
        self.assertIn("Maya: So, the release is slipping", context)
        self.assertIn('"score": 2', context)

        checkpoint = Checkpoint.objects.get(learner=self.learner)
        self.assertEqual(checkpoint.lesson, self.lesson)
        self.assertEqual(checkpoint.results["speaking"]["estimate"], "B1")
        self.assertEqual(len(checkpoint.results["speaking"]["evidence"]), 3)
        self.assertEqual(checkpoint.results["writing"]["gaps_to_target"][0], "use present perfect for experience")
        self.assertEqual(checkpoint.results["overall"], "B1+")
        self.assertIn("Estás en B1+", checkpoint.report_es)

        self.learner.refresh_from_db()
        self.assertEqual((self.learner.cefr_speaking, self.learner.cefr_writing, self.learner.cefr_listening, self.learner.cefr_reading), ("B1", "B1", "B2", "B2"))
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "analyzed")
        self.assertEqual(LessonReport.objects.get(lesson=self.lesson).focus_next[0], "use present perfect for experience")

        html = self.client.get(f"/lessons/{self.lesson.id}/report/").content.decode()
        self.assertIn("Your examiner", html)
        self.assertIn("Te separan dos cosas de B2", html)
        self.assertIn("quote one", html)
        self.assertIn("explain decisions with reasons", html)
        self.assertIn("first measurement", html)
        self.assertIn("2 of 3 correct", html)

    def test_finish_before_the_end_redirects_and_failure_offers_retry(self):
        self.assertRedirects(self.client.post(f"/lessons/{self.lesson.id}/checkpoint-finish/"), self.url, fetch_redirect_response=False)
        self.do_listening(); self.do_reading(); self.do_writing(); self.do_speaking()
        with mock.patch.object(level_assessor.client, "chat_json", side_effect=ai_client.AIUnavailable("down")):
            response = self.client.post(f"/lessons/{self.lesson.id}/checkpoint-finish/", HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Try again", response.content.decode())
        self.assertEqual(Checkpoint.objects.count(), 0)
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "in_progress")


class AssessorValidateTests(TestCase):
    def test_unassessed_skills_are_blanked_and_evidence_capped(self):
        data = fake_assessment(listening={"estimate": "B2", "evidence": ["a", "b", "c", "d"], "gaps_to_target": [], "reasoning": "x", "assessed": True})
        results = level_assessor.validate(data, ["speaking", "writing", "listening"])
        self.assertEqual(results["reading"]["assessed"], False)
        self.assertEqual(results["reading"]["estimate"], "")
        self.assertEqual(len(results["listening"]["evidence"]), 3)
        self.assertEqual(level_assessor.base_level("B1+"), "B1")


class OverallCapTests(TestCase):
    def test_overall_is_capped_by_production(self):
        results = level_assessor.validate(fake_assessment(), level_assessor.SKILLS)
        self.assertEqual(level_assessor.cap_overall("B2", results), "B1+")
        self.assertEqual(level_assessor.cap_overall("B1", results), "B1")
        results["speaking"]["assessed"] = False
        results["writing"]["assessed"] = False
        self.assertEqual(level_assessor.cap_overall("B2", results), "B2")
