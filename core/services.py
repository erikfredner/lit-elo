"""
Legacy services module - kept for backwards compatibility.
New code should use business.py services.
"""
from django.db import transaction
from .elo import update

@transaction.atomic
def record_comparison(item_a, item_b, winner):
    """
    item_a / item_b: Author or Work instances
    winner: 'A' or 'B'
    
    DEPRECATED: Use ComparisonService.record_comparison instead
    """
    score = 1 if winner == 'A' else 0
    new_a, new_b = update(item_a.elo_rating, item_b.elo_rating, score)
    item_a.elo_rating = new_a
    item_b.elo_rating = new_b
    item_a.save(update_fields=["elo_rating"])
    item_b.save(update_fields=["elo_rating"])
