import random
import math
from django.db.models import Count, Q
from django.shortcuts import render, redirect, get_object_or_404
from django.core.paginator import Paginator
from django.urls import reverse
from .models import Author, Work, Comparison, LLMMatchup
from .business import ComparisonService

def home(request):
    return redirect("core:vote")


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


def vote(request):
    mode = request.GET.get("mode")
    winner = request.GET.get("winner")
    item_a_id = request.GET.get("item_a_id")
    item_b_id = request.GET.get("item_b_id")

    if winner and item_a_id and item_b_id and mode in ("authors", "works"):
        model = Author if mode == "authors" else Work
        item_a = get_object_or_404(model, id=item_a_id)
        item_b = get_object_or_404(model, id=item_b_id)
        if winner in ['A', 'B']:
            ComparisonService.record_comparison(item_a, item_b, winner)
        return redirect("core:vote")

    mode = random.choice(["authors", "works"])
    model = Author if mode == "authors" else Work
    item_a, item_b = _get_two_by_elo(model)
    return render(request, "compare.html", {"item_a": item_a, "item_b": item_b, "mode": mode, "current_page": "vote"})


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
            ComparisonService.record_comparison(item_a, item_b, winner)

        # Redirect to new comparison to avoid duplicate votes on refresh
        return redirect("core:compare", mode=mode)

    item_a, item_b = _get_two_by_elo(model)
    return render(request, "compare.html",
                  {"item_a": item_a, "item_b": item_b, "mode": mode, "current_page": "vote"})


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


def _matchup_counts(content_type: str, pks: list) -> dict:
    """Return {pk: comparison_count} for all given PKs in two queries."""
    a = dict(
        LLMMatchup.objects.filter(content_type=content_type, item_a_id__in=pks)
        .values('item_a_id').annotate(c=Count('id')).values_list('item_a_id', 'c')
    )
    b = dict(
        LLMMatchup.objects.filter(content_type=content_type, item_b_id__in=pks)
        .values('item_b_id').annotate(c=Count('id')).values_list('item_b_id', 'c')
    )
    return {pk: a.get(pk, 0) + b.get(pk, 0) for pk in pks}


def author_leaderboard(request):
    authors = Author.objects.all().order_by('-elo_rating')
    paginator = Paginator(authors, 50)
    page_obj = paginator.get_page(request.GET.get('page', 1))
    total_comparisons = LLMMatchup.objects.filter(content_type='author').count()
    counts = _matchup_counts('author', [obj.pk for obj in page_obj])
    for obj in page_obj:
        obj.comp_count = counts.get(obj.pk, 0)
    return render(request, "leaderboard.html", {
        "page_obj": page_obj,
        "title": "Authors by Canonicity",
        "mode": "authors",
        "page_items": _pagination_items(page_obj.number, paginator.num_pages),
        "total_comparisons": total_comparisons,
        "current_page": "authors_lb",
    })


def work_leaderboard(request):
    works = Work.objects.select_related("author").order_by('-elo_rating')
    paginator = Paginator(works, 50)
    page_obj = paginator.get_page(request.GET.get('page', 1))
    total_comparisons = LLMMatchup.objects.filter(content_type='work').count()
    counts = _matchup_counts('work', [obj.pk for obj in page_obj])
    for obj in page_obj:
        obj.comp_count = counts.get(obj.pk, 0)
    return render(request, "leaderboard.html", {
        "page_obj": page_obj,
        "title": "Works by Canonicity",
        "mode": "works",
        "page_items": _pagination_items(page_obj.number, paginator.num_pages),
        "total_comparisons": total_comparisons,
        "current_page": "works_lb",
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
        'total_results': len(results),
        'current_page': 'search',
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
    return render(request, 'about.html', {'current_page': 'about'})


def _build_comparison_rows(matchups_page, pk, lookup, opponent_url_name):
    """Build display rows for the comparison history page."""
    rows = []
    for m in matchups_page:
        is_a = m.item_a_id == pk
        opp_pk = m.item_b_id if is_a else m.item_a_id
        opponent = lookup.get(opp_pk)
        if opponent is None:
            continue
        won = (m.winner == 'A') == is_a
        elo_before = m.elo_a_before if is_a else m.elo_b_before
        elo_after  = m.elo_a_after  if is_a else m.elo_b_after
        rows.append({
            'opponent': opponent,
            'opponent_url': reverse(opponent_url_name, kwargs={'pk': opp_pk}),
            'won': won,
            'elo_before': elo_before,
            'elo_after':  elo_after,
            'delta':      elo_after - elo_before,
            'created_at': m.created_at,
            'model_used': m.model_used,
        })
    return rows


def author_comparisons(request, pk):
    author = get_object_or_404(Author, pk=pk)
    matchups_qs = LLMMatchup.objects.filter(
        Q(content_type='author', item_a_id=pk) | Q(content_type='author', item_b_id=pk)
    ).order_by('-created_at')
    total_comparisons = matchups_qs.count()
    paginator = Paginator(matchups_qs, 50)
    page_obj = paginator.get_page(request.GET.get('page', 1))
    opp_pks = {m.item_b_id if m.item_a_id == pk else m.item_a_id for m in page_obj}
    lookup = {a.pk: a for a in Author.objects.filter(pk__in=opp_pks)}
    return render(request, 'item_comparisons.html', {
        'item': author,
        'item_name': author.name,
        'detail_url': reverse('core:author_detail', kwargs={'pk': pk}),
        'rows': _build_comparison_rows(page_obj, pk, lookup, 'core:author_detail'),
        'page_obj': page_obj,
        'page_items': _pagination_items(page_obj.number, paginator.num_pages),
        'total_comparisons': total_comparisons,
    })


def work_comparisons(request, pk):
    work = get_object_or_404(Work.objects.select_related('author'), pk=pk)
    matchups_qs = LLMMatchup.objects.filter(
        Q(content_type='work', item_a_id=pk) | Q(content_type='work', item_b_id=pk)
    ).order_by('-created_at')
    total_comparisons = matchups_qs.count()
    paginator = Paginator(matchups_qs, 50)
    page_obj = paginator.get_page(request.GET.get('page', 1))
    opp_pks = {m.item_b_id if m.item_a_id == pk else m.item_a_id for m in page_obj}
    lookup = {w.pk: w for w in Work.objects.select_related('author').filter(pk__in=opp_pks)}
    return render(request, 'item_comparisons.html', {
        'item': work,
        'item_name': work.title,
        'detail_url': reverse('core:work_detail', kwargs={'pk': pk}),
        'rows': _build_comparison_rows(page_obj, pk, lookup, 'core:work_detail'),
        'page_obj': page_obj,
        'page_items': _pagination_items(page_obj.number, paginator.num_pages),
        'total_comparisons': total_comparisons,
    })


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
                'model_used': m.model_used,
            })
        return rows

    return render(request, 'recent.html', {
        'author_rows': build_rows(author_matchups, authors_by_id),
        'work_rows': build_rows(work_matchups, works_by_id),
        'current_page': 'recent',
    })