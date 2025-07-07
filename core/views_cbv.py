"""
Modern class-based views following Django best practices.
"""
from django.shortcuts import redirect, get_object_or_404
from django.views.generic import TemplateView, ListView

from .models import Author, Work
from .business import PairingService, ComparisonService, SearchService, LeaderboardService
from .constants import LEADERBOARD_PAGE_SIZE


class HomeView(TemplateView):
    """Redirect to author voting by default."""
    
    def get(self, request, *args, **kwargs):
        return redirect("core:compare", mode="authors")


class CompareView(TemplateView):
    """Handle voting comparisons for authors or works."""
    template_name = "compare.html"
    
    def get(self, request, mode, *args, **kwargs):
        # Handle voting via GET parameters
        winner = request.GET.get("winner")
        item_a_id = request.GET.get("item_a_id")
        item_b_id = request.GET.get("item_b_id")
        
        if winner and item_a_id and item_b_id:
            # Process the vote
            model = Author if mode == "authors" else Work
            item_a = get_object_or_404(model, id=item_a_id)
            item_b = get_object_or_404(model, id=item_b_id)
            ComparisonService.record_comparison(item_a, item_b, winner)
            # Redirect to new comparison to avoid duplicate votes on refresh
            return redirect("core:compare", mode=mode)

        # Get new pairing
        model = Author if mode == "authors" else Work
        item_a, item_b = PairingService.get_two_by_elo(model)
        
        context = self.get_context_data(**kwargs)
        context.update({
            "item_a": item_a, 
            "item_b": item_b, 
            "mode": mode
        })
        return self.render_to_response(context)


class LeaderboardView(ListView):
    """Base leaderboard view with pagination."""
    template_name = "leaderboard.html"
    paginate_by = LEADERBOARD_PAGE_SIZE
    context_object_name = "page_obj"
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paginator = context['paginator']
        page_obj = context['page_obj']
        
        # Add pagination ranges
        context['pagination_ranges'] = LeaderboardService.get_pagination_ranges(
            paginator, page_obj.number
        )
        
        return context


class AuthorLeaderboardView(LeaderboardView):
    """Authors leaderboard."""
    
    def get_queryset(self):
        return Author.objects.by_elo_rating()
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "title": "Authors by Canonicity",
            "mode": "authors"
        })
        return context


class WorkLeaderboardView(LeaderboardView):
    """Works leaderboard."""
    
    def get_queryset(self):
        return Work.objects.by_elo_rating()
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "title": "Works by Canonicity", 
            "mode": "works"
        })
        return context


class SearchView(TemplateView):
    """Search functionality with context."""
    template_name = 'search.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get('q', '').strip()
        mode = self.request.GET.get('mode', 'authors')
        
        results = []
        if query:
            results = SearchService.search_with_context(query, mode)
        
        context.update({
            'query': query,
            'mode': mode,
            'results': results,
            'total_results': len(results)
        })
        return context


class AboutView(TemplateView):
    """About page."""
    template_name = 'about.html'
