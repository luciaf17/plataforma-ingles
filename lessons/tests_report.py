import json
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from ai import analyzer
from ai import client as ai_client
from ai.tests_tutor import PLAN
from learners.models import GrammarTopic, Learner, Track

from . import views
from .models import ErrorItem, Lesson, LessonReport, Turn

SAMPLE = json.loads((Path(settings.BASE_DIR) / "fixtures" / "analysis_sample.json").read_text(encoding="utf-8"))


class ReportFlowBase(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        self.lesson = Lesson.objects.create(
            learner=self.learner, track=Track.objects.get(slug="work"), skill="speaking", status="completed",
            started_at=timezone.now() - timezone.timedelta(minutes=20), completed_at=timezone.now(), duration_seconds=1200,
            grammar_topic=GrammarTopic.objects.get(slug="present-perfect-since-for"), plan=PLAN,
        )
        Turn.objects.create(lesson=self.lesson, role="tutor", text="How long have you worked there?", sequence=1, phase="practice")
        Turn.objects.create(lesson=self.lesson, role="learner", text="I work here since 2021, it depends of the client", sequence=2, phase="practice", audio_duration_ms=5000)
        self.analyze_url = f"/lessons/{self.lesson.id}/analyze/"
        self.report_url = f"/lessons/{self.lesson.id}/report/"
        self.analyzing_url = f"/lessons/{self.lesson.id}/analyzing/"

    def run_analysis(self, sample=None):
        result = analyzer.validate(sample or SAMPLE, targeted_ids=self.lesson.plan.get("targeted_error_ids", []), skill="speaking")
        with mock.patch.object(views.analyzer, "analyze", return_value=result) as analyze:
            response = self.client.post(self.analyze_url, HTTP_HX_REQUEST="true")
        return response, analyze


class EndToReportTests(ReportFlowBase):
    def test_end_lesson_lands_on_the_analyzing_page(self):
        self.lesson.status = "in_progress"
        self.lesson.save()
        response = self.client.post(f"/lessons/{self.lesson.id}/end/")
        self.assertRedirects(response, self.analyzing_url)
        page = self.client.get(self.analyzing_url)
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn(f'hx-post="{self.analyze_url}"', html)
        self.assertIn('hx-trigger="load"', html)

    def test_analyze_runs_the_pipeline_and_redirects_to_the_report(self):
        response, analyze = self.run_analysis()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Redirect"], self.report_url)
        analyze.assert_called_once_with(self.lesson)
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "analyzed")
        self.assertEqual(ErrorItem.objects.filter(source_lesson=self.lesson).count(), 5)
        report = LessonReport.objects.get(lesson=self.lesson)
        self.assertEqual(report.new_errors_count, 5)
        self.assertEqual(len(report.raw_analysis["_meta"]["new_error_ids"]), 5)

    def test_analyze_without_htmx_redirects_normally(self):
        result = analyzer.validate(SAMPLE, skill="speaking")
        with mock.patch.object(views.analyzer, "analyze", return_value=result):
            response = self.client.post(self.analyze_url)
        self.assertRedirects(response, self.report_url)

    def test_analyzer_failure_keeps_the_lesson_and_offers_retry(self):
        with mock.patch.object(views.analyzer, "analyze", side_effect=ai_client.AIUnavailable("down")):
            response = self.client.post(self.analyze_url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 503)
        html = response.content.decode()
        self.assertIn("Try again", html)
        self.assertIn("Nothing is lost", html)
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "completed")
        self.assertEqual(ErrorItem.objects.count(), 0)
        self.assertFalse(LessonReport.objects.filter(lesson=self.lesson).exists())

    def test_analyze_twice_is_safe(self):
        self.run_analysis()
        response = self.client.post(self.analyze_url, HTTP_HX_REQUEST="true")
        self.assertEqual(response["HX-Redirect"], self.report_url)
        self.assertEqual(ErrorItem.objects.count(), 5)

    def test_lesson_without_learner_turns_gets_an_empty_report(self):
        self.lesson.turns.all().delete()
        with mock.patch.object(views.analyzer, "analyze") as analyze:
            response = self.client.post(self.analyze_url, HTTP_HX_REQUEST="true")
        analyze.assert_not_called()
        self.assertEqual(response["HX-Redirect"], self.report_url)
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "analyzed")
        self.assertIn("nada para analizar", self.lesson.report.summary_es)

    def test_analyze_refuses_a_lesson_in_progress(self):
        self.lesson.status = "in_progress"
        self.lesson.save()
        self.assertEqual(self.client.post(self.analyze_url).status_code, 409)


class ReportPageTests(ReportFlowBase):
    def test_report_shows_real_errors_from_the_lesson(self):
        self.run_analysis()
        response = self.client.get(self.report_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Your teacher's note", html)
        self.assertIn("Hoy trabajamos en la práctica de entrevistas", html)
        self.assertIn("Clear and confident explanation of the deterministic pricing engine.", html)
        self.assertIn("New errors", html)
        self.assertIn("<s>I work here since 2021</s>", html)
        self.assertIn("I&#x27;ve worked here since 2021", html)
        self.assertIn("En español usás presente con &#x27;desde&#x27;", html)
        self.assertIn("Grammar · tense", html)
        self.assertIn("Vocabulary · false friend", html)
        self.assertIn("Practice these now", html)
        self.assertNotIn("Errors that came back", html)
        # 10 learner words over 5 seconds.
        self.assertIn(">120<", html)

    def test_report_separates_new_recycled_and_avoided(self):
        existing = ErrorItem.objects.create(
            learner=self.learner, source_lesson=self.lesson, category="grammar", subcategory="prepositions",
            learner_produced="it depends of the client", correction="it depends on the client", explanation="depend on", srs_box=1,
        )
        avoided = ErrorItem.objects.create(
            learner=self.learner, source_lesson=self.lesson, category="grammar", subcategory="agreement",
            learner_produced="people is", correction="people are", explanation="plural", srs_box=4,
        )
        self.lesson.plan = {**PLAN, "targeted_error_ids": [existing.id, avoided.id]}
        self.lesson.save()
        sample = {**SAMPLE, "recycled_error_ids_avoided": [avoided.id]}
        self.run_analysis(sample)

        html = self.client.get(self.report_url).content.decode()
        self.assertIn("Errors that came back", html)
        self.assertIn("Avoided today", html)
        self.assertIn(">mastered<", html)
        self.assertIn("2×", html)

    def test_report_redirects_when_not_analyzed(self):
        response = self.client.get(self.report_url)
        self.assertRedirects(response, f"/lessons/{self.lesson.id}/", fetch_redirect_response=False)

    def test_runner_redirects_finished_lessons_to_the_right_page(self):
        self.assertRedirects(self.client.get(f"/lessons/{self.lesson.id}/"), self.analyzing_url, fetch_redirect_response=False)
        self.run_analysis()
        self.assertRedirects(self.client.get(f"/lessons/{self.lesson.id}/"), self.report_url, fetch_redirect_response=False)

    def test_analyzing_page_redirects_when_already_analyzed(self):
        self.run_analysis()
        self.assertRedirects(self.client.get(self.analyzing_url), self.report_url, fetch_redirect_response=False)
