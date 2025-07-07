import pytest
from core.elo import expected, update

def test_expected_score():
    # Test cases from Wikipedia ELO article
    assert abs(expected(1616, 1600) - 0.523) < 0.001
    assert abs(expected(1600, 1616) - 0.477) < 0.001
    assert abs(expected(2400, 2000) - 0.909) < 0.001
    assert abs(expected(2000, 2400) - 0.091) < 0.001

def test_update_ratings():
    # Test case: Player A (1616) beats Player B (1600)
    rating_a, rating_b = update(1616, 1600, 1) # A wins
    assert abs(rating_a - 1632) < 1 # Should be around 1632
    assert abs(rating_b - 1584) < 1 # Should be around 1584

    # Test case: Player A (1600) loses to Player B (1616)
    rating_a, rating_b = update(1600, 1616, 0) # A loses
    assert abs(rating_a - 1584) < 1 # Should be around 1584
    assert abs(rating_b - 1632) < 1 # Should be around 1632

    # Test case: Draw
    rating_a, rating_b = update(1600, 1600, 0.5) # Draw
    assert abs(rating_a - 1600) < 0.001
    assert abs(rating_b - 1600) < 0.001
