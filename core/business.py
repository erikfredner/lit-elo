"""
Core business logic services for the ELO ranking system.
"""
from typing import Union, List


from .models import Author, Work


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
