from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from ai import client as ai_client
from ai import review as ai_review
from ai.models import ApiCall
from ai.tests_tutor import PLAN
from core import progress, views
from learners.models import Learner, Track
from lessons.models import Checkpoint, ErrorItem, Lesson, LessonReport, ProgressReview, Turn, VocabItem

PRICES = {"chat": {"input_per_m": 2.0, "output_per_m": 10.0}, "gpt-4o-test": {"input_per_m": 2.0, "output_per_m": 10.0}}


class ProgressBase(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.learner.goal_statement = "interviews"
        self.learner.save()
        self.client.login(username="lu", password="pw")
        self.track = Track.objects.get(slug="work")
        self.today = timezone.localdate()

    def lesson(self, *, days_ago=0, skill="speaking", targeted=0, avoided=0, new=0, wpm=None, fillers=None, summary="Nota."):
        plan = {**PLAN, "targeted_error_ids": list(range(1, targeted + 1))}
        lesson = Lesson.objects.create(
            learner=self.learner, track=self.track, skill=skill, status="analyzed", plan=plan,
            scheduled_for=self.today - timedelta(days=days_ago), completed_at=timezone.now() - timedelta(days=days_ago),
        )
        LessonReport.objects.create(
            lesson=lesson, summary_es=summary, strengths=["Clear story."], focus_next=["depend on"],
            errors_avoided_count=avoided, new_errors_count=new, fluency_wpm=wpm, filler_ratio=fillers,
            raw_analysis={"_meta": {"cefr_signal": {}}},
        )
        return lesson

    def error(self, produced, **kwargs):
        # All errors share one old source lesson, so lesson counts stay obvious.
        if not hasattr(self, "_source"):
            self._source = self.lesson(days_ago=9, summary="Clase vieja.")
        fields = {"category": "grammar", "subcategory": "tense", "correction": "fixed", "explanation": "x", "source_lesson": self._source}
        fields.update(kwargs)
        return ErrorItem.objects.create(learner=self.learner, learner_produced=produced, **fields)


class MetricsTests(ProgressBase):
    def test_headline_counts_and_the_avoided_share(self):
        self.lesson(days_ago=1, targeted=3, avoided=2)
        self.lesson(days_ago=3, targeted=3, avoided=3)
        self.lesson(days_ago=10, targeted=4, avoided=0)  # outside the 7-day window
        self.lesson(days_ago=40)  # outside the 30-day window
        self.error("mastered one", status="mastered")
        head = progress.headline(self.learner, self.today)
        self.assertEqual((head["lessons"], head["mastered"]), (4, 1))  # three in range plus the error's source lesson
        self.assertEqual((head["avoided"], head["targeted"], head["avoided_pct"]), (5, 6, 83))

    def test_headline_without_targeted_lessons(self):
        self.lesson(days_ago=1)
        self.assertIsNone(progress.headline(self.learner, self.today)["avoided_pct"])

    def test_chart_has_one_bar_per_targeted_lesson_oldest_first(self):
        self.lesson(days_ago=3, targeted=4, avoided=1)
        self.lesson(days_ago=2, targeted=2, avoided=2)
        self.lesson(days_ago=1)  # no targets, no bar
        bars = progress.avoided_chart(self.learner)
        self.assertEqual([(b["avoided"], b["targeted"], b["good"]) for b in bars], [(1, 4, False), (2, 2, True)])
        self.assertEqual(bars[0]["date"], self.today - timedelta(days=3))
        self.assertEqual(bars[1]["height"], 100)
        self.assertIn("2 of 2 avoided", bars[1]["title"])

    def test_chart_is_capped(self):
        for i in range(15):
            self.lesson(days_ago=i, targeted=2, avoided=1)
        self.assertEqual(len(progress.avoided_chart(self.learner)), progress.CHART_LESSONS)

    def test_pace_averages_and_share(self):
        lesson = self.lesson(days_ago=1, wpm=90.0, fillers=0.04)
        self.lesson(days_ago=2, wpm=110.0, fillers=0.06)
        Turn.objects.create(lesson=lesson, role="learner", text=" ".join(["w"] * 70), sequence=1)
        Turn.objects.create(lesson=lesson, role="tutor", text=" ".join(["w"] * 30), sequence=2)
        pace = progress.pace(self.learner, self.today)
        self.assertEqual((pace["wpm"], pace["filler_pct"], pace["share_pct"]), (100, 5, 70))

    def test_pace_without_data(self):
        pace = progress.pace(self.learner, self.today)
        self.assertEqual((pace["wpm"], pace["filler_pct"], pace["share_pct"]), (None, None, None))

    def test_file_counts(self):
        self.error("active one")
        self.error("mastered one", status="mastered")
        self.error("dismissed one", status="dismissed")
        VocabItem.objects.create(learner=self.learner, term="trade-off")
        VocabItem.objects.create(learner=self.learner, term="upfront", status="acquired")
        counts = progress.file_counts(self.learner)
        self.assertEqual((counts["active"], counts["mastered"], counts["due"]), (1, 1, 1))
        self.assertEqual((counts["vocab_target"], counts["vocab_acquired"]), (1, 1))


class ProgressPageTests(ProgressBase):
    def test_page_shows_the_headline_chart_pace_and_cost(self):
        self.lesson(days_ago=1, targeted=3, avoided=2, wpm=96.0, fillers=0.06)
        with override_settings(OPENAI_PRICES=PRICES):
            ApiCall.record("chat", "gpt-4o-test", prompt_tokens=1_000_000, purpose="tutor")
        html = self.client.get("/progress/").content.decode()
        self.assertIn("67%", html)
        self.assertIn("targeted errors avoided, last 7 days", html)
        self.assertIn("2 of 3", html)
        self.assertIn("Errors avoided per lesson", html)
        self.assertIn("2 of 3 avoided", html)  # bar tooltip
        self.assertIn(">96<", html)
        self.assertIn("What this costs to run", html)
        self.assertIn("$2.00", html)
        self.assertIn("No checkpoint yet", html)
        self.assertIn("Review my recent lessons", html)

    def test_empty_state(self):
        html = self.client.get("/progress/").content.decode()
        self.assertIn("No lesson has targeted an error yet", html)
        self.assertIn("Give at least one lesson", html)
        self.assertNotIn("Review my recent lessons", html)

    def test_last_checkpoint_is_shown_with_its_levels(self):
        lesson = self.lesson(days_ago=5, skill="checkpoint")
        Checkpoint.objects.create(
            learner=self.learner, lesson=lesson, report_es="Estás en B1+ en speaking.",
            results={
                "speaking": {"estimate": "B1+", "assessed": True}, "writing": {"estimate": "B1", "assessed": True},
                "listening": {"estimate": "B2", "assessed": True}, "reading": {"estimate": "", "assessed": False},
            },
        )
        html = self.client.get("/progress/").content.decode()
        self.assertIn("Last checkpoint", html)
        self.assertIn("Estás en B1+ en speaking.", html)
        self.assertIn("B1+", html)
        self.assertIn("See the full checkpoint", html)
        self.assertEqual(html.count('class="cp-level"'), 3)


class ProgressReviewTests(ProgressBase):
    def fake(self):
        return SimpleNamespace(
            data={
                "summary_es": "En estas dos semanas evitaste más errores de los que sumaste.",
                "improving": ["'depends on' ya está en caja 3"],
                "stuck": ["El present perfect vuelve cuando hablás de experiencia", " "],
                "focus": ["Pedí una clase de speaking sobre tu trabajo"],
            },
            prompt_tokens=800, completion_tokens=300,
        )

    def test_review_reads_lessons_errors_and_vocabulary(self):
        self.lesson(days_ago=2, targeted=3, avoided=2, summary="Buena clase el lunes.")
        self.lesson(days_ago=1, targeted=3, avoided=1, summary="Ayer costó más.")
        self.error("it depends of the client", occurrences=4, srs_box=1)
        self.error("mastered one", status="mastered")
        VocabItem.objects.create(learner=self.learner, term="bottleneck", status="emerging", times_produced=2)

        with mock.patch.object(ai_review.client, "chat_json", return_value=self.fake()) as chat_json:
            response = self.client.post("/progress/review/", HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)

        context = chat_json.call_args.args[0][1]["content"]
        self.assertIn("Buena clase el lunes.", context)
        self.assertIn("it depends of the client", context)
        self.assertIn("bottleneck", context)
        self.assertIn('"lessons": 3', context)  # two here plus the error's source lesson

        review = ProgressReview.objects.get()
        self.assertEqual(review.improving, ["'depends on' ya está en caja 3"])
        self.assertEqual(review.stuck, ["El present perfect vuelve cuando hablás de experiencia"])
        self.assertEqual(review.lessons_covered, 3)
        self.assertEqual(review.period_end, self.today - timedelta(days=1))

        html = response.content.decode()
        self.assertIn("evitaste más errores", html)
        self.assertIn("Do this next", html)
        self.assertIn("Review again", html)

        # The stored review shows on the page without another call.
        with mock.patch.object(ai_review.client, "chat_json") as chat_json:
            page = self.client.get("/progress/").content.decode()
        chat_json.assert_not_called()
        self.assertIn("evitaste más errores", page)

    def test_review_without_lessons_is_refused(self):
        with mock.patch.object(ai_review.client, "chat_json") as chat_json:
            response = self.client.post("/progress/review/", HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 422)
        chat_json.assert_not_called()
        self.assertIn("no analyzed lessons", response.content.decode())

    def test_failure_keeps_the_card_usable(self):
        self.lesson(days_ago=1)
        with mock.patch.object(ai_review.client, "chat_json", side_effect=ai_client.AIUnavailable("down")):
            response = self.client.post("/progress/review/", HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Could not write the review", response.content.decode())
        self.assertEqual(ProgressReview.objects.count(), 0)

    def test_only_the_newest_review_is_shown(self):
        self.lesson(days_ago=1)
        with mock.patch.object(ai_review.client, "chat_json", return_value=self.fake()):
            self.client.post("/progress/review/", HTTP_HX_REQUEST="true")
            newer = self.fake()
            newer.data = {**newer.data, "summary_es": "Segunda lectura."}
            with mock.patch.object(ai_review.client, "chat_json", return_value=newer):
                self.client.post("/progress/review/", HTTP_HX_REQUEST="true")
        self.assertEqual(ProgressReview.objects.count(), 2)
        html = self.client.get("/progress/").content.decode()
        self.assertIn("Segunda lectura.", html)
        self.assertNotIn("evitaste más errores", html)
