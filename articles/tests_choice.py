"""The reading of the day is chosen, not drawn. Nothing here touches the network."""

import json
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from ai import choose_article
from ai import client as ai_client
from articles.models import Article, ArticleUse, mark_rejected, taste
from learners.models import Learner

BODY = " ".join(["The team moved the queue and watched the latency drop."] * 40)


class ChoiceBase(TestCase):
    fixtures = ["seed"]

    def setUp(self):
        self.learner = Learner.for_user(get_user_model().objects.create_user("lu"))
        self.learner.goal_statement = "technical interviews"
        self.learner.save()

    def article(self, title, source="dev.to"):
        return Article.objects.create(
            source=source.replace(".", ""), source_name=source,
            url=f"https://blog.test/{title.replace(' ', '-').lower()}",
            title=title, text=BODY, word_count=len(BODY.split()), published_at=timezone.now(),
        )

    def answer(self, article_id, why="fits her work"):
        return SimpleNamespace(
            data={"article_id": article_id, "why": why},
            content="{}", model="gpt-4o-test", prompt_tokens=10, completion_tokens=5,
        )


class ChooseTests(ChoiceBase):
    def test_an_empty_pool_gives_nothing(self):
        self.assertIsNone(choose_article.choose(self.learner))

    def test_a_single_candidate_needs_no_model_call(self):
        only = self.article("The only one")
        with mock.patch.object(choose_article.client, "chat_json") as chat:
            self.assertEqual(choose_article.choose(self.learner), only)
        chat.assert_not_called()

    def test_it_returns_what_the_model_picked(self):
        self.article("A vendor announcement")
        wanted = self.article("How we debugged a production incident")
        with mock.patch.object(choose_article.client, "chat_json", return_value=self.answer(wanted.id)):
            self.assertEqual(choose_article.choose(self.learner), wanted)

    def test_her_taste_reaches_the_model(self):
        read = self.article("A distributed systems write-up")
        ArticleUse.objects.create(article=read, learner=self.learner)
        turned_down = self.article("Series B funding announced")
        mark_rejected(turned_down, self.learner)
        offered = self.article("How we cut our build time in half")
        self.article("Another one on offer")  # two candidates, or the choice is trivial

        with mock.patch.object(choose_article.client, "chat_json", return_value=self.answer(offered.id)) as chat:
            choose_article.choose(self.learner)
        context = json.loads(chat.call_args.args[0][1]["content"])
        self.assertEqual(context["taste"]["read"], ["A distributed systems write-up"])
        self.assertEqual(context["taste"]["rejected"], ["Series B funding announced"])
        self.assertEqual(context["goal"], "technical interviews")
        # Only what she has not seen is on offer.
        self.assertEqual(
            sorted(c["title"] for c in context["candidates"]),
            ["Another one on offer", "How we cut our build time in half"],
        )

    def test_the_system_prompt_puts_rejections_first(self):
        self.article("One")
        self.article("Two")
        with mock.patch.object(choose_article.client, "chat_json", return_value=self.answer(Article.objects.first().id)) as chat:
            choose_article.choose(self.learner)
        self.assertIn("strongest signal", chat.call_args.args[0][0]["content"])

    def test_a_model_failure_still_gives_her_something_to_read(self):
        self.article("One")
        self.article("Two")
        with mock.patch.object(choose_article.client, "chat_json", side_effect=ai_client.AIUnavailable("down")):
            self.assertIsNotNone(choose_article.choose(self.learner))

    def test_an_invented_id_is_not_trusted(self):
        self.article("One")
        self.article("Two")
        with mock.patch.object(choose_article.client, "chat_json", return_value=self.answer(9999)):
            chosen = choose_article.choose(self.learner)
        self.assertIn(chosen.title, ["One", "Two"])


class TasteTests(ChoiceBase):
    def test_rejecting_an_article_marks_the_row_she_already_had(self):
        article = self.article("Dull")
        ArticleUse.objects.create(article=article, learner=self.learner)
        mark_rejected(article, self.learner)
        self.assertEqual(ArticleUse.objects.count(), 1)
        self.assertTrue(ArticleUse.objects.get().rejected)
        self.assertEqual(taste(self.learner), {"read": [], "rejected": ["Dull"]})

    def test_a_rejected_article_never_comes_back(self):
        article = self.article("Dull")
        mark_rejected(article, self.learner)
        self.assertIsNone(choose_article.choose(self.learner))
