from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings

from . import client
from .models import ApiCall
from .tests import fake_completion

PRICES = {
    "gpt-4o-test": {"input_per_m": 2.0, "output_per_m": 10.0},
    "chat": {"input_per_m": 2.0, "output_per_m": 10.0},
    "stt-test": {"per_minute": 0.006},
    "tts-test": {"per_k_chars": 0.02},
}


@override_settings(OPENAI_API_KEY="sk-test", OPENAI_PRICES=PRICES, OPENAI_STT_MODEL="stt-test", OPENAI_TTS_MODEL="tts-test")
class UsageRecordingTests(TestCase):
    def setUp(self):
        client.reset_client()
        self.addCleanup(client.reset_client)

    def test_chat_records_tokens_and_cost(self):
        with mock.patch.object(client, "get_client") as get_client:
            get_client.return_value.chat.completions.create.return_value = fake_completion("hi", prompt_tokens=1_000_000, completion_tokens=100_000)
            client.chat([{"role": "user", "content": "x"}], model="gpt-4o-test", purpose="tutor", lesson_id=7)
        call = ApiCall.objects.get()
        self.assertEqual((call.kind, call.model, call.purpose, call.lesson_id), ("chat", "gpt-4o-test", "tutor", 7))
        self.assertEqual(float(call.cost_usd), 3.0)

    def test_chat_json_defaults_purpose_to_schema_name(self):
        with mock.patch.object(client, "get_client") as get_client:
            get_client.return_value.chat.completions.create.return_value = fake_completion('{"a": 1}')
            client.chat_json([], {"type": "object"}, schema_name="speaking_plan")
        self.assertEqual(ApiCall.objects.get().purpose, "speaking_plan")

    def test_transcribe_uses_duration_for_cost(self):
        with mock.patch.object(client, "get_client") as get_client:
            get_client.return_value.audio.transcriptions.create.return_value = SimpleNamespace(text="hello")
            client.transcribe(("a.webm", b"x"), duration_ms=30_000)
        call = ApiCall.objects.get()
        self.assertEqual((call.kind, call.audio_seconds, float(call.cost_usd)), ("transcribe", 30.0, 0.003))

    def test_speak_uses_characters_for_cost(self):
        with mock.patch.object(client, "get_client") as get_client:
            get_client.return_value.audio.speech.create.return_value = SimpleNamespace(content=b"mp3")
            client.speak("x" * 500)
        call = ApiCall.objects.get()
        self.assertEqual((call.kind, call.characters, float(call.cost_usd)), ("speak", 500, 0.01))

    def test_totals_by_window_and_kind(self):
        ApiCall.record("chat", "gpt-4o-test", prompt_tokens=500_000)
        ApiCall.record("speak", "tts-test", characters=1000)
        totals = ApiCall.totals()
        self.assertEqual(totals["today"]["calls"], 2)
        self.assertAlmostEqual(totals["all"]["cost"], 1.02)
        self.assertAlmostEqual(totals["by_kind"]["chat"], 1.0)
        self.assertAlmostEqual(totals["by_kind"]["speak"], 0.02)

    def test_unknown_model_falls_back_to_kind_price(self):
        self.assertEqual(ApiCall.estimate_cost("chat", "mystery-model", prompt_tokens=1_000_000), 2.0)


class ProgressPlaceholderTests(TestCase):
    def test_progress_page_shows_the_running_cost(self):
        from django.contrib.auth import get_user_model

        get_user_model().objects.create_user("lu", password="pw")
        self.client.login(username="lu", password="pw")
        with override_settings(OPENAI_PRICES=PRICES):
            ApiCall.record("chat", "gpt-4o-test", prompt_tokens=1_000_000, purpose="tutor")
        html = self.client.get("/progress/").content.decode()
        self.assertIn("API cost today", html)
        self.assertIn("$2.00", html)
        self.assertIn("Chat", html)
