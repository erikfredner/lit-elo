"""
Custom managers and querysets for core models.
"""
from django.db import models
from django.db.models import Q


class AuthorQuerySet(models.QuerySet):
    """Custom queryset for Author model."""
    
    def by_elo_rating(self):
        """Order authors by ELO rating (highest first)."""
        return self.order_by('-elo_rating', 'name')
    
    def search(self, query):
        """Search authors by name."""
        return self.filter(Q(name__icontains=query))


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
        """Search works by title or author name."""
        return self.select_related('author').filter(
            Q(title__icontains=query) | Q(author__name__icontains=query)
        )


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
