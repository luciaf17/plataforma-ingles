import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from ai import planner, writing
from ai.tests_tutor import PLAN
from learners.models import GrammarTopic, Learner, Track

from . import views
from .diff import change_count, diff_html
from .models import ErrorItem, Lesson, LessonReport, Turn

SAMPLE = json.loads((Path(settings.BASE_DIR) / "fixtures" / "analysis_sample.json").read_text(encoding="utf-8"))

TASK = {
    "format": "Slack reply",
    "prompt": "Reply to your tech lead explaining why the deploy was reverted.",
    "context": "Hey, saw the revert on prod last night. What happened and what's the plan?",
    "target_words_min": 80,
    "target_words_max": 120,
    "must_use_vocabulary": ["to roll back", "root cause", "heads-up"],
    "structure_hint": "What happened, why, what's next.",
}
WRITING_PLAN = {**PLAN, "skill": "writing", "writing_task": TASK, "duration_min": 20}
TEXT = (
    "Hi! Yesterday I roll back the deploy because the client don't trust the numbers of the new report. "
    "The root cause is a migration that it depends of the region. I work here since 2021 and I never saw this. "
    "I will explain you the plan tomorrow in the standup, and give you a heads-up before the next deploy."
)


class DiffTests(SimpleTestCase):
    def test_marks_replacements_insertions_and_deletions(self):
        html = diff_html("I work here since 2021.", "I've worked here since 2021.")
        self.assertIn("I<ins>&#x27;ve</ins>", html)
        self.assertIn("<del>work</del><ins>worked</ins>", html)
        self.assertIn("since 2021.", html)
        self.assertEqual(change_count("it depends of the client", "it depends on the client"), 1)
        self.assertEqual(change_count("same text", "same text"), 0)

    def test_escapes_html_and_keeps_line_breaks(self):
        html = diff_html("a <b> line\nsecond", "a <b> line\nsecond")
        self.assertIn("&lt;b&gt;", html)
        self.assertIn("<br>", html)


class WritingCorrectorTests(TestCase):
    def test_correct_text_forces_high_confidence_and_keeps_texts(self):
        data = {**SAMPLE, "corrected_text": "Corrected.", "upgraded_text": "Upgraded.", "upgrade_notes_es": ["Nota 1", " "]}
        data["errors"] = [{**data["errors"][0], "confidence": "medium"}]
        data.pop("scan", None)
        chat = SimpleNamespace(data=data, prompt_tokens=50, completion_tokens=20)
        with mock.patch.object(writing.client, "chat_json", return_value=chat) as chat_json:
            result = writing.correct_text(TEXT, task=TASK, cefr="B1", targeted_errors=[{"id": 9, "learner_produced": "x", "correction": "y"}])
        self.assertEqual((result.corrected_text, result.upgraded_text, result.upgrade_notes_es), ("Corrected.", "Upgraded.", ["Nota 1"]))
        self.assertEqual(result.analysis.errors[0].confidence, "high")
        self.assertEqual(result.analysis.cefr_signal["skill"], "writing")
        messages, schema = chat_json.call_args.args
        self.assertIn("corrected_text", messages[0]["content"])
        self.assertIn('"id": 9', messages[1]["content"])
        self.assertIn(TEXT[:30], messages[1]["content"])
        self.assertNotIn("scan", schema["properties"])
        self.assertIn("corrected_text", schema["required"])

    def test_schema_matches_analyzer_fields(self):
        self.assertIn("recycled_error_ids_avoided", writing.WRITING_SCHEMA["properties"])
        self.assertFalse(writing.WRITING_SCHEMA["additionalProperties"])


class WritingPlanTests(TestCase):
    fixtures = ["seed"]

    def test_writing_plan_keeps_the_task_and_request(self):
        learner = Learner.for_user(get_user_model().objects.create_user("lu"))
        raw = {**PLAN, "writing_task": TASK}
        raw["phases"] = [{"key": k, "title": k, "tutor_goal": "", "prompts": []} for k in planner.PHASES]
        chat = SimpleNamespace(data=raw, model="m", prompt_tokens=1, completion_tokens=1)
        with mock.patch.object(planner.client, "chat_json", return_value=chat) as chat_json:
            lesson, created = planner.prepare_next_lesson(learner, skill="writing", request="I have an interview on Friday")
        self.assertTrue(created)
        self.assertEqual(lesson.skill, "writing")
        self.assertEqual(lesson.plan["writing_task"]["format"], "Slack reply")
        self.assertEqual(lesson.plan["learner_request"], "I have an interview on Friday")
        self.assertEqual([p["minutes"] for p in lesson.plan["phases"]], [1, 4, 11, 3, 1])
        messages, schema = chat_json.call_args.args
        self.assertIs(schema, planner.WRITING_PLAN_SCHEMA)
        self.assertIn('"learner_request": "I have an interview on Friday"', messages[1]["content"])

    def test_lessons_of_different_skills_coexist_on_the_same_day(self):
        learner = Learner.for_user(get_user_model().objects.create_user("lu"))
        track = Track.objects.get(slug="work")
        speaking = Lesson.objects.create(learner=learner, track=track, skill="speaking", plan=PLAN)
        self.assertEqual(planner.lesson_for(learner, speaking.scheduled_for, skill="writing"), None)
        self.assertEqual(planner.lesson_for(learner, speaking.scheduled_for, skill="speaking"), speaking)
        self.assertEqual(planner.lesson_for(learner, speaking.scheduled_for), speaking)


