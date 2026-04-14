from django.test import TestCase, Client
from django.urls import reverse
from .models import Author, Work
from .constants import DEFAULT_ELO_RATING

class VotingTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.author1 = Author.objects.create(name="William Shakespeare", birth_year=1564, death_year=1616)
        self.author2 = Author.objects.create(name="Jane Austen", birth_year=1775, death_year=1817)

        self.work1 = Work.objects.create(title="Hamlet", author=self.author1, publication_year=1600)
        self.work2 = Work.objects.create(title="Pride and Prejudice", author=self.author2, publication_year=1813)

    def test_invalid_winner_parameter(self):
        """Test that invalid winner parameters are ignored"""
        url = reverse('core:compare', kwargs={'mode': 'authors'})
        response = self.client.get(url, {
            'winner': 'INVALID',
            'item_a_id': self.author1.id,
            'item_b_id': self.author2.id
        })
        
        # Should redirect (since invalid votes are ignored)
        self.assertEqual(response.status_code, 302)
        
        # Ratings should remain unchanged
        self.author1.refresh_from_db()
        self.author2.refresh_from_db()
        self.assertEqual(self.author1.elo_rating, DEFAULT_ELO_RATING)
        self.assertEqual(self.author2.elo_rating, DEFAULT_ELO_RATING)

class AccentInsensitiveSearchTestCase(TestCase):
    def setUp(self):
        self.author_with_accents = Author.objects.create(name="Gabriel García Márquez", birth_year=1927, death_year=2014)
        self.work_with_accents = Work.objects.create(title="Cien años de soledad", author=self.author_with_accents, publication_year=1967)
        
    def test_author_search_accent_insensitive(self):
        """Test that searching for authors works without accents"""
        # Search without accents should find author with accents
        results = Author.objects.search("garcia marquez")
        self.assertIn(self.author_with_accents, results)
        
        results = Author.objects.search("marquez")
        self.assertIn(self.author_with_accents, results)
        
        results = Author.objects.search("gabriel")
        self.assertIn(self.author_with_accents, results)
        
    def test_work_search_accent_insensitive(self):
        """Test that searching for works works without accents"""
        # Search by title without accents
        results = Work.objects.search("cien anos")
        self.assertIn(self.work_with_accents, results)
        
        # Search by author name without accents
        results = Work.objects.search("garcia")
        self.assertIn(self.work_with_accents, results)
        
    def test_search_view_accent_insensitive(self):
        """Test that the search view works with accent-insensitive queries"""
        url = reverse('core:search')
        
        # Test author search
        response = self.client.get(url, {'q': 'marquez', 'mode': 'authors'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['results']), 1)
        self.assertEqual(response.context['results'][0]['item'], self.author_with_accents)
        
        # Test work search 
        response = self.client.get(url, {'q': 'cien anos', 'mode': 'works'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['results']), 1)
        self.assertEqual(response.context['results'][0]['item'], self.work_with_accents)

class GoogleSearchURLTestCase(TestCase):
    def setUp(self):
        self.author = Author.objects.create(name="Gabriel García Márquez", birth_year=1927, death_year=2014)
        self.work = Work.objects.create(title="One Hundred Years of Solitude", author=self.author, publication_year=1967)
        
    def test_author_google_search_url(self):
        """Test that author Google search URL is generated correctly"""
        url = self.author.get_google_search_url()
        
        # Should contain author name and birth year with spaces encoded as %20
        self.assertIn("Gabriel%20Garc%C3%ADa%20M%C3%A1rquez", url)
        self.assertIn("1927", url)
        self.assertIn("google.com/search", url)
        self.assertIn("udm=14", url)
        
    def test_author_google_search_url_no_birth_year(self):
        """Test that author Google search URL works without birth year"""
        author_no_year = Author.objects.create(name="Anonymous Author")
        url = author_no_year.get_google_search_url()
        
        # Should contain author name with spaces encoded as %20
        self.assertIn("Anonymous%20Author", url)
        self.assertNotIn("None", url)
        self.assertIn("google.com/search", url)
        self.assertIn("udm=14", url)
        
    def test_work_google_search_url(self):
        """Test that work Google search URL is generated correctly"""
        url = self.work.get_google_search_url()
        
        # Should contain author name and work title with spaces encoded as %20
        self.assertIn("Gabriel%20Garc%C3%ADa%20M%C3%A1rquez", url)
        self.assertIn("One%20Hundred%20Years%20of%20Solitude", url)
        self.assertIn("google.com/search", url)
        self.assertIn("udm=14", url)
