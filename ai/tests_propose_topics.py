"""New class subjects written out of one learner's file."""

import json
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from ai import propose_topics
from learners.models import Learner, Topic, Track
from lessons.models import ErrorItem, Lesson


def answer(*topics):
    return SimpleNamespace(
        data={"topics": list(topics)}, content="{}", model="gpt-4o-test", prompt_tokens=10, completion_tokens=20,
    )


TOPIC = {
    "title": "Admitting you broke production to a client",
    "description": "She explains what went wrong and what she is doing about it.",
    "track": "work",
    "seed_vocabulary": ["own up to", "roll back", "get to the bottom of"],
    "reason_es": "Porque pediste practicar situaciones incómodas.",
}


class ProposeBase(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.learner = Learner.for_user(get_user_model().objects.create_user("lu"))
        self.learner.goal_statement = "technical interviews"
        self.learner.save()
        self.work = Track.objects.get(slug="work")


class VisibilityTests(ProposeBase):
    def test_a_proposed_topic_belongs_to_one_learner(self):
        mine = Topic.objects.create(track=self.work, title="Mine", learner=self.learner)
        other = Learner.for_user(get_user_model().objects.create_user("eze"))
        theirs = Topic.objects.create(track=self.work, title="Theirs", learner=other)

        visible = Topic.objects.visible_to(self.learner)
        self.assertIn(mine, visible)
        self.assertNotIn(theirs, visible)
        # The seeded ones stay shared.
        self.assertIn(Topic.objects.filter(learner=None).first(), visible)
        self.assertTrue(mine.is_proposed)
        self.assertFalse(Topic.objects.filter(learner=None).first().is_proposed)

    def test_an_inactive_topic_is_never_visible(self):
        hidden = Topic.objects.create(track=self.work, title="Hidden", learner=self.learner, is_active=False)
        self.assertNotIn(hidden, Topic.objects.visible_to(self.learner))


class ProposeTests(ProposeBase):
    def test_it_stores_what_the_model_wrote(self):
        with mock.patch.object(propose_topics.client, "chat_json", return_value=answer(TOPIC)):
            created = propose_topics.propose(self.learner, how_many=3)
        self.assertEqual(len(created), 1)
        topic = created[0]
        self.assertEqual((topic.title, topic.track.slug, topic.learner), (TOPIC["title"], "work", self.learner))
        self.assertEqual(topic.seed_vocabulary, ["own up to", "roll back", "get to the bottom of"])
        self.assertIn("incómodas", topic.proposed_reason)

    def test_her_file_reaches_the_model(self):
        lesson = Lesson.objects.create(learner=self.learner, track=self.work, skill="speaking", plan={"learner_request": "I have an interview on Friday"})
        ErrorItem.objects.create(
            learner=self.learner, source_lesson=lesson, category="grammar", subcategory="prepositions",
            learner_produced="it depends of the client", correction="it depends on the client",
            explanation="x", occurrences=4,
        )
        with mock.patch.object(propose_topics.client, "chat_json", return_value=answer(TOPIC)) as chat:
            propose_topics.propose(self.learner)
        context = json.loads(chat.call_args.args[0][1]["content"])
        self.assertEqual(context["she_asked_for"], ["I have an interview on Friday"])
        self.assertEqual(context["stubborn_errors"][0]["produced"], "it depends of the client")
        self.assertEqual(context["goal"], "technical interviews")
        self.assertIn("Walking an interviewer through a system's architecture", context["existing_topics"])

    def test_a_duplicate_title_is_dropped(self):
        existing = Topic.objects.filter(learner=None).first()
        with mock.patch.object(propose_topics.client, "chat_json", return_value=answer({**TOPIC, "title": existing.title})):
            self.assertEqual(propose_topics.propose(self.learner), [])

    def test_an_unknown_track_is_dropped(self):
        with mock.patch.object(propose_topics.client, "chat_json", return_value=answer({**TOPIC, "track": "nonsense"})):
            self.assertEqual(propose_topics.propose(self.learner), [])

    def test_it_never_returns_more_than_asked(self):
        many = [{**TOPIC, "title": f"Situation {i}"} for i in range(6)]
        with mock.patch.object(propose_topics.client, "chat_json", return_value=answer(*many)):
            self.assertEqual(len(propose_topics.propose(self.learner, how_many=2)), 2)


class RunningLowTests(ProposeBase):
    def use_up(self, track, keep):
        """Give her a lesson on every topic of a track but `keep` of them."""
        topics = list(Topic.objects.visible_to(self.learner).filter(track=track))
        for topic in topics[: len(topics) - keep]:
            Lesson.objects.create(learner=self.learner, track=track, skill="speaking", topic=topic)

    def test_a_full_list_proposes_nothing(self):
        with mock.patch.object(propose_topics.client, "chat_json") as chat:
            self.assertEqual(propose_topics.propose_if_needed(self.learner), [])
        chat.assert_not_called()

    def test_running_out_in_one_track_is_enough(self):
        self.use_up(self.work, keep=propose_topics.LOW_WATER - 1)
        self.assertTrue(propose_topics.is_running_low(self.learner))
        with mock.patch.object(propose_topics.client, "chat_json", return_value=answer(TOPIC)):
            self.assertEqual(len(propose_topics.propose_if_needed(self.learner)), 1)
