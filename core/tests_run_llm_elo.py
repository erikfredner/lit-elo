"""Tests for the --exclude-overrepresented flag in run_llm_elo."""

import statistics
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from .models import Author, LLMMatchup


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


def _run(**kwargs):
    """Invoke run_llm_elo --dry-run and return captured stdout."""
    out = StringIO()
    defaults = dict(mode="authors", count=1, seed=42, dry_run=True, stdout=out, stderr=StringIO())
    defaults.update(kwargs)
    call_command("run_llm_elo", **defaults)
    return out.getvalue()


class ExcludeOverrepresentedFlagTests(TestCase):
    """
    Setup: 4 authors (A, B, C, D).
    Matchups: (A,B), (A,C), (A,D)  →  games_played: {A:3, B:1, C:1, D:1}
    mean=1.5, pstdev≈0.866, threshold≈2.366  →  A excluded; B, C, D remain.
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
        output = _run(exclude_overrepresented=True)
        self.assertIn("excluded 1 item(s)", output)

    def test_threshold_info_in_output(self):
        output = _run(exclude_overrepresented=True)
        # Verify mean, stdev, and threshold values are printed.
        # The command format is: "above threshold T (mean=M, stdev=S)"
        counts = [3, 1, 1, 1]
        mean = statistics.mean(counts)
        stdev = statistics.pstdev(counts)
        threshold = mean + stdev
        self.assertIn(f"threshold {threshold:.1f}", output)
        self.assertIn(f"mean={mean:.1f}", output)
        self.assertIn(f"stdev={stdev:.1f}", output)

    def test_flag_absent_no_exclusion(self):
        output = _run()
        self.assertNotIn("excluded", output)
        self.assertNotIn("exclude-overrepresented", output)

    def test_skips_filter_when_all_counts_equal(self):
        # No matchups → all games_played counts are 0, pstdev = 0.
        LLMMatchup.objects.all().delete()
        output = _run(exclude_overrepresented=True)
        self.assertIn("skipping filter", output)

    def test_excluded_item_absent_from_pairings(self):
        # With A excluded, pairings should only involve B, C, D.
        output = _run(exclude_overrepresented=True, count=3)
        self.assertNotIn("Author A", output)
        self.assertIn("Author B", output)
