from django.conf import settings
from django.db import models


class CEFR(models.TextChoices):
    A1 = "A1", "A1"
    A2 = "A2", "A2"
    B1 = "B1", "B1"
    B2 = "B2", "B2"
    C1 = "C1", "C1"
    C2 = "C2", "C2"


# Order used for "distance to target" computations across the project.
CEFR_ORDER = [level for level, _ in CEFR.choices]

SKILLS = ("speaking", "listening", "reading", "writing")


class Learner(models.Model):
    """The student file's owner. One per user; single user for now."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="learner")
    cefr_speaking = models.CharField(max_length=2, choices=CEFR.choices, blank=True)
    cefr_listening = models.CharField(max_length=2, choices=CEFR.choices, blank=True)
    cefr_reading = models.CharField(max_length=2, choices=CEFR.choices, blank=True)
    cefr_writing = models.CharField(max_length=2, choices=CEFR.choices, blank=True)
    target_level = models.CharField(max_length=2, choices=CEFR.choices, default=CEFR.B2)
    placement_done = models.BooleanField(default=False)
    placement_notes = models.TextField(blank=True)
    goal_statement = models.TextField(blank=True, help_text="e.g. technical interviews, daily standups")
    native_language = models.CharField(max_length=8, default="es")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.user.get_username()

    def cefr_for(self, skill):
        """Current level for a skill, or the target level minus one band if unknown."""
        if skill not in SKILLS:
            raise ValueError(f"Unknown skill: {skill}")
        level = getattr(self, f"cefr_{skill}")
        if level:
            return level
        index = max(CEFR_ORDER.index(self.target_level) - 1, 0)
        return CEFR_ORDER[index]

    @classmethod
    def for_user(cls, user):
        learner, _ = cls.objects.get_or_create(user=user)
        return learner


class Track(models.Model):
    """'work' or 'general': the two conversational worlds lessons alternate between."""

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=60)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class TopicQuerySet(models.QuerySet):
    def visible_to(self, learner):
        """The shared, seeded topics plus the ones written for this learner."""
        return self.filter(is_active=True).filter(models.Q(learner=None) | models.Q(learner=learner))


class Topic(models.Model):
    """A lesson subject inside a track, with the vocabulary it should surface.

    Seeded topics are shared; a topic with a `learner` was proposed for her out
    of her own file and is hers alone.
    """

    track = models.ForeignKey(Track, on_delete=models.CASCADE, related_name="topics")
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    seed_vocabulary = models.JSONField(default=list, blank=True, help_text="List of target terms")
    is_active = models.BooleanField(default=True)
    # A seeded topic (null learner) is offered to everybody; a proposed one was
    # written for one learner out of her own file and belongs to her alone.
    learner = models.ForeignKey(
        "Learner", null=True, blank=True, on_delete=models.CASCADE, related_name="proposed_topics"
    )
    proposed_reason = models.TextField(blank=True, help_text="Why this was proposed, in Spanish, for her to read")
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    objects = TopicQuerySet.as_manager()

    class Meta:
        ordering = ["track", "title"]

    @property
    def is_proposed(self):
        return self.learner_id is not None

    def __str__(self):
        return self.title


class GrammarTopic(models.Model):
    """One point of the A2 -> B2 syllabus (spec 7b). Seeded, never generated."""

    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=160)
    cefr_level = models.CharField(max_length=2, choices=CEFR.choices)
    order = models.PositiveIntegerField(unique=True)
    summary_es = models.TextField(help_text="Contrastive ES -> EN explanation, in Spanish")
    examples = models.JSONField(default=list, blank=True, help_text="[{wrong, right, note_es}]")
    related_subcategories = models.JSONField(
        default=list, blank=True, help_text="ErrorItem subcategories this topic addresses"
    )

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.cefr_level} · {self.title}"


class LearnerGrammarTopic(models.Model):
    """Where a learner stands on one syllabus topic."""

    class Status(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        INTRODUCED = "introduced", "Introduced"
        PRACTICING = "practicing", "Practicing"
        MASTERED = "mastered", "Mastered"

    learner = models.ForeignKey(Learner, on_delete=models.CASCADE, related_name="grammar_progress")
    topic = models.ForeignKey(GrammarTopic, on_delete=models.CASCADE, related_name="learner_progress")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NOT_STARTED)
    introduced_in = models.ForeignKey(
        "lessons.Lesson", null=True, blank=True, on_delete=models.SET_NULL, related_name="introduced_grammar_topics"
    )
    times_targeted = models.PositiveIntegerField(default=0)
    times_avoided = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("learner", "topic")]
        ordering = ["topic__order"]

    def __str__(self):
        return f"{self.learner} · {self.topic.slug} · {self.status}"
