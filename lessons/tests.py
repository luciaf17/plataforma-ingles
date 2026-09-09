from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase

from learners.models import Learner, Track

from . import srs
from .models import ErrorItem, Lesson, Turn, VocabItem
from .taxonomy import TAXONOMY, validate_taxonomy

TODAY = date(2026, 9, 9)


def make_learner(username="lu"):
    return Learner.for_user(get_user_model().objects.create_user(username))


def make_lesson(learner, **kwargs):
    track, _ = Track.objects.get_or_create(slug="work", defaults={"name": "Work"})
    return Lesson.objects.create(learner=learner, track=track, skill="speaking", scheduled_for=TODAY, **kwargs)


def make_error(learner, lesson, produced, **kwargs):
    fields = {
        "category": "grammar",
        "subcategory": "tense",
        "correction": "fixed",
        "explanation": "explicación",
        "next_review_at": TODAY,
    }
    fields.update(kwargs)
    return ErrorItem.objects.create(learner=learner, source_lesson=lesson, learner_produced=produced, **fields)


class ErrorItemDueTests(TestCase):
    def setUp(self):
        self.learner = make_learner()
        self.lesson = make_lesson(self.learner)

    def test_due_returns_active_items_whose_review_date_has_arrived(self):
        due_today = make_error(self.learner, self.lesson, "due today", next_review_at=TODAY)
        overdue = make_error(self.learner, self.lesson, "overdue", next_review_at=TODAY - timedelta(days=3))
        make_error(self.learner, self.lesson, "tomorrow", next_review_at=TODAY + timedelta(days=1))

        due = ErrorItem.objects.due_for(self.learner, on=TODAY)

        self.assertEqual(set(due), {due_today, overdue})

    def test_due_excludes_mastered_and_dismissed(self):
        make_error(self.learner, self.lesson, "mastered", status="mastered", srs_box=5)
        make_error(self.learner, self.lesson, "dismissed", status="dismissed")
        active = make_error(self.learner, self.lesson, "active")

        self.assertEqual(list(ErrorItem.objects.due_for(self.learner, on=TODAY)), [active])

    def test_due_is_scoped_to_the_learner(self):
        other = make_learner("someone-else")
        make_error(other, make_lesson(other), "not mine")
        mine = make_error(self.learner, self.lesson, "mine")

        self.assertEqual(list(ErrorItem.objects.due_for(self.learner, on=TODAY)), [mine])
        self.assertEqual(ErrorItem.objects.due_for(other, on=TODAY).count(), 1)

    def test_due_orders_most_repeated_first(self):
        once = make_error(self.learner, self.lesson, "once", occurrences=1)
        four = make_error(self.learner, self.lesson, "four times", occurrences=4)
        twice = make_error(self.learner, self.lesson, "twice", occurrences=2)

        self.assertEqual(list(ErrorItem.objects.due_for(self.learner, on=TODAY)), [four, twice, once])

    def test_due_defaults_to_today(self):
        make_error(self.learner, self.lesson, "yesterday", next_review_at=srs.today() - timedelta(days=1))
        make_error(self.learner, self.lesson, "next week", next_review_at=srs.today() + timedelta(days=7))

        self.assertEqual(ErrorItem.objects.due_for(self.learner).count(), 1)

    def test_is_due_property(self):
        item = make_error(self.learner, self.lesson, "x", next_review_at=srs.today())
        self.assertTrue(item.is_due)
        item.status = "mastered"
        self.assertFalse(item.is_due)


class VocabItemDueTests(TestCase):
    def setUp(self):
        self.learner = make_learner()

    def make(self, term, **kwargs):
        fields = {"next_review_at": TODAY}
        fields.update(kwargs)
        return VocabItem.objects.create(learner=self.learner, term=term, **fields)

    def test_due_includes_target_and_emerging_but_not_acquired(self):
        target = self.make("trade-off", status="target")
        emerging = self.make("upfront", status="emerging")
        self.make("cozy", status="acquired")
        self.make("bottleneck", status="target", next_review_at=TODAY + timedelta(days=2))

        self.assertEqual(set(VocabItem.objects.due_for(self.learner, on=TODAY)), {target, emerging})

    def test_term_is_normalised_and_unique_per_learner(self):
        self.make("  Push Back ")
        self.assertEqual(VocabItem.objects.get().term, "push back")
        with self.assertRaises(Exception):
            self.make("push back")

    def test_due_is_scoped_to_the_learner(self):
        other = make_learner("other")
        VocabItem.objects.create(learner=other, term="theirs", next_review_at=TODAY)
        self.make("mine")

        self.assertEqual([v.term for v in VocabItem.objects.due_for(self.learner, on=TODAY)], ["mine"])


class TaxonomyTests(TestCase):
    def test_every_category_has_subcategories(self):
        for category, subs in TAXONOMY.items():
            self.assertTrue(subs, category)

    def test_validate_rejects_unknown_pairs(self):
        validate_taxonomy("grammar", "tense")
        with self.assertRaises(ValueError):
            validate_taxonomy("grammar", "false_friend")
        with self.assertRaises(ValueError):
            validate_taxonomy("emotions", "tense")

    def test_error_item_save_rejects_invented_taxonomy(self):
        learner = make_learner()
        lesson = make_lesson(learner)
        with self.assertRaises(ValueError):
            make_error(learner, lesson, "x", category="grammar", subcategory="vibes")


class SrsTests(TestCase):
    def test_intervals_double_per_box(self):
        base = date(2026, 1, 1)
        self.assertEqual(srs.next_review_date(0, base), base + timedelta(days=1))
        self.assertEqual(srs.next_review_date(1, base), base + timedelta(days=2))
        self.assertEqual(srs.next_review_date(2, base), base + timedelta(days=4))
        self.assertEqual(srs.next_review_date(3, base), base + timedelta(days=8))
        self.assertEqual(srs.next_review_date(4, base), base + timedelta(days=16))

    def test_promote_caps_at_mastered_and_demote_resets(self):
        self.assertEqual(srs.promote(4), srs.MASTERED_BOX)
        self.assertEqual(srs.promote(5), srs.MASTERED_BOX)
        self.assertEqual(srs.demote(4), 0)


class TurnTests(TestCase):
    def test_word_count_is_filled_on_save(self):
        learner = make_learner()
        lesson = make_lesson(learner)
        turn = Turn.objects.create(lesson=lesson, role="learner", text="I work here since 2021", sequence=1)
        self.assertEqual(turn.word_count, 5)
