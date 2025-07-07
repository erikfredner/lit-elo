"""
ELO rating calculation utilities.
"""
from .constants import ELO_K_FACTOR


def expected(score_a: float, score_b: float) -> float:
    """Calculate expected score of A vs B."""
    return 1 / (1 + 10 ** ((score_b - score_a) / 400))


def update(rating_a: float, rating_b: float, result: float) -> tuple[float, float]:
    """
    Update ratings.
    
    Args:
        rating_a: Current rating of player A
        rating_b: Current rating of player B  
        result: 1 if A wins, 0 if B wins, 0.5 if draw
        
    Returns:
        Tuple of (new_rating_a, new_rating_b)
    """
    exp_a = expected(rating_a, rating_b)
    exp_b = 1 - exp_a
    return (
        rating_a + ELO_K_FACTOR * (result - exp_a),
        rating_b + ELO_K_FACTOR * ((1 - result) - exp_b),
    )
