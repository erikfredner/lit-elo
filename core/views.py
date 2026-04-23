import json

from django.db.models import Count, Q
from django.shortcuts import render, redirect, get_object_or_404
from django.core.paginator import Paginator
from django.urls import reverse
from .models import Author, Work, LLMMatchup
from .constants import DEFAULT_ELO_RATING

def home(request):
    author_series = _get_top_chart_series('author')
    work_series = _get_top_chart_series('work')
    return render(request, 'home.html', {
        'author_series': author_series,
        'work_series': work_series,
        'author_series_json': json.dumps([{'name': s['name'], 'history': s['history']} for s in author_series]),
        'work_series_json': json.dumps([{'name': s['name'], 'history': s['history']} for s in work_series]),
        'current_page': 'home',
    })


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
    author_results = []
    work_results = []

    if query:
        all_authors = list(Author.objects.all().order_by('-elo_rating'))
        for author in Author.objects.search(query).order_by('-elo_rating'):
            try:
                position = all_authors.index(author)
                context_start = max(0, position - 2)
                context_end = min(len(all_authors), position + 3)
                author_results.append({
                    'item': author,
                    'rank': position + 1,
                    'context': all_authors[context_start:context_end],
                    'context_start_rank': context_start + 1,
                    'matched_position': position - context_start,
                })
            except ValueError:
                continue

        all_works = list(Work.objects.select_related('author').order_by('-elo_rating'))
        for work in Work.objects.search(query).order_by('-elo_rating'):
            try:
                position = all_works.index(work)
                context_start = max(0, position - 2)
                context_end = min(len(all_works), position + 3)
                work_results.append({
                    'item': work,
                    'rank': position + 1,
                    'context': all_works[context_start:context_end],
                    'context_start_rank': context_start + 1,
                    'matched_position': position - context_start,
                })
            except ValueError:
                continue

    author_paginator = Paginator(author_results, 10)
    work_paginator = Paginator(work_results, 10)
    author_page_obj = author_paginator.get_page(request.GET.get('author_page', 1))
    work_page_obj = work_paginator.get_page(request.GET.get('work_page', 1))

    return render(request, 'search.html', {
        'query': query,
        'author_page_obj': author_page_obj,
        'author_page_items': _pagination_items(author_page_obj.number, author_paginator.num_pages),
        'work_page_obj': work_page_obj,
        'work_page_items': _pagination_items(work_page_obj.number, work_paginator.num_pages),
        'total_results': len(author_results) + len(work_results),
        'current_page': 'search',
    })

def _get_elo_history(content_type: str, pk: int) -> list[float]:
    matchups = (
        LLMMatchup.objects
        .filter(content_type=content_type)
        .filter(Q(item_a_id=pk) | Q(item_b_id=pk))
        .order_by('created_at')
        .values('item_a_id', 'elo_a_after', 'elo_b_after')
    )
    history = [DEFAULT_ELO_RATING]
    for m in matchups:
        history.append(m['elo_a_after'] if m['item_a_id'] == pk else m['elo_b_after'])
    return history


def author_detail(request, pk):
    author = get_object_or_404(Author, pk=pk)
    rank = Author.objects.filter(elo_rating__gt=author.elo_rating).count() + 1
    author_works = list(author.works.order_by('-elo_rating'))
    works_with_rank = [
        {'work': w, 'rank': Work.objects.filter(elo_rating__gt=w.elo_rating).count() + 1}
        for w in author_works
    ]
    history = _get_elo_history('author', pk)
    return render(request, 'author_detail.html', {
        'author': author,
        'rank': rank,
        'works_with_rank': works_with_rank,
        'elo_history_json': json.dumps(history) if len(history) > 1 else '',
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
    history = _get_elo_history('work', pk)
    return render(request, 'work_detail.html', {
        'work': work,
        'rank': rank,
        'works_with_rank': works_with_rank,
        'elo_history_json': json.dumps(history) if len(history) > 1 else '',
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


def _get_top_chart_series(content_type, top_n=10):
    """Return top-N entities by Elo with their full match histories."""
    if content_type == 'author':
        items = list(Author.objects.order_by('-elo_rating')[:top_n])
    else:
        items = list(Work.objects.select_related('author').order_by('-elo_rating')[:top_n])
    pks = [item.pk for item in items]
    matchups = (
        LLMMatchup.objects
        .filter(content_type=content_type)
        .filter(Q(item_a_id__in=pks) | Q(item_b_id__in=pks))
        .order_by('created_at')
        .values('item_a_id', 'item_b_id', 'elo_a_after', 'elo_b_after')
    )
    histories = {pk: [DEFAULT_ELO_RATING] for pk in pks}
    for m in matchups:
        if m['item_a_id'] in histories:
            histories[m['item_a_id']].append(m['elo_a_after'])
        if m['item_b_id'] in histories:
            histories[m['item_b_id']].append(m['elo_b_after'])
    return [
        {
            'name': item.name if content_type == 'author' else item.title,
            'pk': item.pk,
            'history': histories[item.pk],
        }
        for item in items
    ]


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