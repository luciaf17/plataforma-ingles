from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from ai import client as ai_client
from ai.tests_tutor import PLAN
from core import dashboard, views
from learners.models import GrammarTopic, Learner, LearnerGrammarTopic, Track
from lessons.models import ErrorItem, Lesson, LessonReport


class TodayBase(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw", first_name="Lu")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        self.today = timezone.localdate()
        self.work = Track.objects.get(slug="work")

    def lesson(self, days_ago=0, status="analyzed", plan=None, **kwargs):
        lesson = Lesson.objects.create(
            learner=self.learner, track=self.work, skill="speaking", status=status,
            scheduled_for=self.today - timedelta(days=days_ago), plan=plan if plan is not None else PLAN,
            duration_seconds=kwargs.pop("duration_seconds", 1200), **kwargs,
        )
        return lesson


class DashboardNumbersTests(TodayBase):
    def test_streak_counts_consecutive_days_ending_today_or_yesterday(self):
        self.assertEqual(dashboard.streak(self.learner, self.today), 0)
        self.lesson(days_ago=1)
        self.lesson(days_ago=2)
        self.lesson(days_ago=4)
        self.assertEqual(dashboard.streak(self.learner, self.today), 2)
        self.lesson(days_ago=0)
        self.assertEqual(dashboard.streak(self.learner, self.today), 3)

    def test_planned_lessons_do_not_count(self):
        self.lesson(days_ago=0, status="planned")
        self.assertEqual(dashboard.streak(self.learner, self.today), 0)
        self.assertEqual(dashboard.day_number(self.learner, self.today), 0)

    def test_day_number_counts_from_the_first_finished_lesson(self):
        self.lesson(days_ago=11)
        self.assertEqual(dashboard.day_number(self.learner, self.today), 12)

    def test_last_days_cells(self):
        self.lesson(days_ago=1)
        cells = dashboard.last_days(self.learner, self.today)
        self.assertEqual(len(cells), 14)
        self.assertEqual([c["state"] for c in cells[-2:]], ["done", "today"])
        self.assertEqual(cells[0]["state"], "empty")

    def test_yesterday_line_reads_the_report(self):
        self.assertIn("file is empty", dashboard.yesterday_line(self.learner, self.today))
        lesson = self.lesson(days_ago=1, plan={**PLAN, "targeted_error_ids": [1, 2, 3]})
        LessonReport.objects.create(lesson=lesson, summary_es="x", errors_avoided_count=2, new_errors_count=1)
        self.assertEqual(dashboard.yesterday_line(self.learner, self.today), "Yesterday you avoided 2 of your 3 targeted errors.")

    def test_skill_levels_and_program(self):
        self.learner.cefr_speaking = "B1"
        self.learner.cefr_reading = "B2"
        self.learner.save()
        levels = {row["key"]: row for row in dashboard.skill_levels(self.learner)}
        self.assertEqual((levels["speaking"]["level"], levels["speaking"]["pct"], levels["speaking"]["is_target"]), ("B1", 45, False))
        self.assertEqual((levels["reading"]["level"], levels["reading"]["is_target"]), ("B2", True))
        self.assertEqual(levels["writing"]["level"], "—")

        LearnerGrammarTopic.objects.create(learner=self.learner, topic=GrammarTopic.objects.get(order=1), status="mastered")
        program = dashboard.program_progress(self.learner)
        self.assertEqual((program["mastered"], program["total"], program["pct"]), (1, 34, 3))

    def test_recent_lessons_rows(self):
        lesson = self.lesson(days_ago=1, plan={**PLAN, "targeted_error_ids": [1, 2, 3]})
        LessonReport.objects.create(lesson=lesson, summary_es="x", errors_avoided_count=2, new_errors_count=1)
        self.lesson(days_ago=0, status="completed")
        rows = dashboard.recent_lessons(self.learner, self.today)
        self.assertEqual([r["when"] for r in rows], ["Today", "Yesterday"])
        self.assertEqual(rows[0]["result"], "not analyzed")
        self.assertEqual(rows[1]["result"], "2 of 3 avoided · 1 new")
        self.assertEqual(rows[1]["minutes"], 20)


@override_settings(LESSON_SKILLS_ENABLED=["speaking"])
class TodayPageTests(TodayBase):
    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_without_a_lesson_it_shows_the_preparing_card(self):
        html = self.client.get("/").content.decode()
        self.assertIn("Preparing your class", html)
        self.assertIn('hx-post="/today/prepare/"', html)
        self.assertIn("No lessons yet", html)
        self.assertIn("Day ", html) if dashboard.day_number(self.learner, self.today) else self.assertNotIn("Day 0", html)

    def test_with_a_planned_lesson_it_shows_start_and_targets(self):
        error = ErrorItem.objects.create(
            learner=self.learner, source_lesson=self.lesson(days_ago=3), category="grammar", subcategory="tense",
            learner_produced="I work here since 2021", correction="I've worked here since 2021", explanation="x", occurrences=4,
        )
        lesson = self.lesson(status="planned", plan={
            **PLAN, "targeted_error_ids": [error.id],
            "targeted_errors": [{"id": error.id, "learner_produced": error.learner_produced, "correction": error.correction, "how_to_elicit": "ask"}],
        })
        html = self.client.get("/").content.decode()
        self.assertIn("Walking an interviewer through your ERP", html)
        self.assertIn(f'href="/lessons/{lesson.id}/"', html)
        self.assertIn("Start lesson", html)
        self.assertIn("targets 1 error from your file", html)
        self.assertIn("4 times", html)
        self.assertIn("Change skill or topic", html)
        self.assertIn('name="skill"', html)
        self.assertNotIn('<option value="listening"', html)
        self.assertIn("Speaking —", html)  # sidebar footer, no level yet

    def test_in_progress_and_analyzed_states(self):
        lesson = self.lesson(status="in_progress")
        html = self.client.get("/").content.decode()
        self.assertIn("Resume lesson", html)
        self.assertNotIn("Change skill or topic", html)

        lesson.status = "analyzed"
        lesson.save()
        LessonReport.objects.create(lesson=lesson, summary_es="x")
        html = self.client.get("/").content.decode()
        self.assertIn("Read the report", html)
        self.assertIn("Another lesson today", html)
        self.assertIn(f'href="/lessons/{lesson.id}/report/"', html)

    def test_sidebar_shows_due_count_and_level(self):
        self.learner.cefr_speaking = "B1"
        self.learner.goal_statement = "technical interviews"
        self.learner.save()
        ErrorItem.objects.create(learner=self.learner, source_lesson=self.lesson(days_ago=2), category="grammar", subcategory="tense",
                                 learner_produced="x", correction="y", explanation="z", next_review_at=self.today)
        html = self.client.get("/").content.decode()
        self.assertIn('<span class="count">1 due</span>', html)
        self.assertIn("Speaking B1 · Goal: technical interviews", html)


@override_settings(LESSON_SKILLS_ENABLED=["speaking"])
class PrepareTodayTests(TodayBase):
    def prepare(self, data=None, htmx=True):
        lesson = self.lesson(status="planned")
        with mock.patch.object(views.planner, "prepare_next_lesson", return_value=(lesson, True)) as prepare:
            response = self.client.post("/today/prepare/", data or {}, **({"HTTP_HX_REQUEST": "true"} if htmx else {}))
        return response, prepare

    def test_htmx_returns_the_lesson_card(self):
        response, prepare = self.prepare()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Start lesson", html)
        self.assertNotIn("<html", html)
        self.assertEqual(prepare.call_args.kwargs["force"], False)
        self.assertEqual(prepare.call_args.kwargs["skill"], None)

    def test_picker_values_are_passed_with_force(self):
        response, prepare = self.prepare({"force": "1", "skill": "speaking", "track": "general", "duration": "15", "topic": "3"})
        kwargs = prepare.call_args.kwargs
        self.assertEqual((kwargs["force"], kwargs["skill"], kwargs["track"], kwargs["duration"], kwargs["topic"]), (True, "speaking", "general", 15, 3))

    def test_disabled_skill_and_odd_duration_fall_back(self):
        _, prepare = self.prepare({"skill": "listening", "duration": "42"})
        self.assertEqual((prepare.call_args.kwargs["skill"], prepare.call_args.kwargs["duration"]), (None, 20))

    def test_surprise_ignores_the_picker(self):
        _, prepare = self.prepare({"force": "1", "surprise": "1", "skill": "speaking", "track": "general", "duration": "10"})
        kwargs = prepare.call_args.kwargs
        self.assertEqual((kwargs["force"], kwargs["skill"], kwargs["track"], kwargs["duration"]), (True, None, None, 20))

    def test_plain_post_redirects_to_today(self):
        response, _ = self.prepare(htmx=False)
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_failure_renders_retry_with_503(self):
        with mock.patch.object(views.planner, "prepare_next_lesson", side_effect=ai_client.AIUnavailable("down")):
            response = self.client.post("/today/prepare/", {}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 503)
        html = response.content.decode()
        self.assertIn("could not be prepared", html)
        self.assertIn("Try again", html)

    def test_replacing_a_lesson_in_progress_is_refused_gracefully(self):
        self.lesson(status="in_progress")
        with mock.patch.object(views.planner, "prepare_next_lesson", side_effect=ValueError("in progress")):
            response = self.client.post("/today/prepare/", {"force": "1"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 503)
        self.assertIn("in progress", response.content.decode())
