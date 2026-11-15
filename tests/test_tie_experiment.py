"""Tests for the tie side experiment's pure helpers."""

from core.management.commands.tie_experiment import band_of, pair_outcome, parse_answer, vote_for_a


def test_parse_answer_accepts_zero_one_two():
    assert parse_answer('{"more_canonical": 0}') == 0
    assert parse_answer("2") == 2
    assert parse_answer('Sure: {"more_canonical": 1}') == 1
    assert parse_answer('{"more_canonical": 3}') is None
    assert parse_answer("") is None


def test_vote_for_a_respects_order():
    assert vote_for_a("AB", 1) == "A"
    assert vote_for_a("BA", 1) == "B"
    assert vote_for_a("BA", 2) == "A"
    assert vote_for_a("AB", 0) == "tie"


def test_pair_outcome():
    assert pair_outcome(["A", "A"]) == "decisive"
    assert pair_outcome(["tie", "tie"]) == "tie"
    assert pair_outcome(["A", "tie"]) == "lean"
    assert pair_outcome(["A", "B"]) == "split"
    assert pair_outcome(["A"]) == "missing"


def test_band_of():
    assert band_of(1600) == "a ≥1100"
    assert band_of(1000) == "b 950–1100"
    assert band_of(800) == "d <850"
