"""
Tests for the refactored core functionality.
"""
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User

from .models import Author, Work, Comparison
from .business import PairingService, ComparisonService, SearchService
from .constants import DEFAULT_ELO_RATING


class ModelTests(TestCase):
    """Test custom managers and model methods."""
    
    def setUp(self):
        self.author1 = Author.objects.create(name="William Shakespeare", elo_rating=1300)
        self.author2 = Author.objects.create(name="Charles Dickens", elo_rating=1250)
        self.work1 = Work.objects.create(title="Hamlet", author=self.author1, elo_rating=1350)
        self.work2 = Work.objects.create(title="Macbeth", author=self.author1, elo_rating=1280)
    
    def test_author_manager_by_elo_rating(self):
        """Test author ordering by ELO rating."""
        authors = list(Author.objects.by_elo_rating())
        self.assertEqual(authors[0], self.author1)  # Higher rating first
        self.assertEqual(authors[1], self.author2)
    
    def test_author_search(self):
        """Test author search functionality."""
        results = Author.objects.search("shakespeare")
        self.assertIn(self.author1, results)
        self.assertNotIn(self.author2, results)
    
    def test_work_manager_by_elo_rating(self):
        """Test work ordering by ELO rating."""
        works = list(Work.objects.by_elo_rating())
        self.assertEqual(works[0], self.work1)  # Higher rating first
        self.assertEqual(works[1], self.work2)


class BusinessLogicTests(TestCase):
    """Test business logic services."""
    
    def setUp(self):
        self.author1 = Author.objects.create(name="Author 1", elo_rating=1200)
        self.author2 = Author.objects.create(name="Author 2", elo_rating=1200)
        self.author3 = Author.objects.create(name="Author 3", elo_rating=1200)
    
    def test_pairing_service(self):
        """Test ELO-based pairing."""
        item_a, item_b = PairingService.get_two_by_elo(Author)
        self.assertNotEqual(item_a.id, item_b.id)
        self.assertIn(item_a, [self.author1, self.author2, self.author3])
        self.assertIn(item_b, [self.author1, self.author2, self.author3])
    
    def test_comparison_service(self):
        """Test comparison recording."""
        original_rating_a = self.author1.elo_rating
        original_rating_b = self.author2.elo_rating
        
        ComparisonService.record_comparison(self.author1, self.author2, 'A')
        
        # Refresh from database
        self.author1.refresh_from_db()
        self.author2.refresh_from_db()
        
        # Winner should gain rating, loser should lose rating
        self.assertGreater(self.author1.elo_rating, original_rating_a)
        self.assertLess(self.author2.elo_rating, original_rating_b)
    
    def test_search_service(self):
        """Test search with context."""
        results = SearchService.search_with_context("Author", "authors")
        self.assertEqual(len(results), 3)  # Should find all three authors
        
        # Each result should have required fields
        for result in results:
            self.assertIn('item', result)
            self.assertIn('rank', result)
            self.assertIn('context', result)


class ViewTests(TestCase):
    """Test view functionality."""
    
    def setUp(self):
        self.client = Client()
        self.author1 = Author.objects.create(name="Test Author 1")
        self.author2 = Author.objects.create(name="Test Author 2")
    
    def test_home_redirect(self):
        """Test home page redirects to author comparison."""
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.endswith('/compare/authors/'))
    
    def test_compare_view(self):
        """Test comparison view."""
        response = self.client.get(reverse('core:compare', kwargs={'mode': 'authors'}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Author')
    
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


class ComparisonModelTests(TestCase):
    """Test comparison tracking."""
    
    def setUp(self):
        self.author1 = Author.objects.create(name="Author 1")
        self.author2 = Author.objects.create(name="Author 2")
    
    def test_comparison_recording(self):
        """Test comparison gets recorded."""
        initial_count = Comparison.objects.count()
        
        Comparison.record_comparison('author', self.author1.id, self.author2.id)
        
        self.assertEqual(Comparison.objects.count(), initial_count + 1)
    
    def test_recent_comparison_check(self):
        """Test recent comparison detection."""
        # Record a comparison
        Comparison.record_comparison('author', self.author1.id, self.author2.id)
        
        # Should detect recent comparison
        self.assertTrue(
            Comparison.was_recently_compared('author', self.author1.id, self.author2.id, hours=1)
        )
        
        # Should detect in reverse order too
        self.assertTrue(
            Comparison.was_recently_compared('author', self.author2.id, self.author1.id, hours=1)
        )
