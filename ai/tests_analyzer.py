from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from lessons.taxonomy import SUBCATEGORIES

from . import analyzer
from .management.commands.analyze_sample import PLANTED, load_sample_transcript, match_planted


def raw_error(**overrides):
    base = {
        "category": "grammar",
        "subcategory": "tense",
        "learner_produced": "I work here since 2021",
        "correction": "I've been working here since 2021",
        "explanation_es": "Desde + presente en español; present perfect en inglés.",
        "confidence": "high",
        "is_recycled": False,
        "recycled_error_id": None,
    }
    base.update(overrides)
    return base


def raw_analysis(errors=(), **overrides):
    base = {
        "summary_es": "Buena clase.",
        "strengths": ["Clear architecture story."],
        "errors": list(errors),
        "recycled_error_ids_avoided": [],
        "new_vocabulary_produced": [],
        "vocabulary_gaps": [],
        "focus_next": ["depend on"],
        "cefr_signal": {"skill": "speaking", "estimate": "B1+", "reasoning": "Sustains a topic."},
    }
    base.update(overrides)
    return base


class SchemaTests(SimpleTestCase):
    def test_schema_uses_closed_taxonomy(self):
        error_schema = analyzer.ANALYSIS_SCHEMA["properties"]["errors"]["items"]
        self.assertEqual(error_schema["properties"]["category"]["enum"], ["grammar", "vocabulary", "pronunciation", "fluency", "discourse"])
        self.assertEqual(error_schema["properties"]["subcategory"]["enum"], SUBCATEGORIES)
        self.assertFalse(analyzer.ANALYSIS_SCHEMA["additionalProperties"])
        self.assertEqual(set(analyzer.ANALYSIS_SCHEMA["required"]), set(analyzer.ANALYSIS_SCHEMA["properties"]))


class ValidateTests(SimpleTestCase):
    def test_keeps_valid_errors(self):
        result = analyzer.validate(raw_analysis([raw_error()]))
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].subcategory, "tense")
        self.assertEqual(result.summary_es, "Buena clase.")
        self.assertEqual(result.raw["summary_es"], "Buena clase.")

    def test_drops_off_taxonomy_pairs(self):
        bad = raw_error(category="grammar", subcategory="false_friend")
        result = analyzer.validate(raw_analysis([bad, raw_error()]))
        self.assertEqual(len(result.errors), 1)

    def test_drops_errors_without_a_real_correction(self):
        same = raw_error(learner_produced="fine", correction="Fine")
        empty = raw_error(learner_produced="", correction="x")
        result = analyzer.validate(raw_analysis([same, empty]))
        self.assertEqual(result.errors, [])

    def test_caps_at_max_errors(self):
        many = [raw_error(learner_produced=f"error {i}") for i in range(12)]
        result = analyzer.validate(raw_analysis(many))
        self.assertEqual(len(result.errors), analyzer.MAX_ERRORS)

    def test_pronunciation_is_always_low_confidence(self):
        pron = raw_error(category="pronunciation", subcategory="phoneme", confidence="high", learner_produced="sheep", correction="ship")
        result = analyzer.validate(raw_analysis([pron]))
        self.assertEqual(result.errors[0].confidence, "low")

    def test_recycled_flags_only_count_for_targeted_ids(self):
        recycled = raw_error(is_recycled=True, recycled_error_id=7)
        fake = raw_error(learner_produced="depends of", correction="depends on", is_recycled=True, recycled_error_id=99)
        result = analyzer.validate(raw_analysis([recycled, fake], recycled_error_ids_avoided=[7, 8, 42]), targeted_ids=[7, 8])

        self.assertTrue(result.errors[0].is_recycled)
        self.assertEqual(result.errors[0].recycled_error_id, 7)
        self.assertFalse(result.errors[1].is_recycled)
        self.assertIsNone(result.errors[1].recycled_error_id)
        # 7 was made again, so it cannot also be avoided; 42 was never targeted.
        self.assertEqual(result.recycled_error_ids_avoided, [8])

    def test_vocabulary_is_deduplicated_and_lowercased(self):
        result = analyzer.validate(raw_analysis(new_vocabulary_produced=["Trade-off", "trade-off ", "bottleneck"]))
        self.assertEqual(result.new_vocabulary_produced, ["trade-off", "bottleneck"])

    def test_skill_overrides_model_signal(self):
        result = analyzer.validate(raw_analysis(), skill="writing")
        self.assertEqual(result.cefr_signal["skill"], "writing")


class TranscriptTests(SimpleTestCase):
    def test_format_transcript_from_tuples_and_objects(self):
        text = analyzer.format_transcript([("tutor", "Hi!"), ("learner", " Hello ")])
        self.assertEqual(text, "Tutor: Hi!\nLearner: Hello")

        turns = [
            SimpleNamespace(role="tutor", text="Warm up", phase="warm_up"),
            SimpleNamespace(role="learner", text="Ok", phase="warm_up"),
            SimpleNamespace(role="tutor", text="Now the lesson", phase="mini_lesson"),
        ]
        text = analyzer.format_transcript(turns)
        self.assertEqual(text, "[warm_up]\nTutor: Warm up\nLearner: Ok\n\n[mini_lesson]\nTutor: Now the lesson")

    def test_sample_fixture_parses_and_contains_the_planted_errors(self):
        transcript = load_sample_transcript()
        self.assertIn("[warm_up]", transcript)
        self.assertIn("[wrap_up]", transcript)
        for fragment, _ in PLANTED:
            self.assertIn(fragment, transcript.lower())

    def test_match_planted_requires_fragment_and_subcategory(self):
        errors = [
            analyzer.FoundError("grammar", "prepositions", "it depends of the client", "it depends on the client", "…", "high"),
            analyzer.FoundError("grammar", "tense", "actually I'm working", "currently I'm working", "…", "high"),
        ]
        found = match_planted(errors)
        self.assertEqual(list(found), [1])


class AnalyzeTranscriptTests(SimpleTestCase):
    def test_builds_messages_with_prompt_and_context_and_validates(self):
        chat = SimpleNamespace(data=raw_analysis([raw_error()]), prompt_tokens=100, completion_tokens=50)
        with mock.patch.object(analyzer.client, "chat_json", return_value=chat) as chat_json:
            result = analyzer.analyze_transcript(
                "Tutor: Hi\nLearner: I work here since 2021",
                skill="speaking",
                cefr="B1",
                targeted_errors=[{"id": 3, "learner_produced": "depends of", "correction": "depends on"}],
            )

        messages, schema = chat_json.call_args.args
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Taxonomy", messages[0]["content"])
        self.assertIn('"id": 3', messages[1]["content"])
        self.assertIn("I work here since 2021", messages[1]["content"])
        self.assertIs(schema, analyzer.ANALYSIS_SCHEMA)
        self.assertEqual(chat_json.call_args.kwargs["schema_name"], "lesson_analysis")
        self.assertEqual((result.prompt_tokens, result.completion_tokens), (100, 50))
        self.assertEqual(len(result.errors), 1)
