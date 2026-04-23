"""
Tests for the refactored core functionality.
"""
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User

from .models import Author, Work
from .constants import DEFAULT_ELO_RATING


class ModelTests(TestCase):
    """Test custom managers and model methods."""
    
    def setUp(self):
        self.author1 = Author.objects.create(name="William Shakespeare", elo_rating=1300)
        self.author2 = Author.objects.create(name="Charles Dickens", elo_rating=1250)
        self.work1 = Work.objects.create(title="Hamlet", author=self.author1, elo_rating=1350)
        self.work2 = Work.objects.create(title="Macbeth", author=self.author1, elo_rating=1280)
    
    def test_author_search(self):
        """Test author search functionality."""
        results = Author.objects.search("shakespeare")
        self.assertIn(self.author1, results)
        self.assertNotIn(self.author2, results)
    

class ViewTests(TestCase):
    """Test view functionality."""
    
    def setUp(self):
        self.client = Client()
        self.author1 = Author.objects.create(name="Test Author 1")
        self.author2 = Author.objects.create(name="Test Author 2")
    
    def test_home_redirect(self):
        """Test home page redirects to author leaderboard."""
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.endswith('/leaderboard/authors/'))

    def test_author_leaderboard(self):
        """Test author leaderboard."""
        response = self.client.get(reverse('core:authors_lb'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Author 1')
        self.assertContains(response, 'Test Author 2')
    
    def test_search_view(self):
        """Test search functionality."""
        response = self.client.get(reverse('core:search') + '?q=Test&mode=authors')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Author')


