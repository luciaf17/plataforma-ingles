import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from ai import client as ai_client
from ai import planner, writing
from ai.tests_tutor import PLAN
from learners.models import Learner, Track

from . import views
from .models import ErrorItem, Lesson, LessonReport, Turn, VocabItem

SAMPLE = json.loads((Path(settings.BASE_DIR) / "fixtures" / "analysis_sample.json").read_text(encoding="utf-8"))

TEXT = (
    "Race condition when two webhooks hit the same order within 200ms\n\n"
    "We rolled back the deploy last night after the bottleneck in the queue caused duplicate quotes. "
    "The root cause is a missing lock.\n\n"
    "- Steps: send two webhooks\n- Expected: one quote"
)
TASK = {
    "format": "GitHub issue",
    "headline": "Race condition when two webhooks hit the same order",
    "text": TEXT,
    "glossary": [
        {"term": "bottleneck", "definition_en": "the one point that slows everything else down", "example": "the bottleneck in the queue"},
        {"term": "roll back", "definition_en": "undo a deploy", "example": "We rolled back the deploy"},
    ],
    "questions": [
        {"type": "gist", "question": "What is the issue about?", "options": ["A race condition", "A UI bug", "Docs", "Pricing"], "answer_index": 0, "explanation": "The title says so."},
        {"type": "detail", "question": "What did they do last night?", "options": ["Deployed", "Rolled back", "Slept", "Nothing"], "answer_index": 1, "explanation": "'We rolled back the deploy last night'."},
        {"type": "inference", "question": "What fixes it?", "options": ["A lock", "More servers", "A new UI", "Docs"], "answer_index": 0, "explanation": "The root cause is a missing lock."},
    ],
    "production_prompt": "Comment on the issue proposing a fix.",
    "production_terms": ["bottleneck", "roll back"],
    "production_words_min": 40,
    "production_words_max": 80,
}
READING_PLAN = {**PLAN, "skill": "reading", "reading_task": planner.normalise_reading_task(TASK), "duration_min": 20}
PRODUCTION = "I think the bottleneck is the missing lock, so we need to roll back and add a lock on the order id before we process the webhook again in the worker, and it depends of the queue too."


class NormaliseReadingTaskTests(TestCase):
    def test_ids_options_and_answer_bounds(self):
        task = planner.normalise_reading_task({
            "text": "one two three",
            "questions": [
                {"type": "gist", "question": "q", "options": ["a", "b", "c", "d", "e"], "answer_index": 4, "explanation": ""},
                {"type": "detail", "question": "bad", "options": ["only"], "answer_index": 0, "explanation": ""},
            ],
            "glossary": [{"term": " ", "definition_en": "", "example": ""}, {"term": "x", "definition_en": "y", "example": "z"}],
        })
        self.assertEqual(len(task["questions"]), 1)
        self.assertEqual((task["questions"][0]["id"], len(task["questions"][0]["options"]), task["questions"][0]["answer_index"]), (1, 4, 0))
        self.assertEqual(len(task["glossary"]), 1)
        self.assertEqual(task["word_count"], 3)


