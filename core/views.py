import random
import math
from django.shortcuts import render, redirect, get_object_or_404
from django.core.paginator import Paginator
from .models import Author, Work, Comparison, LLMMatchup
from .services import record_comparison

def home(request):
    # Redirect to author voting by default
    return redirect("core:compare", mode="authors")


def _get_two_by_elo(model):
    """
    Select two items for comparison based on ELO ratings.
    More likely to pair items with similar ELOs, but avoids recent pairings.
    """
    all_items = list(model.objects.all())
    
    if len(all_items) < 2:
        # Fallback if not enough items
        return random.sample(all_items, 2)
    
    content_type = 'author' if model == Author else 'work'
    max_attempts = 20  # Limit attempts to avoid infinite loops
    
    for attempt in range(max_attempts):
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
                elo_weight = math.exp(-elo_diff / 100)
                
                # Check if this pairing was recent
                if Comparison.was_recently_compared(content_type, item_a.id, item_b.id, hours=6):
                    # Heavily penalize recent comparisons (but don't eliminate entirely)
                    recent_penalty = 0.1
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
    # This shouldn't happen often, but ensures we always return something
    item_a, item_b = random.sample(all_items, 2)
    Comparison.record_comparison(content_type, item_a.id, item_b.id)
    return item_a, item_b


def compare(request, mode):
    model = Author if mode == "authors" else Work

    # Handle voting via GET parameters
    winner = request.GET.get("winner")
    item_a_id = request.GET.get("item_a_id")
    item_b_id = request.GET.get("item_b_id")
    
    if winner and item_a_id and item_b_id:
        # Process the vote
        item_a = get_object_or_404(model, id=item_a_id)
        item_b = get_object_or_404(model, id=item_b_id)
        
        # Validate winner parameter
        if winner in ['A', 'B']:
            record_comparison(item_a, item_b, winner)
        
        # Redirect to new comparison to avoid duplicate votes on refresh
        return redirect("core:compare", mode=mode)

    item_a, item_b = _get_two_by_elo(model)
    return render(request, "compare.html",
                  {"item_a": item_a, "item_b": item_b, "mode": mode})


def _pagination_items(current: int, total: int) -> list:
    """
    Return a list of page numbers (int) and ellipsis sentinels (None) for display.
    Always shows page 1, the last page, and a window of ±2 around the current page.
    None values render as '…'.
    """
    window = set(range(max(1, current - 2), min(total, current + 2) + 1))
    window.add(1)
    window.add(total)
    pages = sorted(window)
    items: list = []
    for i, p in enumerate(pages):
        if i > 0 and p - pages[i - 1] > 1:
            items.append(None)  # gap → ellipsis
        items.append(p)
    return items


def author_leaderboard(request):
    authors = Author.objects.all().order_by('-elo_rating')
    paginator = Paginator(authors, 50)
    page_obj = paginator.get_page(request.GET.get('page', 1))
    return render(request, "leaderboard.html", {
        "page_obj": page_obj,
        "title": "Authors by Canonicity",
        "mode": "authors",
        "page_items": _pagination_items(page_obj.number, paginator.num_pages),
    })


def work_leaderboard(request):
    works = Work.objects.select_related("author").order_by('-elo_rating')
    paginator = Paginator(works, 50)
    page_obj = paginator.get_page(request.GET.get('page', 1))
    return render(request, "leaderboard.html", {
        "page_obj": page_obj,
        "title": "Works by Canonicity",
        "mode": "works",
        "page_items": _pagination_items(page_obj.number, paginator.num_pages),
    })


