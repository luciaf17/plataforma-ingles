"""Tests for the article pool. Nothing here touches the network: the feed XML
and the article pages are handed in as strings."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from articles import fetch
from articles.models import Article, ArticleUse, pick_for
from learners.models import Learner

FEED = {"slug": "test", "name": "Test Blog", "url": "https://blog.test/feed", "full_text": False}

FEED_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Scaling the queue</title><link>https://blog.test/scaling-the-queue</link>
        <pubDate>Wed, 10 Sep 2026 10:00:00 GMT</pubDate></item>
  <item><title>Slides from the talk</title><link>https://blog.test/slides.pdf</link></item>
  <item><title>Discussion</title><link>https://news.ycombinator.com/item?id=1</link></item>
</channel></rss>"""

ENGLISH = " ".join(["The team decided to move the queue to a new cluster and we watched the latency drop."] * 20)
SPANISH = " ".join(["El equipo movio la cola a otro cluster y miramos como bajaba la latencia."] * 20)


def page(text):
    return f"<html><body><article><p>{text}</p></article></body></html>"


class LooksEnglishTests(TestCase):
    def test_it_separates_english_from_spanish(self):
        self.assertTrue(fetch.looks_english(ENGLISH))
        self.assertFalse(fetch.looks_english(SPANISH))

    def test_empty_text_is_not_english(self):
        self.assertFalse(fetch.looks_english("   "))


class ReadableUrlTests(TestCase):
    def test_it_rejects_what_is_never_an_article(self):
        for url in [
            "https://blog.test/slides.pdf",
            "https://news.ycombinator.com/item?id=1",
            "https://github.com/django/django",
            "https://www.youtube.com/watch?v=1",
            "ftp://blog.test/post",
            "",
        ]:
            self.assertFalse(fetch.is_readable_url(url), url)

    def test_it_accepts_a_normal_post(self):
        self.assertTrue(fetch.is_readable_url("https://blog.test/scaling-the-queue"))


class RefreshTests(TestCase):
    def download(self, url):
        return FEED_XML if url.endswith("/feed") else page(ENGLISH)

    def test_it_stores_articles_and_skips_the_rest(self):
        with mock.patch.object(fetch, "download", side_effect=self.download):
            result = fetch.refresh([FEED])
        self.assertEqual(result["stored"], 1)
        self.assertEqual(result["skipped"], 2)  # the PDF and the HN thread
        article = Article.objects.get()
        self.assertEqual(article.title, "Scaling the queue")
        self.assertEqual(article.source_name, "Test Blog")
        self.assertIn("latency", article.text)
        self.assertEqual(article.word_count, len(article.text.split()))
        self.assertEqual(article.published_at.year, 2026)

    def test_running_twice_stores_nothing_new(self):
        with mock.patch.object(fetch, "download", side_effect=self.download):
            fetch.refresh([FEED])
            second = fetch.refresh([FEED])
        self.assertEqual(second["stored"], 0)
        self.assertEqual(Article.objects.count(), 1)

    def test_a_dead_feed_is_survivable(self):
        with mock.patch.object(fetch, "download", return_value=None):
            result = fetch.refresh([FEED])
        self.assertEqual(result, {"stored": 0, "skipped": 0, "feeds": 0})
        self.assertEqual(Article.objects.count(), 0)

    def test_a_short_or_foreign_article_is_skipped(self):
        for body in ["Too short.", SPANISH]:
            with mock.patch.object(fetch, "download", side_effect=lambda url, body=body: FEED_XML if url.endswith("/feed") else page(body)):
                fetch.refresh([FEED])
            self.assertEqual(Article.objects.count(), 0, body[:20])


class PruneTests(TestCase):
    def article(self, **kwargs):
        return Article.objects.create(
            source="test", source_name="Test Blog", url=kwargs.pop("url", "https://blog.test/a"),
            title="A", text=ENGLISH, word_count=len(ENGLISH.split()), **kwargs,
        )

    def test_it_drops_old_unread_articles_only(self):
        old_unread = self.article(url="https://blog.test/old")
        old_read = self.article(url="https://blog.test/read")
        Article.objects.update(fetched_at=timezone.now() - timedelta(days=90))
        fresh = self.article(url="https://blog.test/fresh")

        user = get_user_model().objects.create_user("lu", password="pw")
        ArticleUse.objects.create(article=old_read, learner=Learner.for_user(user))

        self.assertEqual(fetch.prune(60), 1)
        self.assertFalse(Article.objects.filter(id=old_unread.id).exists())
        self.assertTrue(Article.objects.filter(id=old_read.id).exists())
        self.assertTrue(Article.objects.filter(id=fresh.id).exists())


class PickTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("lu", password="pw")
        self.learner = Learner.for_user(self.user)

    def article(self, url):
        return Article.objects.create(
            source="test", source_name="Test Blog", url=url, title=url.rsplit("/", 1)[-1],
            text=ENGLISH, word_count=len(ENGLISH.split()), published_at=timezone.now(),
        )

    def test_an_empty_pool_gives_nothing(self):
        self.assertIsNone(pick_for(self.learner))

    def test_it_never_returns_one_she_already_read(self):
        first = self.article("https://blog.test/one")
        ArticleUse.objects.create(article=first, learner=self.learner)
        self.assertIsNone(pick_for(self.learner))

        second = self.article("https://blog.test/two")
        self.assertEqual(pick_for(self.learner), second)

    def test_another_learner_can_still_read_it(self):
        article = self.article("https://blog.test/one")
        ArticleUse.objects.create(article=article, learner=self.learner)
        other = Learner.for_user(get_user_model().objects.create_user("eze", password="pw"))
        self.assertEqual(pick_for(other), article)


class ExcerptTests(TestCase):
    def test_it_takes_whole_paragraphs_up_to_the_target(self):
        text = "\n\n".join(["word " * 100, "word " * 100, "word " * 100])
        article = Article.objects.create(
            source="test", source_name="Test Blog", url="https://blog.test/a", title="A",
            text=text, word_count=300,
        )
        excerpt = article.excerpt(150)
        self.assertEqual(len(excerpt.split()), 200)  # stops after the paragraph that crosses 150
        self.assertEqual(len(article.excerpt(1000).split()), 300)  # never more than the article


class ParagraphTests(TestCase):
    def test_single_newlines_become_blank_lines(self):
        """The reading screen splits on blank lines; trafilatura does not use them."""
        self.assertEqual(fetch.paragraphs("one\ntwo\n\n  three  \n"), "one\n\ntwo\n\nthree")

    def test_extracted_text_is_split_into_paragraphs(self):
        html = "<html><body><article><p>First one here.</p><p>Second one here.</p></article></body></html>"
        self.assertEqual(fetch.extract(html, "https://blog.test/a").count("\n\n"), 1)
