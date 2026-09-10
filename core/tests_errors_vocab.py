from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from learners.models import Learner, Track
from lessons.models import ErrorItem, Lesson, VocabItem


class Base(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)
        self.client.login(username="lu", password="pw")
        self.today = timezone.localdate()
        self.lesson = Lesson.objects.create(learner=self.learner, track=Track.objects.get(slug="work"), skill="writing", status="analyzed", scheduled_for=self.today - timedelta(days=2))

    def error(self, produced, **kwargs):
        fields = {"category": "grammar", "subcategory": "tense", "correction": "fixed", "explanation": "explicación", "next_review_at": self.today}
        fields.update(kwargs)
        return ErrorItem.objects.create(learner=self.learner, source_lesson=self.lesson, learner_produced=produced, **fields)


class ErrorsPageTests(Base):
    def test_table_shows_active_errors_with_boxes_source_and_dismiss(self):
        self.error("I work here since 2021", correction="I've worked here since 2021", occurrences=4, srs_box=2)
        self.error("mastered one", status="mastered", srs_box=5)
        html = self.client.get("/errors/").content.decode()
        self.assertIn("<s>I work here since 2021</s>", html)
        self.assertIn("I&#x27;ve worked here since 2021", html)
        self.assertIn("Grammar · tense", html)
        self.assertIn(f"Writing, {self.lesson.scheduled_for:%b %d}".replace(" 0", " "), html)
        self.assertIn("4×", html)
        self.assertIn('<i class="f"></i><i class="f"></i><i class=""></i>', html)
        self.assertIn(">Dismiss<", html)
        self.assertNotIn("mastered one", html)
        self.assertIn("Active · 1", html)
        self.assertIn("Due today · 1", html)
        self.assertIn("Mastered · 1", html)
        self.assertIn("Practice the 1 due", html)

    def test_filters(self):
        self.error("due", next_review_at=self.today)
        self.error("later", next_review_at=self.today + timedelta(days=3))
        self.error("vocab one", category="vocabulary", subcategory="false_friend")
        self.error("gone", status="dismissed")
        self.error("done", status="mastered")
        get = lambda f: self.client.get(f"/errors/?f={f}").content.decode()
        self.assertIn("<s>due</s>", get("due"))
        self.assertNotIn("<s>later</s>", get("due"))
        self.assertNotIn("<s>vocab one</s>", get("grammar"))
        self.assertIn("<s>vocab one</s>", get("vocabulary"))
        self.assertIn("<s>gone</s>", get("dismissed"))
        self.assertIn(">Restore<", get("dismissed"))
        self.assertIn("<s>done</s>", get("mastered"))
        self.assertIn("<s>due</s>", get("bogus"))  # unknown filter falls back to active

    def test_dismiss_and_restore(self):
        error = self.error("it depends of the client")
        response = self.client.post(f"/errors/{error.id}/status/", {"action": "dismiss"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertIn("dismissed", response.content.decode())
        error.refresh_from_db()
        self.assertEqual(error.status, "dismissed")
        self.assertEqual(ErrorItem.objects.due_for(self.learner).count(), 0)

        response = self.client.post(f"/errors/{error.id}/status/", {"action": "restore", "next": "/errors/?f=dismissed"})
        self.assertRedirects(response, "/errors/?f=dismissed", fetch_redirect_response=False)
        error.refresh_from_db()
        self.assertEqual((error.status, error.srs_box, error.next_review_at), ("active", 0, self.today))

    def test_cannot_touch_someone_elses_error(self):
        other = Learner.for_user(get_user_model().objects.create_user("other"))
        lesson = Lesson.objects.create(learner=other, track=self.lesson.track, skill="writing")
        foreign = ErrorItem.objects.create(learner=other, source_lesson=lesson, category="grammar", subcategory="tense", learner_produced="x", correction="y", explanation="z")
        self.assertEqual(self.client.post(f"/errors/{foreign.id}/status/", {"action": "dismiss"}).status_code, 404)
        self.assertNotIn("<s>x</s>", self.client.get("/errors/").content.decode())


class VocabularyPageTests(Base):
    def vocab(self, term, **kwargs):
        fields = {"status": "target", "definition_en": "def", "example_sentence": "ex"}
        fields.update(kwargs)
        return VocabItem.objects.create(learner=self.learner, term=term, **fields)

    def test_cards_by_status(self):
        self.vocab("trade-off", track=self.lesson.track)
        self.vocab("upfront", status="emerging", times_produced=2)
        self.vocab("cozy", status="acquired", times_produced=3)
        html = self.client.get("/vocabulary/").content.decode()
        self.assertIn("<b>trade-off</b>", html)
        self.assertIn("Target · Work", html)
        self.assertNotIn("<b>upfront</b>", html)
        self.assertIn("Target · 1", html)
        self.assertIn("Emerging · 1", html)
        self.assertIn("Acquired · 1", html)
        html = self.client.get("/vocabulary/?f=emerging").content.decode()
        self.assertIn("<b>upfront</b>", html)
        self.assertIn("Emerging · used 2×", html)
        html = self.client.get("/vocabulary/?f=acquired").content.decode()
        self.assertIn("<b>cozy</b>", html)
        self.assertIn("Practise again", html)

    def test_manual_moves(self):
        item = self.vocab("bottleneck")
        self.client.post(f"/vocabulary/{item.id}/status/", {"action": "acquired"})
        item.refresh_from_db()
        self.assertEqual(item.status, "acquired")
        self.client.post(f"/vocabulary/{item.id}/status/", {"action": "target"})
        item.refresh_from_db()
        self.assertEqual((item.status, item.srs_box, item.next_review_at), ("target", 0, self.today))
        response = self.client.post(f"/vocabulary/{item.id}/status/", {"action": "remove", "next": "/vocabulary/?f=target"})
        self.assertRedirects(response, "/vocabulary/?f=target", fetch_redirect_response=False)
        self.assertFalse(VocabItem.objects.filter(id=item.id).exists())

    def test_empty_states(self):
        html = self.client.get("/vocabulary/").content.decode()
        self.assertIn("Nothing to learn yet", html)


class VocabDefineTests(Base):
    def test_a_word_without_a_definition_can_be_defined_on_demand(self):
        from unittest import mock

        from ai import client as ai_client
        from core import views

        item = VocabItem.objects.create(learner=self.learner, term="rent")
        html = self.client.get("/vocabulary/").content.decode()
        self.assertIn("No definition yet", html)
        self.assertIn(f'hx-post="/vocabulary/{item.id}/define/"', html)

        definition = {"term": "rent", "definition_en": "the money you pay to live somewhere", "example": "The rent went up again.", "note_es": "'alquiler'."}
        with mock.patch.object(views.ai_vocab, "define", return_value=definition) as define:
            response = self.client.post(f"/vocabulary/{item.id}/define/", {"f": "target"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertIn("the money you pay to live somewhere", response.content.decode())
        define.assert_called_once()
        item.refresh_from_db()
        self.assertEqual(item.example_sentence, "The rent went up again.")

        # A second click does not call the model again.
        with mock.patch.object(views.ai_vocab, "define") as define:
            self.client.post(f"/vocabulary/{item.id}/define/", {"f": "target"}, HTTP_HX_REQUEST="true")
        define.assert_not_called()

    def test_lookup_failure_keeps_the_card(self):
        from unittest import mock

        from ai import client as ai_client
        from core import views

        item = VocabItem.objects.create(learner=self.learner, term="rq")
        with mock.patch.object(views.ai_vocab, "define", side_effect=ai_client.AIUnavailable("down")):
            response = self.client.post(f"/vocabulary/{item.id}/define/", {"f": "target"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Could not look it up", response.content.decode())
        self.assertIn("<b>rq</b>", response.content.decode())
