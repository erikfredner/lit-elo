"""Tests for matchup pairing: balanced games across the ladder, history counting."""

import random
from collections import Counter
from types import SimpleNamespace

from django.test import TestCase

from core.management.commands.run_llm_elo import _generate_pairings, _load_matchup_index
from core.models import Author, LLMMatchup


def _items():
    # A skewed ladder like the MLAIB prior: a few items far above a big pack.
    top = [SimpleNamespace(pk=i, elo_rating=1600 - 50 * i) for i in range(1, 5)]
    pack = [SimpleNamespace(pk=i, elo_rating=800 + (i % 50)) for i in range(5, 205)]
    return top + pack


def test_pairings_are_distinct_and_complete():
    items = _items()
    pairs = _generate_pairings(items, 500, random.Random(0))
    assert len(pairs) == 500
    assert len({(min(a.pk, b.pk), max(a.pk, b.pk)) for a, b in pairs}) == 500
    assert all(a.pk != b.pk for a, b in pairs)


def test_items_far_above_the_pack_get_their_share_of_games():
    items = _items()
    pairs = _generate_pairings(items, 500, random.Random(0))  # 5 games per item on average
    games = Counter()
    for a, b in pairs:
        games[a.pk] += 1
        games[b.pk] += 1
    assert min(games[i.pk] for i in items) >= 4
    assert all(games[pk] >= 4 for pk in range(1, 5))  # the outliers


def test_history_is_evened_out():
    items = _items()
    history = {i.pk: 10 for i in items[4:]}  # the pack already played; the top did not
    pairs = _generate_pairings(items, 200, random.Random(0), games_played=history)
    first_picks = Counter(a.pk for a, _ in pairs)
    assert all(first_picks[pk] >= 5 for pk in range(1, 5))


class MatchupIndexTests(TestCase):
    def test_pairing_judged_by_several_models_counts_once(self):
        a = Author.objects.create(name="A")
        b = Author.objects.create(name="B")
        for model in ("anthropic/claude-haiku-5.5", "openai/gpt-6-luna"):
            LLMMatchup.objects.create(
                content_type="author", item_a_id=a.pk, item_b_id=b.pk, winner="A",
                elo_a_before=1200, elo_b_before=1200, elo_a_after=1216, elo_b_after=1184,
                model_used=model,
            )
        pair_counts, games = _load_matchup_index("authors")
        assert pair_counts[(min(a.pk, b.pk), max(a.pk, b.pk))] == 1
        assert games == {a.pk: 1, b.pk: 1}
