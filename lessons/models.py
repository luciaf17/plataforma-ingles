from django.db import models

from learners.models import GrammarTopic, Learner, Topic, Track

from . import srs
from .taxonomy import CATEGORY_CHOICES, SUBCATEGORY_CHOICES, validate_taxonomy


class Skill(models.TextChoices):
    SPEAKING = "speaking", "Speaking"
    LISTENING = "listening", "Listening"
    READING = "reading", "Reading"
    WRITING = "writing", "Writing"
    CHECKPOINT = "checkpoint", "Checkpoint"


class Confidence(models.TextChoices):
    HIGH = "high", "High"
    MEDIUM = "medium", "Medium"
    LOW = "low", "Low"


class Lesson(models.Model):
    """One class. The plan is generated once and never regenerated (spec 4.2)."""

    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        ANALYZED = "analyzed", "Analyzed"

    learner = models.ForeignKey(Learner, on_delete=models.CASCADE, related_name="lessons")
    track = models.ForeignKey(Track, on_delete=models.PROTECT, related_name="lessons")
    topic = models.ForeignKey(Topic, null=True, blank=True, on_delete=models.SET_NULL, related_name="lessons")
    grammar_topic = models.ForeignKey(
        GrammarTopic, null=True, blank=True, on_delete=models.SET_NULL, related_name="lessons",
        help_text="The mini-lesson point of the day",
    )
    skill = models.CharField(max_length=10, choices=Skill.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PLANNED)
    plan = models.JSONField(default=dict, blank=True)
    # The day this lesson was prepared for; lets the dashboard find "today's lesson".
    scheduled_for = models.DateField(default=srs.today, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-scheduled_for", "-created_at"]
        indexes = [models.Index(fields=["learner", "-started_at"], name="lesson_learner_started_idx")]

    def __str__(self):
        title = (self.plan or {}).get("title") or (self.topic.title if self.topic else self.get_skill_display())
        return f"{self.scheduled_for} · {self.skill} · {title}"

    @property
    def title(self):
        return (self.plan or {}).get("title") or (self.topic.title if self.topic else self.get_skill_display())


class Turn(models.Model):
    """One exchange inside a lesson: what the tutor said or what the learner produced."""

    class Role(models.TextChoices):
        TUTOR = "tutor", "Tutor"
        LEARNER = "learner", "Learner"

    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name="turns")
    role = models.CharField(max_length=8, choices=Role.choices)
    text = models.TextField()
    audio_file = models.FileField(upload_to="turns/%Y/%m/", null=True, blank=True)
    audio_duration_ms = models.PositiveIntegerField(null=True, blank=True)
    word_count = models.PositiveIntegerField(default=0)
    # Lesson phase this turn happened in (warm_up, mini_lesson, practice, drill, wrap_up).
    phase = models.CharField(max_length=12, blank=True)
    sequence = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        unique_together = [("lesson", "sequence")]

    def __str__(self):
        return f"{self.lesson_id}#{self.sequence} {self.role}: {self.text[:40]}"

    def save(self, *args, **kwargs):
        if not self.word_count and self.text:
            self.word_count = len(self.text.split())
        super().save(*args, **kwargs)


class DueQuerySet(models.QuerySet):
    """Shared by ErrorItem and VocabItem: what should be reviewed today."""

    def due_for(self, learner, on=None):
        on = on or srs.today()
        return self.filter(learner=learner, next_review_at__lte=on).exclude(status__in=self.model.NOT_DUE_STATUSES)


