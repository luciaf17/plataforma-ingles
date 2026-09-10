import json
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from ai import analyzer
from core import dashboard
from learners.models import Learner, Track

from . import leveling, postprocess
from .models import Checkpoint, Lesson, LessonReport

SAMPLE = json.loads((Path(settings.BASE_DIR) / "fixtures" / "analysis_sample.json").read_text(encoding="utf-8"))


class LevelingBase(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.learner.goal_statement = "interviews"
        self.learner.save()
        self.client.login(username="lu", password="pw")
        self.track = Track.objects.get(slug="work")
        self.today = timezone.localdate()

    def analyzed(self, estimate, *, skill="speaking", days_ago=0):
        """One analyzed lesson whose report carries a CEFR signal."""
        lesson = Lesson.objects.create(
            learner=self.learner, track=self.track, skill=skill, status="analyzed",
            scheduled_for=self.today - timedelta(days=days_ago),
            completed_at=timezone.now() - timedelta(days=days_ago),
        )
        LessonReport.objects.create(
            lesson=lesson, summary_es="x",
            raw_analysis={"_meta": {"cefr_signal": {"skill": skill, "estimate": estimate}}},
        )
        return lesson

    def run_postprocess(self, estimate, *, skill="speaking"):
        """A real postprocess run, which is what applies the signal."""
        lesson = Lesson.objects.create(learner=self.learner, track=self.track, skill=skill, status="completed", completed_at=timezone.now())
        data = {**SAMPLE, "errors": [], "cefr_signal": {"skill": skill, "estimate": estimate, "reasoning": "r"}}
        return postprocess.apply_analysis(lesson, analyzer.validate(data, skill=skill))


class SignalRuleTests(LevelingBase):
    def test_one_or_two_lessons_do_not_move_the_level(self):
        self.learner.cefr_speaking = "B1"
        self.learner.save()
        self.analyzed("B2", days_ago=2)
        changed, detail = leveling.apply_signal(self.learner, "speaking")
        self.assertFalse(changed)
        self.assertEqual((detail["band"], detail["streak"]), ("B2", 1))

        self.analyzed("B2", days_ago=1)
        changed, detail = leveling.apply_signal(self.learner, "speaking")
        self.assertFalse(changed)
        self.assertEqual(detail["streak"], 2)
        self.learner.refresh_from_db()
        self.assertEqual(self.learner.cefr_speaking, "B1")

    def test_three_consecutive_lessons_move_it(self):
        self.learner.cefr_speaking = "B1"
        self.learner.save()
        for days in (3, 2, 1):
            self.analyzed("B2", days_ago=days)
        changed, detail = leveling.apply_signal(self.learner, "speaking")
        self.assertTrue(changed)
        self.assertEqual((detail["previous"], detail["new"]), ("B1", "B2"))
        self.learner.refresh_from_db()
        self.assertEqual(self.learner.cefr_speaking, "B2")

    def test_a_disagreeing_lesson_breaks_the_streak(self):
        self.learner.cefr_speaking = "B1"
        self.learner.save()
        self.analyzed("B2", days_ago=4)
        self.analyzed("B1", days_ago=3)
        self.analyzed("B2", days_ago=2)
        self.analyzed("B2", days_ago=1)
        changed, detail = leveling.apply_signal(self.learner, "speaking")
        self.assertFalse(changed)
        self.assertEqual(detail["streak"], 2)

    def test_plus_bands_count_as_their_base_level(self):
        self.learner.cefr_speaking = "B1"
        self.learner.save()
        for days, band in ((3, "B1+"), (2, "B1"), (1, "B1+")):
            self.analyzed(band, days_ago=days)
        changed, detail = leveling.apply_signal(self.learner, "speaking")
        self.assertFalse(changed)  # three agree but on the level already stored
        self.assertEqual(detail["streak"], 3)
        self.learner.refresh_from_db()
        self.assertEqual(self.learner.cefr_speaking, "B1")

    def test_an_unset_level_is_filled_by_three_signals(self):
        for days in (3, 2, 1):
            self.analyzed("B1+", days_ago=days)
        changed, _ = leveling.apply_signal(self.learner, "speaking")
        self.assertTrue(changed)
        self.learner.refresh_from_db()
        self.assertEqual(self.learner.cefr_speaking, "B1")

    def test_skills_are_independent(self):
        for days in (3, 2, 1):
            self.analyzed("B2", days_ago=days, skill="writing")
        self.analyzed("C1", days_ago=1, skill="speaking")
        leveling.apply_signal(self.learner, "writing")
        leveling.apply_signal(self.learner, "speaking")
        self.learner.refresh_from_db()
        self.assertEqual((self.learner.cefr_writing, self.learner.cefr_speaking), ("B2", ""))

    def test_signals_before_the_last_checkpoint_are_ignored(self):
        for days in (6, 5):
            self.analyzed("B2", days_ago=days)
        checkpoint_lesson = Lesson.objects.create(
            learner=self.learner, track=self.track, skill="checkpoint", status="analyzed",
            scheduled_for=self.today - timedelta(days=4), completed_at=timezone.now() - timedelta(days=4),
        )
        Checkpoint.objects.create(learner=self.learner, lesson=checkpoint_lesson, results={})
        self.learner.cefr_speaking = "B1"
        self.learner.save()
        self.analyzed("B2", days_ago=1)
        changed, detail = leveling.apply_signal(self.learner, "speaking")
        self.assertFalse(changed)
        self.assertEqual(detail["streak"], 1)
        self.assertEqual(leveling.recent_signals(self.learner, "speaking"), ["B2"])

    def test_reports_without_a_signal_are_skipped(self):
        lesson = Lesson.objects.create(learner=self.learner, track=self.track, skill="listening", status="analyzed", completed_at=timezone.now())
        LessonReport.objects.create(lesson=lesson, summary_es="x", raw_analysis={"_meta": {"cefr_signal": {}}})
        self.assertEqual(leveling.recent_signals(self.learner, "listening"), [])
        self.assertEqual(leveling.apply_signal(self.learner, "listening")[0], False)
        self.assertEqual(leveling.apply_signal(self.learner, "checkpoint")[0], False)


class PostprocessIntegrationTests(LevelingBase):
    def test_the_third_analyzed_lesson_moves_the_level(self):
        self.learner.cefr_speaking = "B1"
        self.learner.save()
        first = self.run_postprocess("B2")
        self.assertFalse(first.level_changed)
        self.assertEqual(first.level_detail["streak"], 1)
        self.run_postprocess("B2")
        third = self.run_postprocess("B2")
        self.assertTrue(third.level_changed)
        self.assertEqual(third.level_detail["new"], "B2")
        self.learner.refresh_from_db()
        self.assertEqual(self.learner.cefr_speaking, "B2")

    def test_report_page_explains_the_streak(self):
        self.learner.cefr_speaking = "B1"
        self.learner.save()
        summary = self.run_postprocess("B2")
        html = self.client.get(f"/lessons/{summary.report.lesson_id}/report/").content.decode()
        self.assertIn("1 of 3 lessons in a row point at B2", html)


class DashboardSignalTests(LevelingBase):
    def test_today_marks_a_level_that_is_moving(self):
        self.learner.cefr_speaking = "B1"
        self.learner.save()
        self.analyzed("B2", days_ago=2)
        self.analyzed("B2", days_ago=1)
        rows = {r["key"]: r for r in dashboard.skill_levels(self.learner)}
        self.assertEqual(rows["speaking"]["direction"], "up")
        self.assertEqual(rows["speaking"]["hint"], "B2 in 2 of the last 3 lessons")
        self.assertEqual(rows["writing"]["hint"], "")
        html = self.client.get("/").content.decode()
        self.assertIn('title="B2 in 2 of the last 3 lessons"', html)
        self.assertIn('class="sig up"', html)

    def test_a_level_that_agrees_with_the_signal_shows_no_arrow(self):
        self.learner.cefr_speaking = "B1"
        self.learner.save()
        self.analyzed("B1+", days_ago=1)
        rows = {r["key"]: r for r in dashboard.skill_levels(self.learner)}
        self.assertEqual(rows["speaking"]["hint"], "")
