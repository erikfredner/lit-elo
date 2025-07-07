"""
Core business logic services for the ELO ranking system.
"""
import random
import math
from typing import Tuple, Type, Union, List
from django.db import transaction
from django.core.exceptions import ValidationError

from .models import Author, Work, Comparison
from .elo import update as elo_update
from .constants import (
    COMPARISON_PENALTY_HOURS, 
    MAX_PAIRING_ATTEMPTS, 
    ELO_WEIGHT_DIVISOR, 
    RECENT_COMPARISON_PENALTY
)


class PairingService:
    """Service for selecting optimal ELO-based pairings."""
    
    @staticmethod
    def get_two_by_elo(model: Type[Union[Author, Work]]) -> Tuple[Union[Author, Work], Union[Author, Work]]:
        """
        Select two items for comparison based on ELO ratings.
        More likely to pair items with similar ELOs, but avoids recent pairings.
        """
        if model == Author:
            all_items = list(Author.objects.by_elo_rating())
            content_type = 'author'
        else:
            all_items = list(Work.objects.by_elo_rating())
            content_type = 'work'
        
        if len(all_items) < 2:
            raise ValidationError("Not enough items for comparison")
        
        for attempt in range(MAX_PAIRING_ATTEMPTS):
            # Pick first item randomly
            item_a = random.choice(all_items)
            
            # Calculate weights for second item based on ELO difference and recent comparisons
            weights = []
            for item_b in all_items:
                if item_b.id == item_a.id:
                    weights.append(0)  # Can't compare to itself
                else:
                    # Calculate ELO difference weight
                    elo_diff = abs(item_a.elo_rating - item_b.elo_rating)
                    elo_weight = math.exp(-elo_diff / ELO_WEIGHT_DIVISOR)
                    
                    # Check if this pairing was recent
                    if Comparison.was_recently_compared(
                        content_type, item_a.id, item_b.id, hours=COMPARISON_PENALTY_HOURS
                    ):
                        # Heavily penalize recent comparisons (but don't eliminate entirely)
                        recent_penalty = RECENT_COMPARISON_PENALTY
                    else:
                        recent_penalty = 1.0
                    
                    weight = elo_weight * recent_penalty
                    weights.append(weight)
            
            # Choose second item based on weights
            if sum(weights) > 0:
                item_b = random.choices(all_items, weights=weights)[0]
                
                # Record this comparison
                Comparison.record_comparison(content_type, item_a.id, item_b.id)
                
                return item_a, item_b
        
        # Fallback if we can't find a good pairing after max_attempts
        item_a, item_b = random.sample(all_items, 2)
        Comparison.record_comparison(content_type, item_a.id, item_b.id)
        return item_a, item_b


class ComparisonService:
    """Service for recording and processing comparisons."""
    
    @staticmethod
    @transaction.atomic
    def record_comparison(item_a: Union[Author, Work], item_b: Union[Author, Work], winner: str) -> None:
        """
        Record a comparison result and update ELO ratings.
        
        Args:
            item_a: First item being compared
            item_b: Second item being compared  
            winner: 'A' or 'B' indicating which item won
        """
        if winner not in ['A', 'B']:
            raise ValidationError("Winner must be 'A' or 'B'")
        
        score = 1 if winner == 'A' else 0
        new_a, new_b = elo_update(item_a.elo_rating, item_b.elo_rating, score)
        
        item_a.elo_rating = new_a
        item_b.elo_rating = new_b
        
        item_a.save(update_fields=["elo_rating"])
        item_b.save(update_fields=["elo_rating"])


class SearchService:
    """Service for search functionality."""
    
    @staticmethod
    def search_with_context(query: str, mode: str = 'authors') -> List[dict]:
        """
        Search for items and return them with ranking context.
        
        Args:
            query: Search term
            mode: 'authors' or 'works'
            
        Returns:
            List of dicts containing item, rank, and context
        """
        results = []
        
        if mode == 'authors':
            matching_items = Author.objects.search(query).by_elo_rating()
            all_items = list(Author.objects.by_elo_rating())
        else:
            matching_items = Work.objects.search(query).by_elo_rating()
            all_items = list(Work.objects.by_elo_rating())
        
        for item in matching_items:
            try:
                position = all_items.index(item)
                rank = position + 1
                
                # Get context: 2 above and 2 below
                context_start = max(0, position - 2)
                context_end = min(len(all_items), position + 3)
                context_items = all_items[context_start:context_end]
                
                results.append({
                    'item': item,
                    'rank': rank,
                    'context': context_items,
                    'context_start_rank': context_start + 1,
                    'matched_position': position - context_start
                })
            except ValueError:
                # Item not found in list (shouldn't happen)
                continue
                
        return results


class LeaderboardService:
    """Service for leaderboard functionality."""
    
    @staticmethod
    def get_pagination_ranges(paginator, current_page: int) -> List[dict]:
        """
        Calculate pagination display ranges for leaderboards.
        
        Args:
            paginator: Django Paginator object
            current_page: Current page number
            
        Returns:
            List of pagination range info
        """
        pagination_ranges = []
        items_per_page = paginator.per_page
        
        for page_num in paginator.page_range:
            start_num = (page_num - 1) * items_per_page + 1
            end_num = min(page_num * items_per_page, paginator.count)
            pagination_ranges.append({
                'page_num': page_num,
                'range_text': f"{start_num}-{end_num}",
                'is_current': page_num == current_page
            })
        
        return pagination_ranges
