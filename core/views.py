import json

from django.db.models import Count, Q
from django.shortcuts import render, redirect, get_object_or_404
from django.core.paginator import Paginator
from django.urls import reverse
from django.http import Http404, JsonResponse

from .models import Author, Work, LLMMatchup
from .constants import DEFAULT_ELO_RATING
from . import rankings
from .ratings import scope_from_key, scope_key as key_for_scope, scope_label
from .search_index import build_search_index

# Pages are frozen to flat files by build_static, so pagination lives in the
# path (…/page/N/) rather than a query string, which static hosting ignores.
PAGE_SIZE = 50

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


def author_leaderboard(request, page=1):
    authors = Author.objects.order_by('-elo_rating', 'name')
    paginator = Paginator(authors, PAGE_SIZE)
    page_obj = paginator.get_page(page)
    total_comparisons = LLMMatchup.objects.filter(content_type='author').count()
    counts = _matchup_counts('author', [obj.pk for obj in page_obj])
    for obj in page_obj:
        obj.comp_count = counts.get(obj.pk, 0)
        obj.display_elo = obj.elo_rating
    return render(request, "leaderboard.html", {
        "page_obj": page_obj,
        "title": "Authors by Canonicity",
        "mode": "authors",
        "page_items": _pagination_items(page_obj.number, paginator.num_pages),
        "total_comparisons": total_comparisons,
        "current_page": "authors_lb",
        "page_url_prefix": reverse('core:authors_lb') + 'page/',
        **rankings.scope_nav('author', None),
    })


def work_leaderboard(request, page=1):
    works = Work.objects.select_related("author").order_by('-elo_rating', 'title')
    paginator = Paginator(works, PAGE_SIZE)
    page_obj = paginator.get_page(page)
    total_comparisons = LLMMatchup.objects.filter(content_type='work').count()
    counts = _matchup_counts('work', [obj.pk for obj in page_obj])
    for obj in page_obj:
        obj.comp_count = counts.get(obj.pk, 0)
        obj.display_elo = obj.elo_rating
    return render(request, "leaderboard.html", {
        "page_obj": page_obj,
        "title": "Works by Canonicity",
        "mode": "works",
        "page_items": _pagination_items(page_obj.number, paginator.num_pages),
        "total_comparisons": total_comparisons,
        "current_page": "works_lb",
        "page_url_prefix": reverse('core:works_lb') + 'page/',
        **rankings.scope_nav('work', None),
    })


def _scoped_leaderboard_context(mode, scope, items, page_number, overall_ranks):
    content_type = rankings.content_type_for(mode)
    paginator = Paginator(items, PAGE_SIZE)
    page_obj = paginator.get_page(page_number)
    for obj in page_obj:
        obj.comp_count = obj.scope_games
        obj.display_elo = obj.scope_elo
        obj.overall_rank = overall_ranks.get(obj.pk)
    noun = "Authors" if mode == "authors" else "Works"
    return {
        "page_obj": page_obj,
        "title": f"{noun} by Canonicity",
        "subtitle": f"According to {scope_label(scope)}",
        "mode": mode,
        "scoped": True,
        "page_items": _pagination_items(page_obj.number, paginator.num_pages),
        "total_comparisons": sum(i.scope_games for i in items) // 2,
        "current_page": f"{mode}_lb",
        "page_url_prefix": reverse(f'core:{mode}_lb_scoped', kwargs={'scope_key': key_for_scope(scope)}) + 'page/',
        **rankings.scope_nav(content_type, scope),
    }


def scoped_leaderboard(request, mode, scope_key, page=1):
    try:
        scope = scope_from_key(scope_key)
    except ValueError:
        raise Http404("Unknown judge")
    content_type = rankings.content_type_for(mode)
    items = rankings.scoped_ranking(content_type, scope)
    if not items:
        raise Http404("No ratings for this judge")
    model_cls, tiebreak = (Author, 'name') if content_type == 'author' else (Work, 'title')
    order = model_cls.objects.order_by('-elo_rating', tiebreak).values_list('pk', flat=True)
    overall = {pk: i + 1 for i, pk in enumerate(order)}
    ctx = _scoped_leaderboard_context(mode, scope, items, page, overall)
    return render(request, "leaderboard.html", ctx)