@override_settings(LESSON_SKILLS_ENABLED=["speaking", "writing", "reading"])
class ReadingRunnerTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        self.lesson = Lesson.objects.create(learner=self.learner, track=Track.objects.get(slug="work"), skill="reading", plan=READING_PLAN)
        self.url = f"/lessons/{self.lesson.id}/"
        self.submit_url = f"/lessons/{self.lesson.id}/read/"
        self.vocab_url = f"/lessons/{self.lesson.id}/vocab/"

    def fake_result(self):
        data = {**SAMPLE, "corrected_text": PRODUCTION.replace("depends of", "depends on"), "upgraded_text": "Upgraded.", "upgrade_notes_es": ["Nota."]}
        data.pop("scan", None)
        return writing.WritingResult(corrected_text=data["corrected_text"], upgraded_text="Upgraded.", upgrade_notes_es=["Nota."], analysis=writing.validate(data, skill="reading"))

    def test_runner_renders_text_glossary_and_questions(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Race condition when two webhooks", html)
        self.assertIn("We rolled back the deploy", html)
        self.assertIn('data-glossary="bottleneck"', html)
        self.assertIn('name="q1"', html)
        self.assertIn("Comment on the issue proposing a fix.", html)
        self.assertIn(f'"vocab_url": "{self.vocab_url}"', html)
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "in_progress")

    def test_sidebar_entry_prepares_a_reading_lesson(self):
        self.lesson.delete()
        planned = Lesson.objects.create(learner=self.learner, track=Track.objects.get(slug="work"), skill="reading", plan=READING_PLAN, scheduled_for="2000-01-01")
        with mock.patch.object(views.planner, "prepare_next_lesson", return_value=(planned, True)) as prepare:
            response = self.client.get("/reading/")
        self.assertEqual(prepare.call_args.kwargs["skill"], "reading")
        self.assertRedirects(response, f"/lessons/{planned.id}/", fetch_redirect_response=False)

    def test_incomplete_submission_is_rejected_and_answers_kept(self):
        response = self.client.post(self.submit_url, {"q1": "0", "production": PRODUCTION})
        self.assertEqual(response.status_code, 422)
        html = response.content.decode()
        self.assertIn("Answer every question", html)
        self.assertIn('name="q1" value="0" checked', html)
        self.assertIn(PRODUCTION, html)

    def test_submit_grades_corrects_and_files_errors(self):
        with mock.patch.object(views.writing, "correct_text", return_value=self.fake_result()) as correct:
            response = self.client.post(self.submit_url, {"q1": "0", "q2": "0", "q3": "0", "production": PRODUCTION})
        self.assertRedirects(response, f"/lessons/{self.lesson.id}/report/", fetch_redirect_response=False)
        self.assertEqual(correct.call_args.args[0], PRODUCTION)
        self.assertEqual(correct.call_args.kwargs["task"]["must_use_vocabulary"], ["bottleneck", "roll back"])

        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "analyzed")
        self.assertEqual(Turn.objects.get().text, PRODUCTION)
        report = LessonReport.objects.get(lesson=self.lesson)
        self.assertEqual((report.raw_analysis["reading"]["score"], report.raw_analysis["reading"]["total"]), (2, 3))
        self.assertEqual(report.raw_analysis["_meta"]["cefr_signal"]["skill"], "reading")
        errors = ErrorItem.objects.filter(learner=self.learner)
        self.assertEqual(errors.count(), 5)
        self.assertEqual(set(errors.values_list("confidence", flat=True)), {"high"})

        html = self.client.get(f"/lessons/{self.lesson.id}/report/").content.decode()
        self.assertIn("2 of 3 correct", html)
        self.assertIn("· your answer", html)
        self.assertIn("Your answer, corrected", html)
        self.assertIn("<del>of</del><ins>on</ins>", html)

    def test_corrector_failure_keeps_answers(self):
        with mock.patch.object(views.writing, "correct_text", side_effect=ai_client.AIUnavailable("down")):
            response = self.client.post(self.submit_url, {"q1": "0", "q2": "1", "q3": "0", "production": PRODUCTION})
        self.assertEqual(response.status_code, 503)
        self.assertIn("Your answers are kept", response.content.decode())
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "in_progress")

    def test_grade_questions(self):
        score, rows = views.grade_questions(READING_PLAN["reading_task"], {1: 0, 2: 1, 3: 2})
        self.assertEqual(score, 2)
        self.assertEqual([r["ok"] for r in rows], [True, True, False])
        self.assertEqual(rows[2]["chosen"], 2)

    def test_vocab_lookup_from_glossary_creates_a_target_item(self):
        with mock.patch.object(views.vocab, "define") as define:
            response = self.client.post(self.vocab_url, {"word": "Bottleneck,", "sentence": "the bottleneck in the queue"})
        define.assert_not_called()
        data = response.json()
        self.assertEqual((data["term"], data["source"], data["created"], data["status"]), ("bottleneck", "glossary", True, "target"))
        item = VocabItem.objects.get(learner=self.learner, term="bottleneck")
        self.assertEqual((item.definition_en, item.track), ("the one point that slows everything else down", self.lesson.track))

        response = self.client.post(self.vocab_url, {"word": "bottleneck"})
        self.assertFalse(response.json()["created"])
        self.assertEqual(VocabItem.objects.count(), 1)

    def test_vocab_lookup_of_a_glossary_phrase_part_matches_the_phrase(self):
        response = self.client.post(self.vocab_url, {"word": "rolled"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["term"], "roll back")

    def test_vocab_lookup_outside_the_glossary_asks_the_model(self):
        definition = {"term": "lock", "definition_en": "a mechanism that stops two processes touching the same thing", "example": "Take a lock before writing.", "note_es": "'lock' = candado / bloqueo."}
        with mock.patch.object(views.vocab, "define", return_value=definition) as define:
            response = self.client.post(self.vocab_url, {"word": "lock", "sentence": "The root cause is a missing lock."})
        self.assertEqual(define.call_args.kwargs["sentence"], "The root cause is a missing lock.")
        self.assertEqual(response.json()["source"], "model")
        self.assertEqual(VocabItem.objects.get(term="lock").example_sentence, "Take a lock before writing.")

    def test_vocab_lookup_failures(self):
        self.assertEqual(self.client.post(self.vocab_url, {"word": ""}).status_code, 400)
        with mock.patch.object(views.vocab, "define", side_effect=ai_client.AIUnavailable("down")):
            self.assertEqual(self.client.post(self.vocab_url, {"word": "webhook"}).status_code, 503)
        self.assertEqual(VocabItem.objects.count(), 0)


class ReadingLengthRetryTests(TestCase):
    fixtures = ["seed"]

    def test_short_text_triggers_one_regeneration(self):
        learner = Learner.for_user(get_user_model().objects.create_user("lu2"))
        base = {**PLAN, "phases": [{"key": k, "title": k, "tutor_goal": "", "prompts": []} for k in planner.PHASES]}
        short = SimpleNamespace(data={**base, "reading_task": {**TASK, "text": "short text"}}, content="{}", model="m", prompt_tokens=1, completion_tokens=1)
        long_text = " ".join(["word"] * 320)
        long = SimpleNamespace(data={**base, "reading_task": {**TASK, "text": long_text}}, content="{}", model="m", prompt_tokens=1, completion_tokens=1)
        with mock.patch.object(planner.client, "chat_json", side_effect=[short, long]) as chat_json:
            lesson, _ = planner.prepare_next_lesson(learner, skill="reading")
        self.assertEqual(chat_json.call_count, 2)
        self.assertEqual(lesson.plan["reading_task"]["word_count"], 320)
        self.assertIn("at least 300", chat_json.call_args.args[0][-1]["content"])

    def test_glossary_terms_drop_the_to_prefix(self):
        task = planner.normalise_reading_task({"text": "x", "questions": [], "glossary": [{"term": "to look forward to", "definition_en": "d", "example": "e"}]})
        self.assertEqual(task["glossary"][0]["term"], "look forward to")


@override_settings(LESSON_SKILLS_ENABLED=["reading"])
class AnotherArticleTests(TestCase):
    """A boring article can be swapped as long as nothing has been handed in."""

    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        self.work = Track.objects.get(slug="work")
        self.lesson = Lesson.objects.create(
            learner=self.learner, track=self.work, skill="reading", status="in_progress",
            scheduled_for=timezone.localdate(), plan=READING_PLAN,
        )
        self.url = f"/lessons/{self.lesson.id}/another-article/"

    def swap(self):
        chat = SimpleNamespace(
            data={**PLAN, "reading_task": TASK, "mini_lesson_card": None},
            content="{}", model="m", prompt_tokens=1, completion_tokens=1,
        )
        with mock.patch.object(planner.client, "chat_json", return_value=chat):
            return self.client.post(self.url)

    def test_it_replaces_the_lesson_and_takes_her_to_the_new_one(self):
        response = self.swap()
        replacement = Lesson.objects.exclude(id=self.lesson.id).get()
        self.assertRedirects(response, f"/lessons/{replacement.id}/", target_status_code=200)
        self.assertEqual(replacement.skill, "reading")
        self.assertFalse(Lesson.objects.filter(id=self.lesson.id).exists())

    def test_a_finished_lesson_is_not_thrown_away(self):
        self.lesson.status = "analyzed"
        self.lesson.save(update_fields=["status"])
        response = self.client.post(self.url)
        self.assertEqual(Lesson.objects.count(), 1)
        self.assertEqual(response.status_code, 302)

    def test_the_button_is_offered_while_the_lesson_is_open(self):
        html = self.client.get(f"/lessons/{self.lesson.id}/").content.decode()
        self.assertIn("Read something else", html)
        self.assertIn(self.url, html)
