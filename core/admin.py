from django.contrib import admin
from .models import Author, Work, Comparison

@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    list_display = ("name", "elo_rating", "birth_year", "death_year", "has_wikipedia_url")
    search_fields = ("name",)
    fields = ("name", "birth_year", "death_year", "wikipedia_url", "elo_rating")
    
    def has_wikipedia_url(self, obj):
        return bool(obj.wikipedia_url)
    has_wikipedia_url.boolean = True
    has_wikipedia_url.short_description = "Has Wikipedia URL"

@admin.register(Work)
class WorkAdmin(admin.ModelAdmin):
    list_display = ("title", "author", "elo_rating", "publication_year", "form", "has_wikipedia_url")
    list_filter = ("form", "author")
    search_fields = ("title",)
    fields = ("title", "author", "publication_year", "form", "wikipedia_url", "elo_rating")
    
    def has_wikipedia_url(self, obj):
        return bool(obj.wikipedia_url)
    has_wikipedia_url.boolean = True
    has_wikipedia_url.short_description = "Has Wikipedia URL"

@admin.register(Comparison)
class ComparisonAdmin(admin.ModelAdmin):
    list_display = ("content_type", "item_a_id", "item_b_id", "created_at")
    list_filter = ("content_type", "created_at")
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)
    
    def has_add_permission(self, request):
        return False  # Don't allow manual creation