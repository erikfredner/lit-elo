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
    from .models import Author, LLMMatchup

    score = 1 if winner == 'A' else 0
    elo_a_before = item_a.elo_rating
    elo_b_before = item_b.elo_rating

    new_a, new_b = update(elo_a_before, elo_b_before, score)
    item_a.elo_rating = new_a
    item_b.elo_rating = new_b
    item_a.save(update_fields=["elo_rating"])
    item_b.save(update_fields=["elo_rating"])

    content_type = 'author' if isinstance(item_a, Author) else 'work'
    LLMMatchup.objects.create(
        content_type=content_type,
        item_a_id=item_a.pk,
        item_b_id=item_b.pk,
        winner=winner,
        elo_a_before=elo_a_before,
        elo_b_before=elo_b_before,
        elo_a_after=new_a,
        elo_b_after=new_b,
        model_used='human',
    )
