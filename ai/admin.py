from django.contrib import admin

from .models import ApiCall


@admin.register(ApiCall)
class ApiCallAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "model", "purpose", "lesson_id", "prompt_tokens", "completion_tokens", "audio_seconds", "characters", "latency_ms", "cost_usd")
    list_filter = ("kind", "model", "purpose")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in ApiCall._meta.fields]
