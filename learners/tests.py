from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import GrammarTopic, Learner, Topic, Track


class SeedFixtureTests(TestCase):
    fixtures = ["seed"]

    def test_seed_loads_tracks_topics_and_syllabus(self):
        self.assertEqual(Track.objects.count(), 2)
        self.assertEqual(Topic.objects.filter(track__slug="work").count(), 10)
        self.assertEqual(Topic.objects.filter(track__slug="general").count(), 10)
        self.assertGreaterEqual(GrammarTopic.objects.count(), 30)

    def test_syllabus_is_ordered_by_level(self):
        levels = list(GrammarTopic.objects.order_by("order").values_list("cefr_level", flat=True))
        self.assertEqual(levels, sorted(levels, key=["A2", "B1", "B2"].index))

    def test_every_grammar_topic_has_examples_and_subcategories(self):
        for topic in GrammarTopic.objects.all():
            self.assertTrue(topic.examples, topic.slug)
            self.assertTrue(topic.related_subcategories, topic.slug)
            for example in topic.examples:
                self.assertEqual(set(example), {"wrong", "right", "note_es"}, topic.slug)


class LearnerTests(TestCase):
    def test_cefr_for_falls_back_to_one_band_below_target(self):
        user = get_user_model().objects.create_user("lu")
        learner = Learner.for_user(user)
        self.assertEqual(learner.cefr_for("speaking"), "B1")
        learner.cefr_speaking = "B2"
        self.assertEqual(learner.cefr_for("speaking"), "B2")
        with self.assertRaises(ValueError):
            learner.cefr_for("dancing")