def search(request):
    query = request.GET.get('q', '').strip()
    mode = request.GET.get('mode', 'authors')  # 'authors' or 'works'
    results = []
    
    if query:
        if mode == 'authors':
            # Search authors by name (accent-insensitive)
            matching_authors = Author.objects.search(query).order_by('-elo_rating')
            
            # Get all authors ordered by ELO for context
            all_authors = list(Author.objects.all().order_by('-elo_rating'))
            
            for author in matching_authors:
                # Find the author's position in the ranked list
                try:
                    position = all_authors.index(author)
                    rank = position + 1
                    
                    # Get context: 2 above and 2 below
                    context_start = max(0, position - 2)
                    context_end = min(len(all_authors), position + 3)
                    context_authors = all_authors[context_start:context_end]
                    
                    results.append({
                        'item': author,
                        'rank': rank,
                        'context': context_authors,
                        'context_start_rank': context_start + 1,
                        'matched_position': position - context_start  # Position of matched item in context
                    })
                except ValueError:
                    # Author not found in list (shouldn't happen)
                    continue
                    
        else:  # mode == 'works'
            # Search works by title or author name (accent-insensitive)
            matching_works = Work.objects.search(query).order_by('-elo_rating')
            
            # Get all works ordered by ELO for context
            all_works = list(Work.objects.select_related('author').all().order_by('-elo_rating'))
            
            for work in matching_works:
                # Find the work's position in the ranked list
                try:
                    position = all_works.index(work)
                    rank = position + 1
                    
                    # Get context: 2 above and 2 below
                    context_start = max(0, position - 2)
                    context_end = min(len(all_works), position + 3)
                    context_works = all_works[context_start:context_end]
                    
                    results.append({
                        'item': work,
                        'rank': rank,
                        'context': context_works,
                        'context_start_rank': context_start + 1,
                        'matched_position': position - context_start  # Position of matched item in context
                    })
                except ValueError:
                    # Work not found in list (shouldn't happen)
                    continue
    
    return render(request, 'search.html', {
        'query': query,
        'mode': mode,
        'results': results,
        'total_results': len(results)
    })

def author_detail(request, pk):
    author = get_object_or_404(Author, pk=pk)
    rank = Author.objects.filter(elo_rating__gt=author.elo_rating).count() + 1
    author_works = list(author.works.order_by('-elo_rating'))
    works_with_rank = [
        {'work': w, 'rank': Work.objects.filter(elo_rating__gt=w.elo_rating).count() + 1}
        for w in author_works
    ]
    return render(request, 'author_detail.html', {
        'author': author,
        'rank': rank,
        'works_with_rank': works_with_rank,
    })


def work_detail(request, pk):
    work = get_object_or_404(Work.objects.select_related('author'), pk=pk)
    rank = Work.objects.filter(elo_rating__gt=work.elo_rating).count() + 1
    author_works = list(work.author.works.order_by('-elo_rating'))
    works_with_rank = [
        {
            'work': w,
            'rank': Work.objects.filter(elo_rating__gt=w.elo_rating).count() + 1,
            'is_current': w.pk == work.pk,
        }
        for w in author_works
    ]
    return render(request, 'work_detail.html', {
        'work': work,
        'rank': rank,
        'works_with_rank': works_with_rank,
    })


def about(request):
    return render(request, 'about.html')


def recent_results(request):
    author_matchups = list(
        LLMMatchup.objects.filter(content_type='author').order_by('-created_at')[:10]
    )
    work_matchups = list(
        LLMMatchup.objects.filter(content_type='work').order_by('-created_at')[:10]
    )

    # Resolve author PKs to objects in two queries
    author_ids = {m.item_a_id for m in author_matchups} | {m.item_b_id for m in author_matchups}
    authors_by_id = {a.pk: a for a in Author.objects.filter(pk__in=author_ids)}

    work_ids = {m.item_a_id for m in work_matchups} | {m.item_b_id for m in work_matchups}
    works_by_id = {
        w.pk: w
        for w in Work.objects.select_related('author').filter(pk__in=work_ids)
    }

    def build_rows(matchups, lookup):
        rows = []
        for m in matchups:
            item_a = lookup.get(m.item_a_id)
            item_b = lookup.get(m.item_b_id)
            if not item_a or not item_b:
                continue
            if m.winner == 'A':
                winner, loser = item_a, item_b
                delta = m.elo_a_after - m.elo_a_before
            else:
                winner, loser = item_b, item_a
                delta = m.elo_b_after - m.elo_b_before
            rows.append({
                'winner': winner,
                'loser': loser,
                'delta': delta,
                'created_at': m.created_at,
            })
        return rows

    return render(request, 'recent.html', {
        'author_rows': build_rows(author_matchups, authors_by_id),
        'work_rows': build_rows(work_matchups, works_by_id),
    })