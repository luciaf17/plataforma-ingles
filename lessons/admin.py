from django.contrib import admin

from .models import Checkpoint, ErrorItem, Lesson, LessonReport, Turn, VocabItem


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
    readonly_fields = ("created_at",)
    inlines = [TurnInline]


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
