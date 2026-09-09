from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings
from openai import APIConnectionError, APIStatusError

from . import client


def fake_completion(content, *, refusal=None, prompt_tokens=10, completion_tokens=5):
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
        model="gpt-4o-test",
    )


@override_settings(OPENAI_API_KEY="sk-test")
class ChatJsonTests(SimpleTestCase):
    def setUp(self):
        client.reset_client()
        self.addCleanup(client.reset_client)

    def test_parses_json_and_reports_usage(self):
        with mock.patch.object(client, "get_client") as get_client:
            get_client.return_value.chat.completions.create.return_value = fake_completion('{"ok": true}')
            result = client.chat_json([{"role": "user", "content": "hi"}], {"type": "object"}, schema_name="t")

        self.assertEqual(result.data, {"ok": True})
        self.assertEqual((result.prompt_tokens, result.completion_tokens), (10, 5))
        kwargs = get_client.return_value.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["response_format"]["type"], "json_schema")
        self.assertTrue(kwargs["response_format"]["json_schema"]["strict"])
        self.assertEqual(kwargs["response_format"]["json_schema"]["name"], "t")

    def test_invalid_json_raises_ai_unavailable(self):
        with mock.patch.object(client, "get_client") as get_client:
            get_client.return_value.chat.completions.create.return_value = fake_completion("not json")
            with self.assertRaises(client.AIUnavailable):
                client.chat_json([], {"type": "object"})

    def test_refusal_raises_ai_unavailable(self):
        with mock.patch.object(client, "get_client") as get_client:
            get_client.return_value.chat.completions.create.return_value = fake_completion("", refusal="no")
            with self.assertRaises(client.AIUnavailable):
                client.chat([])

    def test_connection_errors_become_ai_unavailable(self):
        with mock.patch.object(client, "get_client") as get_client:
            get_client.return_value.chat.completions.create.side_effect = APIConnectionError(request=mock.Mock())
            with self.assertRaises(client.AIUnavailable):
                client.chat([])

    def test_http_errors_become_ai_unavailable(self):
        response = mock.Mock(status_code=503, headers={})
        error = APIStatusError("down", response=response, body=None)
        with mock.patch.object(client, "get_client") as get_client:
            get_client.return_value.chat.completions.create.side_effect = error
            with self.assertRaises(client.AIUnavailable):
                client.chat([])


@override_settings(OPENAI_API_KEY="")
class MissingKeyTests(SimpleTestCase):
    def test_missing_key_is_reported_clearly(self):
        client.reset_client()
        self.addCleanup(client.reset_client)
        with self.assertRaises(client.AIUnavailable):
            client.get_client()


@override_settings(OPENAI_API_KEY="sk-test", OPENAI_STT_MODEL="stt-test", OPENAI_TTS_MODEL="tts-test")
class AudioTests(SimpleTestCase):
    def setUp(self):
        client.reset_client()
        self.addCleanup(client.reset_client)

    def test_transcribe_sends_verbatim_prompt_and_counts_words(self):
        with mock.patch.object(client, "get_client") as get_client:
            get_client.return_value.audio.transcriptions.create.return_value = SimpleNamespace(text=" I work here since 2021 ")
            result = client.transcribe(("a.webm", b"bytes"))

        self.assertEqual(result.text, "I work here since 2021")
        self.assertEqual(result.word_count, 5)
        kwargs = get_client.return_value.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "stt-test")
        self.assertIn("Do not fix grammar", kwargs["prompt"])

    def test_speak_returns_bytes(self):
        with mock.patch.object(client, "get_client") as get_client:
            get_client.return_value.audio.speech.create.return_value = SimpleNamespace(content=b"mp3")
            audio = client.speak("Hello", voice="sage")

        self.assertEqual(audio, b"mp3")
        kwargs = get_client.return_value.audio.speech.create.call_args.kwargs
        self.assertEqual((kwargs["model"], kwargs["voice"], kwargs["input"]), ("tts-test", "sage", "Hello"))
