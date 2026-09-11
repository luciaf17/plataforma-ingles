import json
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from learners.models import GrammarTopic, Learner, Track
from lessons.models import Lesson, Turn

from . import tutor

PLAN = {
    "title": "Walking an interviewer through your ERP",
    "summary": "Interview role play.",
    "tutor_role": "CTO interviewing you",
    "phases": [
        {"key": "warm_up", "title": "Your week", "tutor_goal": "relax", "prompts": ["How was your week?"], "minutes": 3},
        {"key": "mini_lesson", "title": "Since / for", "tutor_goal": "teach", "prompts": ["Explain since"], "minutes": 4},
        {"key": "practice", "title": "The interview", "tutor_goal": "elicit", "prompts": ["Tell me about the ERP"], "minutes": 9},
        {"key": "drill", "title": "Quick fire", "tutor_goal": "drill", "prompts": ["How long have you worked there?"], "minutes": 3},
        {"key": "wrap_up", "title": "Close", "tutor_goal": "close", "prompts": ["Two good things"], "minutes": 1},
    ],
    "targeted_errors": [{"id": 1, "learner_produced": "I work here since 2021", "correction": "I've worked here since 2021", "how_to_elicit": "Ask how long."}],
    "vocabulary": [{"term": "trade-off", "how_to_plant": "Ask about a trade-off."}],
    "if_stuck_hints": ["The way it works is…"],
    "duration_min": 20,
}


class RecastMarkupTests(SimpleTestCase):
    def test_for_speech_strips_markers(self):
        self.assertEqual(tutor.for_speech("Nice. *So you've been working on it since January*, right?"), "Nice. So you've been working on it since January, right?")

    def test_html_parts_flag_recasts(self):
        parts = tutor.to_html_parts("Nice. *since January*, and then?")
        self.assertEqual(parts, [(False, "Nice. "), (True, "since January"), (False, ", and then?")])

    def test_plain_text_is_one_part(self):
        self.assertEqual(tutor.to_html_parts("Hello there"), [(False, "Hello there")])

    def test_phase_info_reports_time_left(self):
        info = tutor.phase_info(PLAN, "practice", elapsed_in_phase_s=8 * 60 + 30)
        self.assertEqual((info["title"], info["minutes"], info["remaining_seconds"], info["almost_over"]), ("The interview", 9, 30, True))

    def test_unknown_phase_falls_back_to_first(self):
        self.assertEqual(tutor.phase_info(PLAN, "nope")["key"], "warm_up")


class BuildMessagesTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        user = get_user_model().objects.create_user("lu", first_name="Lu")
        self.learner = Learner.for_user(user)
        self.lesson = Lesson.objects.create(
            learner=self.learner, track=Track.objects.get(slug="work"), skill="speaking", status="in_progress",
            grammar_topic=GrammarTopic.objects.get(slug="present-perfect-since-for"), plan=PLAN,
        )
        Turn.objects.create(lesson=self.lesson, role="tutor", text="Hi Lu! How was your week?", sequence=1, phase="warm_up")
        Turn.objects.create(lesson=self.lesson, role="learner", text="It was okay, busy", sequence=2, phase="warm_up")

    def test_messages_carry_prompt_context_and_history(self):
        messages = tutor.build_messages(self.lesson, phase_key="warm_up", event="turn")
        self.assertIn("Never speak Spanish", messages[0]["content"])
        context = messages[1]["content"]
        self.assertIn('"name": "Lu"', context)
        self.assertIn('"event": "turn"', context)
        self.assertIn('"key": "warm_up"', context)
        self.assertIn("I work here since 2021", context)
        self.assertIn("Present perfect with since / for", context)
        self.assertEqual([m["role"] for m in messages[2:]], ["assistant", "user"])
        self.assertEqual(messages[-1]["content"], "It was okay, busy")

    def test_phase_start_adds_an_event_marker(self):
        messages = tutor.build_messages(self.lesson, phase_key="mini_lesson", event="phase_start")
        self.assertEqual(messages[-1], {"role": "user", "content": "[phase_start: phase mini_lesson]"})
        self.assertIn('"almost_over": false', messages[1]["content"])

    def test_history_is_capped(self):
        for i in range(3, 60):
            Turn.objects.create(lesson=self.lesson, role="learner" if i % 2 else "tutor", text=f"t{i}", sequence=i)
        messages = tutor.build_messages(self.lesson, phase_key="practice")
        self.assertEqual(len(messages), 2 + tutor.MAX_HISTORY_TURNS)

    def test_respond_uses_short_max_tokens_and_returns_text(self):
        result = SimpleNamespace(content="  Nice. *since January*. What next?  ")
        with mock.patch.object(tutor.client, "chat", return_value=result) as chat:
            text = tutor.respond(self.lesson, phase_key="practice", event="turn")
        self.assertEqual(text, "Nice. *since January*. What next?")
        self.assertEqual(chat.call_args.kwargs["max_tokens"], tutor.MAX_REPLY_TOKENS)


class FluencyRetellContextTests(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.learner = Learner.for_user(get_user_model().objects.create_user("lu", first_name="Lu"))
        self.lesson = Lesson.objects.create(
            learner=self.learner, track=Track.objects.get(slug="work"), skill="speaking",
            plan={**PLAN, "fluency_retell": {"prompt": "Tell me again how you chose the queue.", "rounds": [60, 40]}},
        )

    def context(self, phase="wrap_up"):
        messages = tutor.build_messages(self.lesson, phase_key=phase, event="phase_start")
        # The context message is prefixed with a label before the JSON.
        return json.loads(messages[1]["content"].split("\n", 1)[1])

    def test_the_tutor_gets_the_retell_and_its_clock(self):
        retell = self.context()["plan"]["fluency_retell"]
        self.assertEqual(retell["prompt"], "Tell me again how you chose the queue.")
        self.assertEqual(retell["rounds"], [60, 40])

    def test_the_rules_of_the_round_reach_the_system_prompt(self):
        system = tutor.build_messages(self.lesson, phase_key="wrap_up", event="phase_start")[0]["content"]
        self.assertIn("fluency round", system)
        self.assertIn("Do not correct anything", system)

    def test_a_lesson_without_one_carries_nothing(self):
        self.lesson.plan = PLAN
        self.lesson.save()
        self.assertIsNone(self.context()["plan"]["fluency_retell"])
