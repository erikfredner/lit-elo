"""
Custom managers and querysets for core models.
"""
import unicodedata
from django.db import models

from django.conf import settings


def normalize_search_text(text):
    """Normalize text for accent-insensitive search."""
    # Remove accents and convert to lowercase
    normalized = unicodedata.normalize('NFD', text.lower())
    # Remove combining characters (accents)
    return ''.join(char for char in normalized if unicodedata.category(char) != 'Mn')


class AuthorQuerySet(models.QuerySet):
    """Custom queryset for Author model."""
    
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
    
    def search(self, query):
        return self.get_queryset().search(query)


class WorkQuerySet(models.QuerySet):
    """Custom queryset for Work model."""
    
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
    
    def search(self, query):
        return self.get_queryset().search(query)
