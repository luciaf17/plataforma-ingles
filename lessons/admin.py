import json

from django.contrib import admin
from django.utils.html import format_html

from .models import Checkpoint, ErrorItem, Lesson, LessonReport, ProgressReview, Turn, VocabItem


class TurnInline(admin.TabularInline):
    model = Turn
    extra = 0
    fields = ("sequence", "role", "phase", "text", "word_count", "audio_duration_ms")
    readonly_fields = ("word_count",)
    ordering = ("sequence",)


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ("scheduled_for", "learner", "skill", "track", "topic", "grammar_topic", "status")
    list_filter = ("skill", "status", "track")
    date_hierarchy = "scheduled_for"
    autocomplete_fields = ("topic", "grammar_topic")
    readonly_fields = ("created_at", "plan_pretty")
    fields = (
        ("learner", "scheduled_for", "status"),
        ("skill", "track", "topic", "grammar_topic"),
        ("started_at", "completed_at", "duration_seconds"),
        "plan_pretty",
        "plan",
        "created_at",
    )
    inlines = [TurnInline]

    @admin.display(description="Plan (read-only)")
    def plan_pretty(self, obj):
        if not obj.plan:
            return "—"
        phases = "".join(
            f"<li><b>{p['key']}</b> · {p['minutes']} min · {p.get('title', '')}<br>"
            f"<i>{p.get('tutor_goal', '')}</i><ol>" + "".join(f"<li>{q}</li>" for q in p.get("prompts", [])) + "</ol></li>"
            for p in obj.plan.get("phases", [])
        )
        targets = "".join(
            f"<li>#{t['id']} <s>{t['learner_produced']}</s> → {t['correction']}<br><i>{t.get('how_to_elicit', '')}</i></li>"
            for t in obj.plan.get("targeted_errors", [])
        )
        return format_html(
            "<div style='max-width:900px;line-height:1.5'><h3 style='margin:0 0 6px'>{}</h3><p>{}</p>"
            "<p><b>Tutor role:</b> {}</p><h4>Phases</h4><ul>{}</ul><h4>Targeted errors</h4><ul>{}</ul>"
            "<details><summary>Raw JSON</summary><pre style='white-space:pre-wrap'>{}</pre></details></div>",
            obj.plan.get("title", ""), obj.plan.get("summary", ""), obj.plan.get("tutor_role", ""),
            format_html(phases), format_html(targets), json.dumps(obj.plan, ensure_ascii=False, indent=2),
        )


@admin.register(Turn)
class TurnAdmin(admin.ModelAdmin):
    list_display = ("lesson", "sequence", "role", "phase", "word_count", "audio_duration_ms")
    list_filter = ("role", "phase")
    search_fields = ("text",)


@admin.register(ErrorItem)
class ErrorItemAdmin(admin.ModelAdmin):
    list_display = ("learner_produced", "correction", "category", "subcategory", "occurrences", "srs_box", "next_review_at", "status", "confidence")
    list_filter = ("status", "category", "subcategory", "confidence")
    search_fields = ("learner_produced", "correction", "explanation")
    readonly_fields = ("created_at", "last_seen_at")


@admin.register(VocabItem)
class VocabItemAdmin(admin.ModelAdmin):
    list_display = ("term", "status", "track", "srs_box", "next_review_at", "times_produced")
    list_filter = ("status", "track")
    search_fields = ("term", "definition_en")


@admin.register(LessonReport)
class LessonReportAdmin(admin.ModelAdmin):
    list_display = ("lesson", "new_errors_count", "recycled_errors_count", "errors_avoided_count", "fluency_wpm", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Checkpoint)
class CheckpointAdmin(admin.ModelAdmin):
    list_display = ("taken_at", "learner", "lesson")
    readonly_fields = ("taken_at",)


@admin.register(ProgressReview)
class ProgressReviewAdmin(admin.ModelAdmin):
    list_display = ("created_at", "learner", "lessons_covered", "period_start", "period_end")
    readonly_fields = ("created_at",)
