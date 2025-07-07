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
    winner: 'A', 'B', or 'TIE'
    
    DEPRECATED: Use ComparisonService.record_comparison instead
    """
    if winner == 'A':
        score = 1
    elif winner == 'B':
        score = 0
    else:  # TIE
        score = 0.5
        
    new_a, new_b = update(item_a.elo_rating, item_b.elo_rating, score)
    item_a.elo_rating = new_a
    item_b.elo_rating = new_b
    item_a.save(update_fields=["elo_rating"])
    item_b.save(update_fields=["elo_rating"])
