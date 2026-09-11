from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from ai import client as ai_client
from ai import planner
from ai.tests_tutor import PLAN
from learners.models import Learner, Track

from . import views
from .models import ErrorItem, Lesson, LessonReport, Turn, VocabItem

RAW_TASK = {
    "format": "dialogue",
    "headline": "Scope for a release that's slipping",
    "setting": "A product manager and an engineer negotiate scope.",
    "speakers": ["Maya", "Diego"],
    "lines": [
        {"speaker": "Maya", "text": "So, the release is slipping. What can we cut?"},
        {"speaker": "Diego", "text": "The export feature is the bottleneck. We could push it back a sprint."},
        {"speaker": "Maya", "text": "Fine, but the dashboard is a must-have."},
    ],
    "glossary": [{"term": "bottleneck", "definition_en": "the one point that slows everything else down", "example": "The export feature is the bottleneck."}],
    "questions": [
        {"type": "gist", "question": "What are they discussing?", "options": ["Cutting scope", "Hiring", "Salaries", "Lunch"], "answer_index": 0, "explanation": "They negotiate what to cut.", "evidence": "What can we cut?"},
        {"type": "detail", "question": "What does Diego propose?", "options": ["Cancel the release", "Push the export back", "Hire someone", "Work weekends"], "answer_index": 1, "explanation": "He proposes pushing it back.", "evidence": "push it back a sprint"},
        {"type": "inference", "question": "What matters most to Maya?", "options": ["The export", "The dashboard", "The sprint", "Diego"], "answer_index": 1, "explanation": "She calls it a must-have.", "evidence": "the dashboard is a must-have"},
    ],
}
LISTENING_PLAN = {**PLAN, "skill": "listening", "listening_task": planner.normalise_listening_task(RAW_TASK), "targeted_error_ids": [], "targeted_errors": []}


class NormaliseListeningTests(TestCase):
    def test_dialogue_gets_two_voices_and_line_ids(self):
        task = LISTENING_PLAN["listening_task"]
        self.assertEqual([s["voice"] for s in task["speakers"]], ["coral", "onyx"])
        self.assertEqual([(l["id"], l["voice"]) for l in task["lines"]], [(1, "coral"), (2, "onyx"), (3, "coral")])
        self.assertEqual(task["questions"][1]["id"], 2)
        self.assertEqual((task["listens"], task["max_listens"], task["segments"]), (0, 2, []))
        self.assertGreater(task["word_count"], 20)

    def test_monologue_has_one_voice(self):
        task = planner.normalise_listening_task({"format": "monologue", "speakers": ["Ana"], "lines": [{"speaker": "Ana", "text": "Hi."}, {"speaker": "Someone", "text": "Still Ana."}], "questions": [], "glossary": []})
        self.assertEqual(task["speakers"], [{"name": "Ana", "voice": "verse"}])
        self.assertEqual([l["speaker"] for l in task["lines"]], ["Ana", "Ana"])


