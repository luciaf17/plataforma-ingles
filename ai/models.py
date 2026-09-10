"""One row per OpenAI call, so the app can show what it costs to run.

Prices are estimates kept in settings (per million tokens or per minute);
the point is a visible running total, not an invoice.
"""

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.utils import timezone


class ApiCall(models.Model):
    class Kind(models.TextChoices):
        CHAT = "chat", "Chat"
        TRANSCRIBE = "transcribe", "Transcribe"
        SPEAK = "speak", "Speak"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    model = models.CharField(max_length=60)
    # Where it happened, when known: planner, analyzer, tutor, smoke...
    purpose = models.CharField(max_length=30, blank=True)
    lesson_id = models.IntegerField(null=True, blank=True)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    audio_seconds = models.FloatField(default=0)
    characters = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.kind} {self.model} ${self.cost_usd}"

    @staticmethod
    def estimate_cost(kind, model, *, prompt_tokens=0, completion_tokens=0, audio_seconds=0, characters=0):
        prices = settings.OPENAI_PRICES.get(model) or settings.OPENAI_PRICES.get(kind) or {}
        cost = 0.0
        cost += prompt_tokens / 1_000_000 * prices.get("input_per_m", 0)
        cost += completion_tokens / 1_000_000 * prices.get("output_per_m", 0)
        cost += audio_seconds / 60 * prices.get("per_minute", 0)
        cost += characters / 1_000 * prices.get("per_k_chars", 0)
        return round(cost, 6)

    @classmethod
    def record(cls, kind, model, **fields):
        cost = cls.estimate_cost(
            kind, model,
            prompt_tokens=fields.get("prompt_tokens", 0),
            completion_tokens=fields.get("completion_tokens", 0),
            audio_seconds=fields.get("audio_seconds", 0),
            characters=fields.get("characters", 0),
        )
        return cls.objects.create(kind=kind, model=model, cost_usd=cost, **fields)

    @classmethod
    def totals(cls):
        """Cost for today, the last 7 days, this month and all time, plus calls."""
        now = timezone.localtime()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        windows = {
            "today": today,
            "week": today - timedelta(days=6),
            "month": today.replace(day=1),
            "all": None,
        }
        out = {}
        for key, since in windows.items():
            qs = cls.objects.all() if since is None else cls.objects.filter(created_at__gte=since)
            agg = qs.aggregate(cost=Sum("cost_usd"), calls=models.Count("id"))
            out[key] = {"cost": float(agg["cost"] or 0), "calls": agg["calls"] or 0}
        by_kind = {
            row["kind"]: float(row["cost"] or 0)
            for row in cls.objects.values("kind").annotate(cost=Sum("cost_usd"))
        }
        out["by_kind"] = by_kind
        return out
