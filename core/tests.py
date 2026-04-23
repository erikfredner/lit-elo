"""Test suite for the core application."""

import statistics
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, Client
from django.urls import reverse

from .models import Author, Work, LLMMatchup


# ── Search and model tests ────────────────────────────────────────────────────

class AccentInsensitiveSearchTestCase(TestCase):
    def setUp(self):
        self.author_with_accents = Author.objects.create(
            name="Gabriel García Márquez", birth_year=1927, death_year=2014
        )
        self.work_with_accents = Work.objects.create(
            title="Cien años de soledad",
            author=self.author_with_accents,
            publication_year=1967,
        )

    def test_author_search_accent_insensitive(self):
        self.assertIn(self.author_with_accents, Author.objects.search("garcia marquez"))
        self.assertIn(self.author_with_accents, Author.objects.search("marquez"))
        self.assertIn(self.author_with_accents, Author.objects.search("gabriel"))

    def test_work_search_accent_insensitive(self):
        self.assertIn(self.work_with_accents, Work.objects.search("cien anos"))
        self.assertIn(self.work_with_accents, Work.objects.search("garcia"))

    def test_search_view_accent_insensitive(self):
        url = reverse('core:search')

        response = self.client.get(url, {'q': 'marquez'})
        self.assertEqual(response.status_code, 200)
        author_items = [row['item'] for row in response.context['author_page_obj']]
        self.assertIn(self.author_with_accents, author_items)

        response = self.client.get(url, {'q': 'cien anos'})
        self.assertEqual(response.status_code, 200)
        work_items = [row['item'] for row in response.context['work_page_obj']]
        self.assertIn(self.work_with_accents, work_items)


class GoogleSearchURLTestCase(TestCase):
    def setUp(self):
        self.author = Author.objects.create(
            name="Gabriel García Márquez", birth_year=1927, death_year=2014
        )
        self.work = Work.objects.create(
            title="One Hundred Years of Solitude",
            author=self.author,
            publication_year=1967,
        )

    def test_author_google_search_url(self):
        url = self.author.get_google_search_url()
        self.assertIn("Gabriel%20Garc%C3%ADa%20M%C3%A1rquez", url)
        self.assertIn("1927", url)
        self.assertIn("google.com/search", url)
        self.assertIn("udm=14", url)

    def test_author_google_search_url_no_birth_year(self):
        author = Author.objects.create(name="Anonymous Author")
        url = author.get_google_search_url()
        self.assertIn("Anonymous%20Author", url)
        self.assertNotIn("None", url)
        self.assertIn("google.com/search", url)
        self.assertIn("udm=14", url)

    def test_work_google_search_url(self):
        url = self.work.get_google_search_url()
        self.assertIn("Gabriel%20Garc%C3%ADa%20M%C3%A1rquez", url)
        self.assertIn("One%20Hundred%20Years%20of%20Solitude", url)
        self.assertIn("google.com/search", url)
        self.assertIn("udm=14", url)


# ── Manager tests ─────────────────────────────────────────────────────────────

class ModelManagerTestCase(TestCase):
    def setUp(self):
        self.author = Author.objects.create(name="William Shakespeare", elo_rating=1300)
        Author.objects.create(name="Charles Dickens", elo_rating=1250)

    def test_author_search(self):
        results = Author.objects.search("shakespeare")
        self.assertIn(self.author, results)
        self.assertNotIn(Author.objects.get(name="Charles Dickens"), results)


# ── View tests ────────────────────────────────────────────────────────────────

class HomeViewTestCase(TestCase):
    def test_home_renders(self):
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 200)


