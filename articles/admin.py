from django.contrib import admin

from .models import Article, ArticleUse


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("title", "source_name", "word_count", "published_at")
    list_filter = ("source",)
    search_fields = ("title", "url")


@admin.register(ArticleUse)
class ArticleUseAdmin(admin.ModelAdmin):
    list_display = ("article", "learner", "used_at")
