"""Pure pytest tests for compute_mlaib_elo (no database required)."""

import math

import pytest
from core.management.commands.seed_mlaib_elo import ELO_PER_LN_COUNT, compute_mlaib_elo


def test_empty_list():
    assert compute_mlaib_elo([]) == []


def test_all_none_counts():
    assert compute_mlaib_elo([None, None]) == [None, None]


def test_single_count_sits_at_the_default():
    assert compute_mlaib_elo([500]) == [pytest.approx(1200)]
    assert compute_mlaib_elo([None, 50]) == [None, pytest.approx(1200)]


def test_all_same_counts_sit_at_the_default():
    assert compute_mlaib_elo([100, 100, 100]) == [pytest.approx(1200)] * 3


def test_each_tenfold_adds_the_same_amount():
    lo, mid, hi = compute_mlaib_elo([10, 100, 1000])
    assert mid == pytest.approx(1200)
    assert hi - mid == pytest.approx(mid - lo) == pytest.approx(ELO_PER_LN_COUNT * math.log(10))


def test_centered_on_the_mean_log_count_and_none_ignored():
    result = compute_mlaib_elo([None, 5, 40, 7525])
    assert result[0] is None
    assert sum(result[1:]) / 3 == pytest.approx(1200)


def test_one_outlier_does_not_flatten_the_rest():
    # Under min-max scaling, a single huge count squeezed everyone else together.
    without = compute_mlaib_elo([10, 100])
    with_outlier = compute_mlaib_elo([10, 100, 100_000])
    assert with_outlier[1] - with_outlier[0] == pytest.approx(without[1] - without[0])


def test_slope_and_clamp_below_one():
    a, b = compute_mlaib_elo([1, math.e], slope=100)
    assert b - a == pytest.approx(100)
    assert compute_mlaib_elo([0, 1]) == [pytest.approx(1200)] * 2


def test_monotonically_increasing_and_length_preserved():
    counts = [10, 50, 200, 1000]
    result = compute_mlaib_elo(counts)
    assert len(result) == len(counts)
    assert all(result[i] < result[i + 1] for i in range(len(result) - 1))