class ErrorItem(models.Model):
    """The heart of the system: one recurring mistake and how close it is to being gone."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        MASTERED = "mastered", "Mastered"
        DISMISSED = "dismissed", "Dismissed"

    NOT_DUE_STATUSES = (Status.MASTERED, Status.DISMISSED)

    learner = models.ForeignKey(Learner, on_delete=models.CASCADE, related_name="errors")
    category = models.CharField(max_length=16, choices=CATEGORY_CHOICES)
    subcategory = models.CharField(max_length=20, choices=SUBCATEGORY_CHOICES)
    learner_produced = models.TextField(help_text="Exactly what the learner said or wrote")
    correction = models.TextField()
    explanation = models.TextField(help_text="In Spanish, short, contrastive with Spanish")
    source_turn = models.ForeignKey(Turn, null=True, blank=True, on_delete=models.SET_NULL, related_name="errors")
    source_lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name="errors")
    confidence = models.CharField(max_length=6, choices=Confidence.choices, default=Confidence.MEDIUM)
    occurrences = models.PositiveIntegerField(default=1)
    srs_box = models.PositiveSmallIntegerField(default=0)
    next_review_at = models.DateField(default=srs.today)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now_add=True)

    objects = DueQuerySet.as_manager()

    class Meta:
        # Most repeated first, then the ones that have waited longest.
        ordering = ["-occurrences", "next_review_at", "-last_seen_at"]
        indexes = [models.Index(fields=["learner", "status", "next_review_at"], name="error_due_idx")]

    def __str__(self):
        return f"{self.learner_produced} → {self.correction}"

    def clean(self):
        validate_taxonomy(self.category, self.subcategory)

    def save(self, *args, **kwargs):
        validate_taxonomy(self.category, self.subcategory)
        super().save(*args, **kwargs)

    @property
    def is_due(self):
        return self.status == self.Status.ACTIVE and self.next_review_at <= srs.today()


class VocabItem(models.Model):
    """A word the learner should acquire, is starting to use, or already owns."""

    class Status(models.TextChoices):
        TARGET = "target", "Target"
        EMERGING = "emerging", "Emerging"
        ACQUIRED = "acquired", "Acquired"

    NOT_DUE_STATUSES = (Status.ACQUIRED,)

    learner = models.ForeignKey(Learner, on_delete=models.CASCADE, related_name="vocab")
    term = models.CharField(max_length=80)
    definition_en = models.TextField(blank=True)
    example_sentence = models.TextField(blank=True)
    track = models.ForeignKey(Track, null=True, blank=True, on_delete=models.SET_NULL, related_name="vocab")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.TARGET)
    srs_box = models.PositiveSmallIntegerField(default=0)
    next_review_at = models.DateField(default=srs.today)
    times_produced = models.PositiveIntegerField(default=0, help_text="Spontaneous uses by the learner")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = DueQuerySet.as_manager()

    class Meta:
        ordering = ["next_review_at", "term"]
        unique_together = [("learner", "term")]
        indexes = [models.Index(fields=["learner", "status", "next_review_at"], name="vocab_due_idx")]

    def __str__(self):
        return self.term

    def save(self, *args, **kwargs):
        self.term = self.term.strip().lower()
        super().save(*args, **kwargs)


class LessonReport(models.Model):
    """What the post-lesson analyzer said. `raw_analysis` is kept whole for reprocessing."""

    lesson = models.OneToOneField(Lesson, on_delete=models.CASCADE, related_name="report")
    summary_es = models.TextField()
    strengths = models.JSONField(default=list, blank=True)
    focus_next = models.JSONField(default=list, blank=True)
    fluency_wpm = models.FloatField(null=True, blank=True)
    filler_ratio = models.FloatField(null=True, blank=True)
    new_errors_count = models.PositiveIntegerField(default=0)
    recycled_errors_count = models.PositiveIntegerField(default=0)
    errors_avoided_count = models.PositiveIntegerField(default=0)
    raw_analysis = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Report for {self.lesson}"


class Checkpoint(models.Model):
    """Monthly level assessment across the four skills (spec 7c)."""

    learner = models.ForeignKey(Learner, on_delete=models.CASCADE, related_name="checkpoints")
    lesson = models.OneToOneField(Lesson, null=True, blank=True, on_delete=models.SET_NULL, related_name="checkpoint")
    taken_at = models.DateTimeField(auto_now_add=True)
    # Per skill: {"estimate": "B1+", "evidence": [...], "gaps_to_target": [...]}
    results = models.JSONField(default=dict, blank=True)
    report_es = models.TextField(blank=True)

    class Meta:
        ordering = ["-taken_at"]

    def __str__(self):
        return f"Checkpoint {self.taken_at:%Y-%m-%d} · {self.learner}"

    def estimate_for(self, skill):
        return (self.results.get(skill) or {}).get("estimate", "")


class ProgressReview(models.Model):
    """A coach's read of the last few lessons (Progress screen, "Review my recent lessons").

    Generated on demand and kept, so opening Progress does not spend a call.
    """

    learner = models.ForeignKey(Learner, on_delete=models.CASCADE, related_name="reviews")
    summary_es = models.TextField()
    improving = models.JSONField(default=list, blank=True)
    stuck = models.JSONField(default=list, blank=True)
    focus = models.JSONField(default=list, blank=True)
    lessons_covered = models.PositiveIntegerField(default=0)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Review for {self.learner} · {self.created_at:%Y-%m-%d}"
