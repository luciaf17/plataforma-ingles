from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from ai import client as ai_client
from ai.tests_tutor import PLAN
from learners.models import GrammarTopic, Learner, Track

from . import views
from .models import Lesson, Turn


@override_settings(MEDIA_ROOT="media/test")
class SpeakingRunnerTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        self.lesson = Lesson.objects.create(
            learner=self.learner, track=Track.objects.get(slug="work"), skill="speaking",
            grammar_topic=GrammarTopic.objects.get(slug="present-perfect-since-for"), plan=PLAN,
        )
        self.url = f"/lessons/{self.lesson.id}/"

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_opening_the_runner_starts_the_lesson_and_renders_the_plan(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "in_progress")
        self.assertIsNotNone(self.lesson.started_at)
        html = response.content.decode()
        self.assertIn("Walking an interviewer through your ERP", html)
        self.assertIn('data-phase="warm_up"', html)
        self.assertIn("I work here since 2021", html)
        self.assertIn("The way it works is…", html)
        self.assertIn('"current_phase": "warm_up"', html)
        self.assertIn('"has_turns": false', html)

    def test_reload_keeps_the_clock_and_turns(self):
        self.client.get(self.url)
        Turn.objects.create(lesson=self.lesson, role="tutor", text="Hi! *since 2021* yes", sequence=1)
        response = self.client.get(self.url)
        html = response.content.decode()
        self.assertIn('<span class="recast">since 2021</span>', html)
        self.assertIn('"has_turns": true', html)

    def test_other_learners_lesson_is_404(self):
        other = Learner.for_user(get_user_model().objects.create_user("other"))
        foreign = Lesson.objects.create(learner=other, track=self.lesson.track, skill="speaking", plan=PLAN)
        self.assertEqual(self.client.get(f"/lessons/{foreign.id}/").status_code, 404)

    def test_finished_lesson_redirects(self):
        self.lesson.status = "completed"
        self.lesson.save()
        response = self.client.get(self.url)
        self.assertRedirects(response, f"/lessons/{self.lesson.id}/finished/")

    def test_current_phase_from_elapsed(self):
        self.assertEqual(views.current_phase_key(PLAN, 0), ("warm_up", 0))
        self.assertEqual(views.current_phase_key(PLAN, 3 * 60), ("mini_lesson", 0))
        self.assertEqual(views.current_phase_key(PLAN, 10 * 60), ("practice", 3 * 60))
        self.assertEqual(views.current_phase_key(PLAN, 25 * 60), ("wrap_up", 6 * 60))