@override_settings(LESSON_SKILLS_ENABLED=["speaking", "writing"])
class WritingRunnerTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        self.lesson = Lesson.objects.create(
            learner=self.learner, track=Track.objects.get(slug="work"), skill="writing",
            grammar_topic=GrammarTopic.objects.get(slug="present-perfect-since-for"), plan=WRITING_PLAN,
        )
        self.url = f"/lessons/{self.lesson.id}/"
        self.submit_url = f"/lessons/{self.lesson.id}/write/"

    def fake_result(self):
        data = {**SAMPLE, "corrected_text": TEXT.replace("I roll back", "I rolled back"), "upgraded_text": "Hey! Quick heads-up: I rolled back last night's deploy.", "upgrade_notes_es": ["Un nativo abre con 'heads-up'."]}
        data.pop("scan", None)
        return writing.WritingResult(
            corrected_text=data["corrected_text"], upgraded_text=data["upgraded_text"], upgrade_notes_es=data["upgrade_notes_es"],
            analysis=writing.validate(data, skill="writing"),
        )

    def test_runner_renders_the_task_and_starts_the_lesson(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Reply to your tech lead", html)
        self.assertIn("saw the revert on prod", html)
        self.assertIn('data-term="to roll back"', html)
        self.assertIn("80–120 words", html)
        self.assertIn("Hand it in", html)
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "in_progress")

    def test_sidebar_entry_prepares_a_writing_lesson_lazily(self):
        self.lesson.delete()
        planned = Lesson.objects.create(learner=self.learner, track=Track.objects.get(slug="work"), skill="writing", plan=WRITING_PLAN, scheduled_for="2000-01-01")
        with mock.patch.object(views.planner, "prepare_next_lesson", return_value=(planned, True)) as prepare:
            response = self.client.get("/writing/")
        self.assertEqual(prepare.call_args.kwargs["skill"], "writing")
        self.assertRedirects(response, f"/lessons/{planned.id}/", fetch_redirect_response=False)

    def test_short_text_is_rejected_and_kept(self):
        response = self.client.post(self.submit_url, {"text": "too short"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("at least 20 words", response.content.decode())
        self.assertIn("too short", response.content.decode())
        self.assertEqual(Turn.objects.count(), 0)

    def test_submit_corrects_files_errors_with_high_confidence_and_shows_the_report(self):
        with mock.patch.object(views.writing, "correct", return_value=self.fake_result()) as correct:
            response = self.client.post(self.submit_url, {"text": TEXT})
        self.assertRedirects(response, f"/lessons/{self.lesson.id}/report/", fetch_redirect_response=False)
        correct.assert_called_once()
        self.assertEqual(correct.call_args.args[1], TEXT)

        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "analyzed")
        self.assertEqual(Turn.objects.get().text, TEXT)
        errors = ErrorItem.objects.filter(learner=self.learner)
        self.assertEqual(errors.count(), 5)
        self.assertEqual(set(errors.values_list("confidence", flat=True)), {"high"})
        # The module's acceptance check: the writing error is in the file and due.
        self.assertIn("it depends of the client", [e.learner_produced for e in errors])
        self.assertEqual(ErrorItem.objects.due_for(self.learner, on=errors[0].next_review_at).count(), 5)

        report = LessonReport.objects.get(lesson=self.lesson)
        self.assertEqual(report.raw_analysis["writing"]["upgraded_text"], "Hey! Quick heads-up: I rolled back last night's deploy.")

        html = self.client.get(f"/lessons/{self.lesson.id}/report/").content.decode()
        self.assertIn("Your text, corrected", html)
        self.assertIn("<del>roll</del><ins>rolled</ins>", html)
        self.assertIn("How a native would write it", html)
        self.assertIn("Un nativo abre con", html)
        self.assertIn("New errors", html)

    def test_corrector_failure_keeps_the_text_for_retry(self):
        with mock.patch.object(views.writing, "correct", side_effect=views.client.AIUnavailable("down")):
            response = self.client.post(self.submit_url, {"text": TEXT})
        self.assertEqual(response.status_code, 503)
        self.assertIn("Your text is saved", response.content.decode())
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "in_progress")
        self.assertEqual(Turn.objects.count(), 1)
        # A retry with the same text does not duplicate the turn.
        with mock.patch.object(views.writing, "correct", return_value=self.fake_result()):
            self.client.post(self.submit_url, {"text": TEXT})
        self.assertEqual(Turn.objects.count(), 1)

    def test_submit_on_a_speaking_lesson_is_refused(self):
        speaking = Lesson.objects.create(learner=self.learner, track=self.lesson.track, skill="speaking", plan=PLAN)
        self.assertEqual(self.client.post(f"/lessons/{speaking.id}/write/", {"text": TEXT}).status_code, 409)

    def test_analyzed_writing_lesson_goes_to_the_report(self):
        with mock.patch.object(views.writing, "correct", return_value=self.fake_result()):
            self.client.post(self.submit_url, {"text": TEXT})
        self.assertRedirects(self.client.get(self.url), f"/lessons/{self.lesson.id}/report/", fetch_redirect_response=False)
        self.assertRedirects(self.client.post(self.submit_url, {"text": TEXT}), f"/lessons/{self.lesson.id}/report/", fetch_redirect_response=False)
