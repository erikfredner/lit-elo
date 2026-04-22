"""Pure pytest tests for compute_mlaib_elo (no database required)."""

import pytest
from core.management.commands.import_csv_data import (
    _MLAIB_ELO_MAX,
    _MLAIB_ELO_MIN,
    compute_mlaib_elo,
)


def test_empty_list():
    assert compute_mlaib_elo([]) == []


def test_single_count_returns_none():
    assert compute_mlaib_elo([500]) == [None]


def test_all_none_counts():
    assert compute_mlaib_elo([None, None]) == [None, None]


def test_single_valid_among_nones_returns_all_none():
    # Only 1 valid count — stdev is undefined
    assert compute_mlaib_elo([None, 50]) == [None, None]


def test_all_same_counts_returns_midpoint():
    midpoint = (_MLAIB_ELO_MIN + _MLAIB_ELO_MAX) / 2
    result = compute_mlaib_elo([100, 100, 100])
    assert result == [midpoint, midpoint, midpoint]


def test_two_distinct_min_gets_zero_max_gets_3000():
    result = compute_mlaib_elo([0, 100])
    assert result[0] == pytest.approx(_MLAIB_ELO_MIN)
    assert result[1] == pytest.approx(_MLAIB_ELO_MAX)


def test_none_mixed_scaling_excludes_none_from_range():
    # None at index 0; valid entries [100, 200] scale over their own range
    result = compute_mlaib_elo([None, 100, 200])
    assert result[0] is None
    assert result[1] == pytest.approx(_MLAIB_ELO_MIN)
    assert result[2] == pytest.approx(_MLAIB_ELO_MAX)


def test_monotonically_increasing():
    counts = [10, 50, 200, 1000]
    result = compute_mlaib_elo(counts)
    assert all(result[i] < result[i + 1] for i in range(len(result) - 1))


def test_output_length_matches_input():
    counts = [10, 20, 30]
    assert len(compute_mlaib_elo(counts)) == len(counts)


def test_all_non_none_values_in_range():
    counts = [5, 10, 50, 200, 1000, 5000]
    result = compute_mlaib_elo(counts)
    for v in result:
        assert v is not None
        assert _MLAIB_ELO_MIN <= v <= _MLAIB_ELO_MAX


def test_min_author_gets_zero():
    counts = [10, 50, 200]
    result = compute_mlaib_elo(counts)
    assert result[0] == pytest.approx(_MLAIB_ELO_MIN)


def test_max_author_gets_3000():
    counts = [10, 50, 200]
    result = compute_mlaib_elo(counts)
    assert result[-1] == pytest.approx(_MLAIB_ELO_MAX)