@override_settings(MEDIA_ROOT="media/test")
class TurnEndpointTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        self.lesson = Lesson.objects.create(
            learner=self.learner, track=Track.objects.get(slug="work"), skill="speaking", status="in_progress",
            started_at=timezone.now(), plan=PLAN,
        )
        self.turn_url = f"/lessons/{self.lesson.id}/turn/"
        self.tutor_url = f"/lessons/{self.lesson.id}/tutor/"

    def patched(self, transcript="I work here since 2021", reply="Nice. *You've worked there since 2021.* What do you build?", tts=b"mp3"):
        patches = [
            mock.patch.object(ai_client, "transcribe", return_value=ai_client.TranscriptResult(text=transcript, model="stt", word_count=len(transcript.split()))),
            mock.patch.object(views.tutor, "respond", return_value=reply),
            mock.patch.object(ai_client, "speak", return_value=tts),
        ]
        return patches

    def test_audio_turn_transcribes_answers_and_voices(self):
        patches = self.patched()
        with patches[0] as transcribe, patches[1] as respond, patches[2]:
            response = self.client.post(self.turn_url, {
                "audio": SimpleUploadedFile("turn.webm", b"opus-bytes", content_type="audio/webm"),
                "phase": "practice", "duration_ms": "3200", "elapsed_in_phase": "61",
            })

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["learner"]["text"], "I work here since 2021")
        self.assertEqual(data["learner"]["word_count"], 5)
        self.assertIn('<span class="recast">', data["tutor"]["html"])
        self.assertTrue(data["tutor"]["audio_url"].endswith(".mp3"))

        transcribe.assert_called_once()
        self.assertEqual(respond.call_args.kwargs, {"phase_key": "practice", "event": "turn", "elapsed_in_phase_s": 61})

        learner_turn, tutor_turn = Turn.objects.order_by("sequence")
        self.assertEqual((learner_turn.role, learner_turn.phase, learner_turn.audio_duration_ms, learner_turn.sequence), ("learner", "practice", 3200, 1))
        self.assertTrue(learner_turn.audio_file.name.endswith(".webm"))
        self.assertEqual((tutor_turn.role, tutor_turn.sequence), ("tutor", 2))
        self.assertTrue(tutor_turn.audio_file.name.endswith(".mp3"))

    def test_text_turn_skips_transcription(self):
        patches = self.patched()
        with patches[0] as transcribe, patches[1], patches[2]:
            response = self.client.post(self.turn_url, {"text": "It depends of the client", "phase": "drill"})
        self.assertEqual(response.status_code, 200)
        transcribe.assert_not_called()
        self.assertEqual(Turn.objects.get(role="learner").text, "It depends of the client")

    def test_tts_failure_still_returns_text(self):
        patches = self.patched()
        with patches[0], patches[1], mock.patch.object(ai_client, "speak", side_effect=ai_client.AIUnavailable("tts down")):
            response = self.client.post(self.turn_url, {"text": "hello", "phase": "warm_up"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["tutor"]["audio_url"])
        self.assertIn("since 2021", data["tutor"]["text"])

    def test_transcription_failure_is_503_and_stores_nothing(self):
        with mock.patch.object(ai_client, "transcribe", side_effect=ai_client.AIUnavailable("stt down")):
            response = self.client.post(self.turn_url, {"audio": SimpleUploadedFile("t.webm", b"x"), "phase": "practice"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(Turn.objects.count(), 0)

    def test_tutor_failure_keeps_the_learner_turn(self):
        with mock.patch.object(views.tutor, "respond", side_effect=ai_client.AIUnavailable("llm down")):
            response = self.client.post(self.turn_url, {"text": "hello", "phase": "practice"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["learner"]["text"], "hello")
        self.assertEqual(Turn.objects.count(), 1)

    def test_empty_turn_is_rejected(self):
        response = self.client.post(self.turn_url, {"text": "   ", "phase": "practice"})
        self.assertEqual(response.status_code, 422)

    def test_tutor_prompt_for_lesson_start_and_phase_change(self):
        patches = self.patched(reply="Hi Lu! Today we talk about your ERP. How was your week?")
        with patches[1] as respond, patches[2]:
            response = self.client.post(self.tutor_url, {"phase": "warm_up", "event": "lesson_start"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(respond.call_args.kwargs["event"], "lesson_start")
        self.assertEqual(Turn.objects.get().role, "tutor")

        with patches[1] as respond, patches[2]:
            self.client.post(self.tutor_url, {"phase": "mini_lesson", "event": "phase_start", "elapsed_in_phase": "0"})
        self.assertEqual(respond.call_args.kwargs["phase_key"], "mini_lesson")
        self.assertEqual(Turn.objects.count(), 2)

    def test_unknown_phase_and_event_fall_back(self):
        patches = self.patched()
        with patches[1] as respond, patches[2]:
            self.client.post(self.tutor_url, {"phase": "party", "event": "dance"})
        self.assertEqual(respond.call_args.kwargs, {"phase_key": "warm_up", "event": "phase_start", "elapsed_in_phase_s": 0})

    def test_endpoints_refuse_when_not_in_progress(self):
        self.lesson.status = "planned"
        self.lesson.save()
        self.assertEqual(self.client.post(self.turn_url, {"text": "hi"}).status_code, 409)
        self.assertEqual(self.client.post(self.tutor_url, {"phase": "warm_up"}).status_code, 409)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.turn_url).status_code, 405)


class EndLessonTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        self.lesson = Lesson.objects.create(
            learner=self.learner, track=Track.objects.get(slug="work"), skill="speaking", status="in_progress",
            started_at=timezone.now() - timezone.timedelta(minutes=19), plan=PLAN,
        )

    def test_end_completes_and_records_duration(self):
        response = self.client.post(f"/lessons/{self.lesson.id}/end/")
        self.assertRedirects(response, f"/lessons/{self.lesson.id}/finished/")
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.status, "completed")
        self.assertGreaterEqual(self.lesson.duration_seconds, 19 * 60)
        self.assertEqual(self.client.get(f"/lessons/{self.lesson.id}/finished/").status_code, 200)


@override_settings(LESSON_SKILLS_ENABLED=["speaking"])
class SpeakingTodayTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")

    def test_redirects_to_todays_lesson_preparing_it_lazily(self):
        with mock.patch.object(views.planner, "prepare_next_lesson") as prepare:
            lesson = Lesson.objects.create(learner=self.learner, track=Track.objects.get(slug="work"), skill="speaking", plan=PLAN)
            prepare.return_value = (lesson, True)
            Lesson.objects.filter(id=lesson.id).update(scheduled_for="2000-01-01")  # not today, so lazy prepare runs
            response = self.client.get("/speaking/")
        self.assertRedirects(response, f"/lessons/{lesson.id}/", fetch_redirect_response=False)
        self.assertEqual(prepare.call_args.kwargs["skill"], "speaking")

    def test_existing_todays_lesson_is_reused(self):
        lesson = Lesson.objects.create(learner=self.learner, track=Track.objects.get(slug="work"), skill="speaking", plan=PLAN)
        with mock.patch.object(views.planner, "prepare_next_lesson") as prepare:
            response = self.client.get("/speaking/")
        prepare.assert_not_called()
        self.assertRedirects(response, f"/lessons/{lesson.id}/", fetch_redirect_response=False)

    def test_unavailable_page_when_planner_fails(self):
        with mock.patch.object(views.planner, "prepare_next_lesson", side_effect=ai_client.AIUnavailable("down")):
            response = self.client.get("/speaking/")
        self.assertEqual(response.status_code, 503)
        self.assertIn("could not be prepared", response.content.decode())
