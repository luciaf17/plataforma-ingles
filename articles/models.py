"""Real tech articles pulled from public feeds, used as reading material.

The planner adapts nothing here: a reading lesson quotes the article as it was
published and builds the glossary and questions on top of it.
"""

import random

from django.db import models


class ArticleQuerySet(models.QuerySet):
    def unused_by(self, learner):
        """Articles this learner has not read yet, newest first."""
        return self.exclude(uses__learner=learner).order_by("-published_at", "-fetched_at")


class Article(models.Model):
    source = models.SlugField(max_length=40)
    source_name = models.CharField(max_length=80)
    url = models.URLField(max_length=500, unique=True)
    title = models.CharField(max_length=300)
    author = models.CharField(max_length=160, blank=True)
    text = models.TextField()
    word_count = models.PositiveIntegerField()
    published_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    objects = ArticleQuerySet.as_manager()

    class Meta:
        ordering = ["-published_at", "-fetched_at"]
        indexes = [models.Index(fields=["-published_at"])]

    def __str__(self):
        return f"{self.title} ({self.source})"

    def excerpt(self, words):
        """Whole paragraphs from the top, up to roughly `words` words.

        Reading tasks want 180-400 words and these articles run to thousands,
        so the lesson uses the opening passage rather than the whole piece.
        """
        taken, total = [], 0
        for paragraph in self.text.split("\n\n"):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            taken.append(paragraph)
            total += len(paragraph.split())
            if total >= words:
                break
        return "\n\n".join(taken)


class ArticleUse(models.Model):
    """One row per learner per article, so two learners can read the same piece."""

    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="uses")
    learner = models.ForeignKey("learners.Learner", on_delete=models.CASCADE, related_name="article_uses")
    lesson = models.ForeignKey("lessons.Lesson", on_delete=models.SET_NULL, null=True, blank=True, related_name="article_uses")
    used_at = models.DateTimeField(auto_now_add=True)
    rejected = models.BooleanField(default=False, help_text="She asked for a different one instead of reading it")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["article", "learner"], name="unique_article_per_learner")]

    def __str__(self):
        return f"{self.learner} read {self.article}"


# Pick among the newest few rather than always the very latest, so one busy
# feed does not supply every reading lesson in a row.
PICK_POOL = 10


def pick_for(learner, rng=random):
    candidates = list(Article.objects.unused_by(learner)[:PICK_POOL])
    return rng.choice(candidates) if candidates else None


def mark_used(article, learner, lesson=None):
    ArticleUse.objects.get_or_create(article=article, learner=learner, defaults={"lesson": lesson})


def mark_rejected(article, learner):
    """She asked for something else. Worth more than the fact she saw it."""
    ArticleUse.objects.update_or_create(article=article, learner=learner, defaults={"rejected": True})


def taste(learner, limit=8):
    """What she has read and what she turned down, newest first.

    Titles only: enough for a model to tell "distributed systems write-up"
    from "funding round announcement", and cheap to send.
    """
    rows = (
        ArticleUse.objects.filter(learner=learner)
        .select_related("article")
        .order_by("-used_at")[: limit * 2]
    )
    kept = [use.article.title for use in rows if not use.rejected][:limit]
    rejected = [use.article.title for use in rows if use.rejected][:limit]
    return {"read": kept, "rejected": rejected}