@override_settings(LESSON_SKILLS_ENABLED=["speaking", "writing", "reading", "listening"], MEDIA_ROOT="media/test")
class ListeningRunnerTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        self.lesson = Lesson.objects.create(learner=self.learner, track=Track.objects.get(slug="work"), skill="listening", plan=LISTENING_PLAN)
        self.url = f"/lessons/{self.lesson.id}/"
        self.audio_url = f"/lessons/{self.lesson.id}/audio/"
        self.submit_url = f"/lessons/{self.lesson.id}/listen/"

    def voice_it(self, failing_text=None):
        """Fake the TTS. Keyed on the line, never on call order: the calls go
        out in a thread pool and finish in whatever order the network decides."""

        def fake(text, **kwargs):
            if text == failing_text:
                raise ai_client.AIUnavailable("tts down")
            return ai_client.Speech(audio=b"mp3", characters=len(text), latency_ms=1)

        with mock.patch.object(ai_client, "_speak", side_effect=fake) as spoken:
            response = self.client.post(self.audio_url, HTTP_HX_REQUEST="true")
        return response, spoken

    def test_without_audio_the_runner_shows_the_preparing_page(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("voicing the dialogue", html)
        self.assertIn(f'hx-post="{self.audio_url}"', html)
        self.assertNotIn("What can we cut?", html)  # the script is never shown before answering

    def test_audio_generation_voices_each_line_with_its_speaker_voice(self):
        response, spoken = self.voice_it()
        self.assertEqual(response["HX-Redirect"], self.url)
        self.assertEqual(spoken.call_count, 3)
        voices = {call.args[0]: call.kwargs["voice"] for call in spoken.call_args_list}
        self.assertEqual(voices, {
            "So, the release is slipping. What can we cut?": "coral",
            "The export feature is the bottleneck. We could push it back a sprint.": "onyx",
            "Fine, but the dashboard is a must-have.": "coral",
        })
        self.lesson.refresh_from_db()
        segments = self.lesson.plan["listening_task"]["segments"]
        # Stored in script order however the calls came back.
        self.assertEqual([s["line_id"] for s in segments], [1, 2, 3])
        self.assertTrue(all(s["url"].endswith(".mp3") for s in segments))
        self.assertEqual(Turn.objects.filter(lesson=self.lesson, role="tutor").count(), 3)

        # Second call does nothing new.
        _, spoken = self.voice_it()
        spoken.assert_not_called()

    def test_audio_failure_keeps_voiced_lines_and_offers_retry(self):
        response, _ = self.voice_it(failing_text="The export feature is the bottleneck. We could push it back a sprint.")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Try again", response.content.decode())
        self.lesson.refresh_from_db()
        # The lines that did come back are kept, including the one after the
        # failure: they were all in flight together, so none of them is lost.
        self.assertEqual([s["line_id"] for s in self.lesson.plan["listening_task"]["segments"]], [1, 3])

    def test_a_retry_only_voices_what_is_missing(self):
        self.voice_it(failing_text="Fine, but the dashboard is a must-have.")
        response, spoken = self.voice_it()
        self.assertEqual(spoken.call_count, 1)
        self.assertEqual(spoken.call_args.args[0], "Fine, but the dashboard is a must-have.")
        self.assertEqual(response["HX-Redirect"], self.url)
        self.lesson.refresh_from_db()
        self.assertEqual([s["line_id"] for s in self.lesson.plan["listening_task"]["segments"]], [1, 2, 3])

    def test_runner_with_audio_hides_the_script_and_shows_the_player(self):
        self.voice_it()
        html = self.client.get(self.url).content.decode()
        self.assertIn('id="play"', html)
        self.assertIn("2 listens left", html)
        self.assertIn("Scope for a release", html)
        self.assertIn('name="q1"', html)
        self.assertNotIn("What can we cut?", html)
        self.assertIn('"max_listens": 2', html)
        self.assertEqual(html.count(f"listening-{self.lesson.id}-"), 3)  # three segment urls in the config

    def test_listens_are_counted_and_capped(self):
        for expected in (1, 2, 2):
            data = self.client.post(f"/lessons/{self.lesson.id}/listened/").json()
            self.assertEqual(data["listens"], expected)
        html = self.client.get(self.url).content.decode()
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.plan["listening_task"]["listens"], 2)

    def test_submit_grades_reveals_transcript_and_writes_no_errors(self):
        self.voice_it()
        self.client.post(f"/lessons/{self.lesson.id}/listened/")
        response = self.client.post(self.submit_url, {"q1": "0", "q2": "3", "q3": "1"})
        self.assertRedirects(response, f"/lessons/{self.lesson.id}/report/", fetch_redirect_response=False)
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "analyzed")
        report = LessonReport.objects.get(lesson=self.lesson)
        self.assertEqual((report.raw_analysis["listening"]["score"], report.raw_analysis["listening"]["total"], report.raw_analysis["listening"]["listens"]), (2, 3, 1))
        self.assertIn("acertaste 2 de 3", report.summary_es)
        self.assertIn("detalle", report.summary_es)
        self.assertEqual(report.focus_next, ["Listening: detail questions"])
        self.assertEqual(ErrorItem.objects.count(), 0)

        html = self.client.get(f"/lessons/{self.lesson.id}/report/").content.decode()
        self.assertIn("2 of 3 correct · 1 listen", html)
        self.assertIn("<b>Maya:</b>", html)
        self.assertIn('<mark class="ok" title="Question 1">What can we cut?</mark>', html)
        self.assertIn('<mark class="miss" title="Question 2">push it back a sprint</mark>', html)
        self.assertIn("· your answer", html)

    def test_incomplete_submission_is_rejected(self):
        self.voice_it()
        response = self.client.post(self.submit_url, {"q1": "0"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Answer every question", response.content.decode())
        self.assertNotIn("What can we cut?", response.content.decode())

    def test_glossary_lookup_works_on_the_revealed_transcript(self):
        response = self.client.post(f"/lessons/{self.lesson.id}/vocab/", {"word": "bottleneck"})
        self.assertEqual(response.json()["source"], "glossary")
        self.assertEqual(VocabItem.objects.get(term="bottleneck").status, "target")

    def test_sidebar_entry_prepares_a_listening_lesson(self):
        self.lesson.delete()
        planned = Lesson.objects.create(learner=self.learner, track=Track.objects.get(slug="work"), skill="listening", plan=LISTENING_PLAN, scheduled_for="2000-01-01")
        with mock.patch.object(views.planner, "prepare_next_lesson", return_value=(planned, True)) as prepare:
            response = self.client.get("/listening/")
        self.assertEqual(prepare.call_args.kwargs["skill"], "listening")
        self.assertRedirects(response, f"/lessons/{planned.id}/", fetch_redirect_response=False)


class ListeningPlanTests(TestCase):
    fixtures = ["seed"]

    def test_short_script_triggers_one_regeneration(self):
        learner = Learner.for_user(get_user_model().objects.create_user("lu3"))
        base = {**PLAN, "phases": [{"key": k, "title": k, "tutor_goal": "", "prompts": []} for k in planner.PHASES]}
        short = SimpleNamespace(data={**base, "listening_task": RAW_TASK}, content="{}", model="m", prompt_tokens=1, completion_tokens=1)
        long_lines = [{"speaker": "Maya", "text": " ".join(["word"] * 170)}, {"speaker": "Diego", "text": " ".join(["word"] * 170)}]
        long = SimpleNamespace(data={**base, "listening_task": {**RAW_TASK, "lines": long_lines}}, content="{}", model="m", prompt_tokens=1, completion_tokens=1)
        with mock.patch.object(planner.client, "chat_json", side_effect=[short, long]) as chat_json:
            lesson, _ = planner.prepare_next_lesson(learner, skill="listening")
        self.assertEqual(chat_json.call_count, 2)
        self.assertEqual(lesson.plan["listening_task"]["word_count"], 340)
        messages, schema = chat_json.call_args.args
        self.assertIs(schema, planner.LISTENING_PLAN_SCHEMA)