def judges(request):
    stats = rankings.judge_stats()
    models = [s["model"] for s in stats]
    matrix = rankings.pairwise_agreement(models)
    return render(request, "judges.html", {
        "stats": stats,
        "agreement_rows": [
            {"label": s["label"], "cells": row} for s, row in zip(stats, matrix)
        ],
        "current_page": "judges",
    })


def search(request):
    return render(request, 'search.html', {'current_page': 'search'})


def search_data(request):
    """The client-side search index; build_static writes it to search-data.json."""
    return JsonResponse(build_search_index(), json_dumps_params={'ensure_ascii': False})


def _leaderboard_rank(obj, tiebreak: str) -> int:
    """1-based leaderboard position, breaking Elo ties the way the leaderboard sorts."""
    value = getattr(obj, tiebreak)
    return type(obj).objects.filter(
        Q(elo_rating__gt=obj.elo_rating)
        | Q(elo_rating=obj.elo_rating, **{f'{tiebreak}__lt': value})
    ).count() + 1


def _get_elo_history(content_type: str, pk: int) -> list[float]:
    matchups = (
        LLMMatchup.objects
        .filter(content_type=content_type)
        .filter(Q(item_a_id=pk) | Q(item_b_id=pk))
        .order_by('created_at')
        .values('item_a_id', 'elo_a_before', 'elo_b_before', 'elo_a_after', 'elo_b_after')
    )
    history = []
    for m in matchups:
        is_a = m['item_a_id'] == pk
        if not history:
            history.append(m['elo_a_before'] if is_a else m['elo_b_before'])
        history.append(m['elo_a_after'] if is_a else m['elo_b_after'])
    return history or [DEFAULT_ELO_RATING]


def author_detail(request, pk):
    author = get_object_or_404(Author, pk=pk)
    rank = _leaderboard_rank(author, 'name')
    author_works = list(author.works.order_by('-elo_rating', 'title'))
    works_with_rank = [
        {'work': w, 'rank': _leaderboard_rank(w, 'title')}
        for w in author_works
    ]
    history = _get_elo_history('author', pk)
    comparison_count = LLMMatchup.objects.filter(
        Q(content_type='author', item_a_id=pk) | Q(content_type='author', item_b_id=pk)
    ).count()
    return render(request, 'author_detail.html', {
        'scope_ratings': rankings.item_scope_ratings('author', pk),
        'author': author,
        'rank': rank,
        'works_with_rank': works_with_rank,
        'elo_history_json': json.dumps(history) if len(history) > 1 else '',
        'comparison_count': comparison_count,
    })