class LeaderboardViewTestCase(TestCase):
    def setUp(self):
        Author.objects.create(name="Test Author 1")
        Author.objects.create(name="Test Author 2")

    def test_author_leaderboard(self):
        response = self.client.get(reverse('core:authors_lb'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Author 1')
        self.assertContains(response, 'Test Author 2')


class SearchViewTestCase(TestCase):
    def setUp(self):
        Author.objects.create(name="Test Author 1")

    def test_search_view(self):
        response = self.client.get(reverse('core:search') + '?q=Test&mode=authors')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Author')


# ── import_csv_data management command tests ──────────────────────────────────

def _write_csvs(tmpdir: Path, authors_rows: list, works_rows: list | None = None) -> None:
    """Write minimal authors.csv and works.csv to tmpdir."""
    authors_path = tmpdir / "authors.csv"
    with authors_path.open("w", encoding="utf-8") as f:
        f.write("author_id,first_name,last_name,birth,death,mlaib_record_count,viaf_id\n")
        for row in authors_rows:
            f.write(
                "{author_id},{first_name},{last_name},{birth},{death},{mlaib_record_count},{viaf_id}\n".format(
                    author_id=row.get("author_id", ""),
                    first_name=row.get("first_name", ""),
                    last_name=row.get("last_name", ""),
                    birth=row.get("birth", ""),
                    death=row.get("death", ""),
                    mlaib_record_count=row.get("mlaib_record_count", ""),
                    viaf_id=row.get("viaf_id", ""),
                )
            )
    works_path = tmpdir / "works.csv"
    with works_path.open("w", encoding="utf-8") as f:
        f.write("author_id,title,year,mlaib_record_count\n")
        for row in (works_rows or []):
            f.write("{author_id},{title},{year},{mlaib_record_count}\n".format(**row))


def _run_import(tmpdir: Path, **kwargs):
    """Call import_csv_data with both data-dir paths patched to tmpdir."""
    out = StringIO()
    defaults = dict(min_count=0, dry_run=False, stdout=out, stderr=StringIO())
    defaults.update(kwargs)
    with (
        patch("core.management.commands.import_csv_data._DATA_DIR", tmpdir),
        patch("core.management.commands.seed_mlaib_elo._DATA_DIR", tmpdir),
    ):
        call_command("import_csv_data", **defaults)
    return out.getvalue()


class ImportCsvDataMlaiEloTests(TestCase):
    def test_mlaib_elo_set_for_new_authors(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 10},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
                {"author_id": 3, "first_name": "Carol", "last_name": "C", "mlaib_record_count": 200},
            ])
            _run_import(tmpdir)
        self.assertEqual(Author.objects.filter(mlaib_elo__isnull=False).count(), 3)

    def test_min_count_author_gets_elo_min(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 10},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
                {"author_id": 3, "first_name": "Carol", "last_name": "C", "mlaib_record_count": 200},
            ])
            _run_import(tmpdir)
        self.assertAlmostEqual(Author.objects.get(name="Alice A").mlaib_elo, 800.0)

    def test_max_count_author_gets_elo_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 10},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
                {"author_id": 3, "first_name": "Carol", "last_name": "C", "mlaib_record_count": 200},
            ])
            _run_import(tmpdir)
        self.assertAlmostEqual(Author.objects.get(name="Carol C").mlaib_elo, 1600.0)

    def test_single_author_mlaib_elo_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 500},
            ])
            _run_import(tmpdir)
        self.assertIsNone(Author.objects.get(name="Alice A").mlaib_elo)

    def test_all_same_count_gets_midpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 100},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 100},
                {"author_id": 3, "first_name": "Carol", "last_name": "C", "mlaib_record_count": 100},
            ])
            _run_import(tmpdir)
        for author in Author.objects.all():
            self.assertAlmostEqual(author.mlaib_elo, 1200.0)

    def test_existing_author_mlaib_elo_recomputed(self):
        # mlaib_elo is always recomputed from the CSV distribution; only elo_rating
        # is protected (not overwritten if it has moved away from the default).
        Author.objects.create(name="Alice A", mlaib_elo=999.0, mlaib_record_count=10)
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 10},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
            ])
            _run_import(tmpdir)
        self.assertAlmostEqual(Author.objects.get(name="Alice A").mlaib_elo, 800.0)

    def test_missing_count_yields_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": ""},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
            ])
            _run_import(tmpdir)
        self.assertIsNone(Author.objects.get(name="Alice A").mlaib_elo)

    def test_dry_run_creates_no_authors(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 10},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
            ])
            _run_import(tmpdir, dry_run=True)
        self.assertEqual(Author.objects.count(), 0)

    def test_mlaib_elo_within_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 5},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
                {"author_id": 3, "first_name": "Carol", "last_name": "C", "mlaib_record_count": 500},
                {"author_id": 4, "first_name": "Dave", "last_name": "D", "mlaib_record_count": 5000},
            ])
            _run_import(tmpdir)
        for author in Author.objects.filter(mlaib_elo__isnull=False):
            self.assertGreaterEqual(author.mlaib_elo, 800.0)
            self.assertLessEqual(author.mlaib_elo, 1600.0)


# ── run_llm_elo management command tests ─────────────────────────────────────

def _make_matchup(a, b):
    LLMMatchup.objects.create(
        content_type="author",
        item_a_id=a.pk,
        item_b_id=b.pk,
        winner="A",
        elo_a_before=1200.0,
        elo_b_before=1200.0,
        elo_a_after=1216.0,
        elo_b_after=1184.0,
        model_used="test",
    )


def _run_llm_elo(**kwargs):
    """Invoke run_llm_elo --dry-run and return captured stdout."""
    out = StringIO()
    defaults = dict(mode="authors", count=1, seed=42, dry_run=True, stdout=out, stderr=StringIO())
    defaults.update(kwargs)
    call_command("run_llm_elo", **defaults)
    return out.getvalue()


class ExcludeOverrepresentedFlagTests(TestCase):
    """
    Setup: 4 authors (A, B, C, D).
    Matchups: (A,B), (A,C), (A,D) → games_played: {A:3, B:1, C:1, D:1}
    mean=1.5, pstdev≈0.866, threshold≈2.366 → A excluded; B, C, D remain.
    """

    def setUp(self):
        self.a = Author.objects.create(name="Author A", elo_rating=1200)
        self.b = Author.objects.create(name="Author B", elo_rating=1200)
        self.c = Author.objects.create(name="Author C", elo_rating=1200)
        self.d = Author.objects.create(name="Author D", elo_rating=1200)
        _make_matchup(self.a, self.b)
        _make_matchup(self.a, self.c)
        _make_matchup(self.a, self.d)

    def test_overrepresented_item_excluded(self):
        self.assertIn("excluded 1 item(s)", _run_llm_elo(exclude_overrepresented=True))

    def test_threshold_info_in_output(self):
        output = _run_llm_elo(exclude_overrepresented=True)
        counts = [3, 1, 1, 1]
        mean = statistics.mean(counts)
        stdev = statistics.pstdev(counts)
        threshold = mean + stdev
        self.assertIn(f"threshold {threshold:.1f}", output)
        self.assertIn(f"mean={mean:.1f}", output)
        self.assertIn(f"stdev={stdev:.1f}", output)

    def test_flag_absent_no_exclusion(self):
        output = _run_llm_elo()
        self.assertNotIn("excluded", output)
        self.assertNotIn("exclude-overrepresented", output)

    def test_skips_filter_when_all_counts_equal(self):
        LLMMatchup.objects.all().delete()
        self.assertIn("skipping filter", _run_llm_elo(exclude_overrepresented=True))

    def test_excluded_item_absent_from_pairings(self):
        output = _run_llm_elo(exclude_overrepresented=True, count=3)
        self.assertNotIn("Author A", output)
        self.assertIn("Author B", output)
