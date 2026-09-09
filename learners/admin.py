from django.contrib import admin

from .models import GrammarTopic, Learner, LearnerGrammarTopic, Topic, Track


class LearnerGrammarTopicInline(admin.TabularInline):
    model = LearnerGrammarTopic
    extra = 0
    fields = ("topic", "status", "times_targeted", "times_avoided")
    autocomplete_fields = ("topic",)


@admin.register(Learner)
class LearnerAdmin(admin.ModelAdmin):
    list_display = ("user", "cefr_speaking", "cefr_listening", "cefr_reading", "cefr_writing", "target_level", "placement_done")
    inlines = [LearnerGrammarTopicInline]


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("title", "track", "is_active")
    list_filter = ("track", "is_active")
    search_fields = ("title", "description")


@admin.register(GrammarTopic)
class GrammarTopicAdmin(admin.ModelAdmin):
    list_display = ("order", "title", "cefr_level", "slug")
    list_display_links = ("title",)
    list_filter = ("cefr_level",)
    search_fields = ("title", "slug", "summary_es")
    ordering = ("order",)


@admin.register(LearnerGrammarTopic)
class LearnerGrammarTopicAdmin(admin.ModelAdmin):
    list_display = ("learner", "topic", "status", "times_targeted", "times_avoided")
    list_filter = ("status", "topic__cefr_level")
    autocomplete_fields = ("topic",)