def work_detail(request, pk):
    work = get_object_or_404(Work.objects.select_related('author'), pk=pk)
    rank = _leaderboard_rank(work, 'title')
    author_works = list(work.author.works.order_by('-elo_rating', 'title'))
    works_with_rank = [
        {
            'work': w,
            'rank': _leaderboard_rank(w, 'title'),
            'is_current': w.pk == work.pk,
        }
        for w in author_works
    ]
    history = _get_elo_history('work', pk)
    comparison_count = LLMMatchup.objects.filter(
        Q(content_type='work', item_a_id=pk) | Q(content_type='work', item_b_id=pk)
    ).count()
    return render(request, 'work_detail.html', {
        'scope_ratings': rankings.item_scope_ratings('work', pk),
        'work': work,
        'rank': rank,
        'works_with_rank': works_with_rank,
        'elo_history_json': json.dumps(history) if len(history) > 1 else '',
        'comparison_count': comparison_count,
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
        score = None if m.score_a is None else (m.score_a if is_a else 1 - m.score_a)
        if score == 0.5:
            result = 'Split'
        else:
            result = 'W' if (m.winner == 'A') == is_a else 'L'
        elo_before = m.elo_a_before if is_a else m.elo_b_before
        elo_after  = m.elo_a_after  if is_a else m.elo_b_after
        rows.append({
            'opponent': opponent,
            'opponent_url': reverse(opponent_url_name, kwargs={'pk': opp_pk}),
            'result': result,
            'elo_before': elo_before,
            'elo_after':  elo_after,
            'delta':      elo_after - elo_before,
            'created_at': m.created_at,
            'model_used': m.model_used,
            # Show fractional scores only; 1 / 0.5 / 0 are already in ``result``.
            'score': score if score not in (None, 0.0, 0.5, 1.0) else None,
        })
    return rows


def author_comparisons(request, pk, page=1):
    author = get_object_or_404(Author, pk=pk)
    matchups_qs = LLMMatchup.objects.filter(
        Q(content_type='author', item_a_id=pk) | Q(content_type='author', item_b_id=pk)
    ).order_by('-created_at')
    total_comparisons = matchups_qs.count()
    paginator = Paginator(matchups_qs, PAGE_SIZE)
    page_obj = paginator.get_page(page)
    opp_pks = {m.item_b_id if m.item_a_id == pk else m.item_a_id for m in page_obj}
    lookup = {a.pk: a for a in Author.objects.filter(pk__in=opp_pks)}
    return render(request, 'item_comparisons.html', {
        'item': author,
        'item_name': author.name,
        'detail_url': reverse('core:author_detail', kwargs={'pk': pk}),
        'page_url_prefix': reverse('core:author_comparisons', kwargs={'pk': pk}) + 'page/',
        'rows': _build_comparison_rows(page_obj, pk, lookup, 'core:author_detail'),
        'page_obj': page_obj,
        'page_items': _pagination_items(page_obj.number, paginator.num_pages),
        'total_comparisons': total_comparisons,
    })


def work_comparisons(request, pk, page=1):
    work = get_object_or_404(Work.objects.select_related('author'), pk=pk)
    matchups_qs = LLMMatchup.objects.filter(
        Q(content_type='work', item_a_id=pk) | Q(content_type='work', item_b_id=pk)
    ).order_by('-created_at')
    total_comparisons = matchups_qs.count()
    paginator = Paginator(matchups_qs, PAGE_SIZE)
    page_obj = paginator.get_page(page)
    opp_pks = {m.item_b_id if m.item_a_id == pk else m.item_a_id for m in page_obj}
    lookup = {w.pk: w for w in Work.objects.select_related('author').filter(pk__in=opp_pks)}
    return render(request, 'item_comparisons.html', {
        'item': work,
        'item_name': work.title,
        'detail_url': reverse('core:work_detail', kwargs={'pk': pk}),
        'page_url_prefix': reverse('core:work_comparisons', kwargs={'pk': pk}) + 'page/',
        'rows': _build_comparison_rows(page_obj, pk, lookup, 'core:work_detail'),
        'page_obj': page_obj,
        'page_items': _pagination_items(page_obj.number, paginator.num_pages),
        'total_comparisons': total_comparisons,
    })


def _get_top_chart_series(content_type, top_n=5):
    """Return top-N entities by Elo with their full match histories."""
    if content_type == 'author':
        items = list(Author.objects.order_by('-elo_rating', 'name')[:top_n])
    else:
        items = list(Work.objects.select_related('author').order_by('-elo_rating', 'title')[:top_n])
    pks = [item.pk for item in items]
    matchups = (
        LLMMatchup.objects
        .filter(content_type=content_type)
        .filter(Q(item_a_id__in=pks) | Q(item_b_id__in=pks))
        .order_by('created_at')
        .values('item_a_id', 'item_b_id', 'elo_a_before', 'elo_b_before', 'elo_a_after', 'elo_b_after')
    )
    histories = {pk: [] for pk in pks}
    for m in matchups:
        for pk, before, after in ((m['item_a_id'], m['elo_a_before'], m['elo_a_after']),
                                  (m['item_b_id'], m['elo_b_before'], m['elo_b_after'])):
            if pk in histories:
                if not histories[pk]:
                    histories[pk].append(before)
                histories[pk].append(after)
    for item in items:
        if not histories[item.pk]:
            histories[item.pk] = [item.elo_rating]
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
            # A split (score 0.5: the votes followed presentation order) has no
            # winner; show it in pairing order with A's change.
            split = m.score_a == 0.5
            if m.winner == 'A' or split:
                winner, loser = item_a, item_b
                delta = m.elo_a_after - m.elo_a_before
            else:
                winner, loser = item_b, item_a
                delta = m.elo_b_after - m.elo_b_before
            rows.append({
                'split': split,
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