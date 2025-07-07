"""
Custom managers and querysets for core models.
"""
import unicodedata
from django.db import models
from django.db.models import Q
from django.conf import settings


def normalize_search_text(text):
    """Normalize text for accent-insensitive search."""
    # Remove accents and convert to lowercase
    normalized = unicodedata.normalize('NFD', text.lower())
    # Remove combining characters (accents)
    return ''.join(char for char in normalized if unicodedata.category(char) != 'Mn')


class AuthorQuerySet(models.QuerySet):
    """Custom queryset for Author model."""
    
    def by_elo_rating(self):
        """Order authors by ELO rating (highest first)."""
        return self.order_by('-elo_rating', 'name')
    
    def search(self, query):
        """Search authors by name (accent-insensitive)."""
        if not query:
            return self.none()
        
        # For MySQL, use accent-insensitive collation
        if 'mysql' in settings.DATABASES['default']['ENGINE']:
            return self.extra(
                where=["name COLLATE utf8mb4_unicode_ci LIKE %s"],
                params=[f'%{query}%']
            )
        
        # For other databases, use Python normalization
        # Get all authors and filter in Python for accent-insensitive search
        normalized_query = normalize_search_text(query)
        matching_ids = []
        
        for author in self.all():
            normalized_name = normalize_search_text(author.name)
            if normalized_query in normalized_name:
                matching_ids.append(author.id)
        
        return self.filter(id__in=matching_ids)


class AuthorManager(models.Manager):
    """Custom manager for Author model."""
    
    def get_queryset(self):
        return AuthorQuerySet(self.model, using=self._db)
    
    def by_elo_rating(self):
        return self.get_queryset().by_elo_rating()
    
    def search(self, query):
        return self.get_queryset().search(query)


class WorkQuerySet(models.QuerySet):
    """Custom queryset for Work model."""
    
    def by_elo_rating(self):
        """Order works by ELO rating (highest first)."""
        return self.select_related('author').order_by('-elo_rating', 'title')
    
    def search(self, query):
        """Search works by title or author name (accent-insensitive)."""
        if not query:
            return self.none()
        
        # For MySQL, use accent-insensitive collation
        if 'mysql' in settings.DATABASES['default']['ENGINE']:
            return self.select_related('author').extra(
                where=["title COLLATE utf8mb4_unicode_ci LIKE %s OR author.name COLLATE utf8mb4_unicode_ci LIKE %s"],
                params=[f'%{query}%', f'%{query}%'],
                tables=['core_author']
            )
        
        # For other databases, use Python normalization
        normalized_query = normalize_search_text(query)
        matching_ids = []
        
        for work in self.select_related('author').all():
            normalized_title = normalize_search_text(work.title)
            normalized_author = normalize_search_text(work.author.name)
            if normalized_query in normalized_title or normalized_query in normalized_author:
                matching_ids.append(work.id)
        
        return self.filter(id__in=matching_ids)


class WorkManager(models.Manager):
    """Custom manager for Work model."""
    
    def get_queryset(self):
        return WorkQuerySet(self.model, using=self._db)
    
    def by_elo_rating(self):
        return self.get_queryset().by_elo_rating()
    
    def search(self, query):
        return self.get_queryset().search(query)


class ComparisonQuerySet(models.QuerySet):
    """Custom queryset for Comparison model."""
    
    def recent(self, hours=24):
        """Get recent comparisons within the last N hours."""
        from django.utils import timezone
        from datetime import timedelta
        since = timezone.now() - timedelta(hours=hours)
        return self.filter(created_at__gte=since)
    
    def for_items(self, content_type, item_a_id, item_b_id):
        """Get comparisons for specific item pair (in either order)."""
        return self.filter(
            content_type=content_type
        ).filter(
            Q(item_a_id=item_a_id, item_b_id=item_b_id) |
            Q(item_a_id=item_b_id, item_b_id=item_a_id)
        )
    
    def cleanup_old(self, days=7):
        """Remove comparisons older than N days."""
        from django.utils import timezone
        from datetime import timedelta
        cutoff = timezone.now() - timedelta(days=days)
        return self.filter(created_at__lt=cutoff).delete()


class ComparisonManager(models.Manager):
    """Custom manager for Comparison model."""
    
    def get_queryset(self):
        return ComparisonQuerySet(self.model, using=self._db)
    
    def recent(self, hours=24):
        return self.get_queryset().recent(hours)
    
    def for_items(self, content_type, item_a_id, item_b_id):
        return self.get_queryset().for_items(content_type, item_a_id, item_b_id)
    
    def was_recently_compared(self, content_type, item_a_id, item_b_id, hours=24):
        """Check if two items were compared recently."""
        return self.recent(hours).for_items(content_type, item_a_id, item_b_id).exists()
    
    def record_comparison(self, content_type, item_a_id, item_b_id):
        """Record a new comparison with items in consistent order."""
        if item_a_id > item_b_id:
            item_a_id, item_b_id = item_b_id, item_a_id
        return self.create(
            content_type=content_type,
            item_a_id=item_a_id,
            item_b_id=item_b_id
        )
    
    def cleanup_old(self, days=7):
        return self.get_queryset().cleanup_old(days)
