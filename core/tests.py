from django.test import TestCase, Client
from django.urls import reverse
from .models import Author, Work
from .services import record_comparison
from .constants import DEFAULT_ELO_RATING

class TieVotingTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.author1 = Author.objects.create(name="William Shakespeare", birth_year=1564, death_year=1616)
        self.author2 = Author.objects.create(name="Jane Austen", birth_year=1775, death_year=1817)
        
        self.work1 = Work.objects.create(title="Hamlet", author=self.author1, publication_year=1600)
        self.work2 = Work.objects.create(title="Pride and Prejudice", author=self.author2, publication_year=1813)

    def test_tie_vote_elo_calculation_equal_ratings(self):
        """Test that tie votes between equal-rated items don't change ratings"""
        initial_rating_a = self.author1.elo_rating
        initial_rating_b = self.author2.elo_rating
        
        # Ensure both authors have the same rating
        self.assertEqual(initial_rating_a, initial_rating_b)
        
        record_comparison(self.author1, self.author2, 'TIE')
        
        self.author1.refresh_from_db()
        self.author2.refresh_from_db()
        
        # For equal-rated items, a tie should not change ratings
        self.assertEqual(self.author1.elo_rating, initial_rating_a)
        self.assertEqual(self.author2.elo_rating, initial_rating_b)

    def test_tie_vote_elo_calculation_different_ratings(self):
        """Test that tie votes between differently-rated items adjust ratings toward each other"""
        # Set different ratings
        self.author1.elo_rating = 1300
        self.author1.save()
        self.author2.elo_rating = 1100
        self.author2.save()
        
        record_comparison(self.author1, self.author2, 'TIE')
        
        self.author1.refresh_from_db()
        self.author2.refresh_from_db()
        
        # Higher-rated player should lose points, lower-rated should gain points
        self.assertLess(self.author1.elo_rating, 1300)
        self.assertGreater(self.author2.elo_rating, 1100)
        
        # They should move closer together
        final_diff = abs(self.author1.elo_rating - self.author2.elo_rating)
        initial_diff = abs(1300 - 1100)
        self.assertLess(final_diff, initial_diff)

    def test_author_comparison_tie_vote_url(self):
        """Test that tie voting works via URL parameters for authors"""
        # Set different ratings so we can see a change
        self.author1.elo_rating = 1300
        self.author1.save()
        self.author2.elo_rating = 1100 
        self.author2.save()
        
        url = reverse('core:compare', kwargs={'mode': 'authors'})
        response = self.client.get(url, {
            'winner': 'TIE',
            'item_a_id': self.author1.id,
            'item_b_id': self.author2.id
        })
        
        # Should redirect after voting
        self.assertEqual(response.status_code, 302)
        
        # Check that ratings were updated (higher should decrease, lower should increase)
        self.author1.refresh_from_db()
        self.author2.refresh_from_db()
        self.assertLess(self.author1.elo_rating, 1300)
        self.assertGreater(self.author2.elo_rating, 1100)

    def test_work_comparison_tie_vote_url(self):
        """Test that tie voting works via URL parameters for works"""
        # Set different ratings so we can see a change
        self.work1.elo_rating = 1300
        self.work1.save()
        self.work2.elo_rating = 1100
        self.work2.save()
        
        url = reverse('core:compare', kwargs={'mode': 'works'})
        response = self.client.get(url, {
            'winner': 'TIE',
            'item_a_id': self.work1.id,
            'item_b_id': self.work2.id
        })
        
        # Should redirect after voting
        self.assertEqual(response.status_code, 302)
        
        # Check that ratings were updated (higher should decrease, lower should increase)
        self.work1.refresh_from_db()
        self.work2.refresh_from_db()
        self.assertLess(self.work1.elo_rating, 1300)
        self.assertGreater(self.work2.elo_rating, 1100)

    def test_compare_page_contains_tie_option(self):
        """Test that the comparison page includes the tie option"""
        url = reverse('core:compare', kwargs={'mode': 'authors'})
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'winner=TIE')
        self.assertContains(response, 'Tie')
        self.assertContains(response, 'Equal canonicity')

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
