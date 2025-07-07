from django.db import models
from .constants import DEFAULT_ELO_RATING
from .managers import AuthorManager, WorkManager, ComparisonManager

class Author(models.Model):
    name            = models.CharField(max_length=128, unique=True, db_index=True)
    birth_year      = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    death_year      = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    elo_rating      = models.FloatField(default=DEFAULT_ELO_RATING, db_index=True)  # starter ELO

    objects = AuthorManager()

    class Meta:
        ordering = ["-elo_rating", "name"]
        indexes = [
            models.Index(fields=['-elo_rating', 'name']),
            models.Index(fields=['birth_year', 'death_year']),
        ]

    def __str__(self):
        return str(self.name)
    
    def get_google_search_url(self):
        """Return a Google search URL for this author"""
        from urllib.parse import quote
        search_terms = [self.name]
        if self.birth_year:
            search_terms.append(str(self.birth_year))
        query = " ".join(search_terms)
        return f"https://www.google.com/search?q={quote(query)}&udm=14"


class Work(models.Model):
    title           = models.CharField(max_length=256, db_index=True)
    author          = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="works", db_index=True)
    publication_year= models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    form            = models.CharField(max_length=64, blank=True, help_text="e.g., novel, poem, play", db_index=True)
    elo_rating      = models.FloatField(default=DEFAULT_ELO_RATING, db_index=True)

    objects = WorkManager()

    class Meta:
        unique_together = [("title", "author")]
        ordering = ["-elo_rating", "title"]
        indexes = [
            models.Index(fields=['-elo_rating', 'title']),
            models.Index(fields=['author', '-elo_rating']),
            models.Index(fields=['form', '-elo_rating']),
            models.Index(fields=['publication_year']),
        ]

    def __str__(self):
        return f"{self.title} ({self.author.name})"
    
    def get_google_search_url(self):
        """Return a Google search URL for this work"""
        from urllib.parse import quote
        search_terms = [self.title, self.author.name]
        query = " ".join(search_terms)
        return f"https://www.google.com/search?q={quote(query)}&udm=14"


class Comparison(models.Model):
    """Track recent comparisons to avoid repetition"""
    content_type = models.CharField(max_length=10, choices=[('author', 'Author'), ('work', 'Work')], db_index=True)
    item_a_id = models.PositiveIntegerField(db_index=True)
    item_b_id = models.PositiveIntegerField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    objects = ComparisonManager()
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['content_type', 'item_a_id', 'item_b_id']),
            models.Index(fields=['content_type', 'created_at']),
            models.Index(fields=['-created_at']),
        ]
    
    @classmethod
    def was_recently_compared(cls, content_type, item_a_id, item_b_id, hours=24):
        """Check if two items were compared recently (within last N hours)"""
        return cls.objects.was_recently_compared(content_type, item_a_id, item_b_id, hours)
    
    @classmethod
    def record_comparison(cls, content_type, item_a_id, item_b_id):
        """Record a new comparison"""
        return cls.objects.record_comparison(content_type, item_a_id, item_b_id)
    
    @classmethod
    def cleanup_old_comparisons(cls, days=7):
        """Remove comparison records older than N days"""
        return cls.objects.cleanup_old(days)