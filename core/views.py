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

def _chart_json(series):
    keys = ('name', 'by', 'role', 'rank', 'mlaib_rank', 'change', 'history')
    return [{k: s[k] for k in keys} for s in series]


def _chart_groups(series):
    """Legend groups for the grey lines, in display order."""
    labels = [('contender', 'Close behind'), ('riser', 'Biggest risers'), ('faller', 'Biggest fallers')]
    groups = []
    for role, label in labels:
        members = [(i, s) for i, s in enumerate(series) if s['role'] == role]
        if members:
            groups.append({'label': label, 'members': members})
    return groups


def home(request):
    author_series = _get_top_chart_series('author')
    work_series = _get_top_chart_series('work')
    return render(request, 'home.html', {
        'author_series': author_series,
        'work_series': work_series,
        'author_groups': _chart_groups(author_series),
        'work_groups': _chart_groups(work_series),
        'chart_winners': CHART_WINNERS,
        'author_series_json': json.dumps(_chart_json(author_series)),
        'work_series_json': json.dumps(_chart_json(work_series)),
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


def _annotate_intervals(objs) -> None:
    """Position each item's 95% rating interval as percentages for the bar column.

    The scale spans the page's own intervals, so bars compare neighbours.
    """
    fitted = [o for o in objs if getattr(o, 'fit', None)]
    if not fitted:
        return
    lo = min(o.fit.rating - o.fit_ci for o in fitted)
    hi = max(o.fit.rating + o.fit_ci for o in fitted)
    span = (hi - lo) or 1.0
    for o in fitted:
        o.ci_lo, o.ci_hi = o.fit.rating - o.fit_ci, o.fit.rating + o.fit_ci
        o.bar_left = round(100 * (o.fit.rating - o.fit_ci - lo) / span, 2)
        o.bar_width = round(100 * 2 * o.fit_ci / span, 2)
        o.bar_point = round(100 * (o.fit.rating - lo) / span, 2)


def _overall_leaderboard(request, mode, page):
    content_type = rankings.content_type_for(mode)
    items = rankings.fitted_ranking(content_type)
    paginator = Paginator(items, PAGE_SIZE)
    page_obj = paginator.get_page(page)
    counts = _matchup_counts(content_type, [obj.pk for obj in page_obj])
    starting = rankings.mlaib_ranks(content_type)
    for i, obj in enumerate(page_obj):
        obj.comp_count = counts.get(obj.pk, 0)
        obj.display_elo = obj.elo_rating
        rankings.annotate_rank_change(obj, page_obj.start_index() + i, starting)
    _annotate_intervals(page_obj)
    noun = "Authors" if mode == "authors" else "Works"
    return render(request, "leaderboard.html", {
        "page_obj": page_obj,
        "title": f"{noun} by Canonicity",
        "mode": mode,
        "fitted": bool(items and items[0].fit),
        "page_items": _pagination_items(page_obj.number, paginator.num_pages),
        "total_comparisons": LLMMatchup.objects.filter(content_type=content_type).count(),
        "current_page": f"{mode}_lb",
        "page_url_prefix": reverse(f'core:{mode}_lb') + 'page/',
        **rankings.scope_nav(content_type, None),
    })


def author_leaderboard(request, page=1):
    return _overall_leaderboard(request, "authors", page)


def work_leaderboard(request, page=1):
    return _overall_leaderboard(request, "works", page)


def _scoped_leaderboard_context(mode, scope, items, page_number, overall_ranks):
    content_type = rankings.content_type_for(mode)
    paginator = Paginator(items, PAGE_SIZE)
    page_obj = paginator.get_page(page_number)
    starting = rankings.mlaib_ranks(content_type)
    for i, obj in enumerate(page_obj):
        rankings.annotate_rank_change(obj, page_obj.start_index() + i, starting)
        obj.comp_count = obj.fit.games if obj.fit else obj.scope_games
        obj.display_elo = getattr(obj, 'scope_elo', None)
        obj.overall_rank = overall_ranks.get(obj.pk)
    _annotate_intervals(page_obj)
    noun = "Authors" if mode == "authors" else "Works"
    return {
        "page_obj": page_obj,
        "title": f"{noun} by Canonicity",
        "subtitle": f"According to {scope_label(scope)}",
        "mode": mode,
        "scoped": True,
        "fitted": bool(items and items[0].fit),
        "page_items": _pagination_items(page_obj.number, paginator.num_pages),
        "total_comparisons": LLMMatchup.objects.filter(
            content_type=content_type, model_used__in=_scope_models(scope)).count(),
        "current_page": f"{mode}_lb",
        "page_url_prefix": reverse(f'core:{mode}_lb_scoped', kwargs={'scope_key': key_for_scope(scope)}) + 'page/',
        **rankings.scope_nav(content_type, scope),
    }


def _scope_models(scope: str) -> list[str]:
    """Judge model slugs that contribute to a scope."""
    kind, _, value = scope.partition(":")
    if kind == "model":
        return [value]
    from .ratings import model_maker
    used = LLMMatchup.objects.values_list('model_used', flat=True).distinct()
    return [m for m in used if model_maker(m) == value]


def scoped_leaderboard(request, mode, scope_key, page=1):
    try:
        scope = scope_from_key(scope_key)
    except ValueError:
        raise Http404("Unknown judge")
    content_type = rankings.content_type_for(mode)
    items = rankings.fitted_ranking(content_type, scope)
    if not items:
        raise Http404("No ratings for this judge")
    overall = {obj.pk: i + 1 for i, obj in enumerate(rankings.fitted_ranking(content_type))}
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


def _detail_rank(obj, content_type: str, tiebreak: str):
    """(rank, FittedRating or None) on the all-judges leaderboard."""
    fit = rankings.fit_map(content_type, pks=[obj.pk]).get(obj.pk)
    if fit is not None:
        return fit.rank, fit
    return _leaderboard_rank(obj, tiebreak), None


def _starting_rank_context(content_type: str, pk: int, rank: int) -> dict:
    mlaib_rank = rankings.mlaib_ranks(content_type).get(pk)
    return {
        'mlaib_rank': mlaib_rank,
        'rank_change': None if mlaib_rank is None else mlaib_rank - rank,
    }


def _sibling_works(author, content_type='work'):
    """An author's works in leaderboard order, each with its rank and fit."""
    works = list(author.works.all())
    fits = rankings.fit_map(content_type, pks=[w.pk for w in works])
    rows = []
    for w in works:
        fit = fits.get(w.pk)
        rows.append({'work': w, 'fit': fit, 'rank': fit.rank if fit else _leaderboard_rank(w, 'title')})
    rows.sort(key=lambda r: (r['rank'], r['work'].title))
    return rows


def _leaderboard_rank(obj, tiebreak: str) -> int:
    """1-based Elo leaderboard position, breaking ties the way the Elo order sorts."""
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
    rank, fit = _detail_rank(author, 'author', 'name')
    works_with_rank = _sibling_works(author)
    history = _get_elo_history('author', pk)
    comparison_count = LLMMatchup.objects.filter(
        Q(content_type='author', item_a_id=pk) | Q(content_type='author', item_b_id=pk)
    ).count()
    return render(request, 'author_detail.html', {
        'scope_ratings': rankings.item_scope_ratings('author', pk),
        'author': author,
        'rank': rank,
        **_starting_rank_context('author', author.pk, rank),
        'fit': fit,
        'fit_ci': 1.96 * fit.se if fit else None,
        'works_with_rank': works_with_rank,
        'elo_history_json': json.dumps(history) if len(history) > 1 else '',
        'comparison_count': comparison_count,
    })


def work_detail(request, pk):
    work = get_object_or_404(Work.objects.select_related('author'), pk=pk)
    rank, fit = _detail_rank(work, 'work', 'title')
    works_with_rank = _sibling_works(work.author)
    for row in works_with_rank:
        row['is_current'] = row['work'].pk == work.pk
    history = _get_elo_history('work', pk)
    comparison_count = LLMMatchup.objects.filter(
        Q(content_type='work', item_a_id=pk) | Q(content_type='work', item_b_id=pk)
    ).count()
    return render(request, 'work_detail.html', {
        'scope_ratings': rankings.item_scope_ratings('work', pk),
        'work': work,
        'rank': rank,
        **_starting_rank_context('work', work.pk, rank),
        'fit': fit,
        'fit_ci': 1.96 * fit.se if fit else None,
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


# Home-page chart: the leaders plus a field of grey "competition" lines.
CHART_WINNERS = 5
CHART_CONTENDERS = 5   # next places on the fitted leaderboard
CHART_RISERS = 8       # biggest climbers now in the top CHART_WINDOW
CHART_FALLERS = 8      # biggest drops among the MLAIB top CHART_WINDOW
CHART_WINDOW = 60


def _chart_selection(content_type):
    """[(item, role, current_rank, mlaib_rank)] for the home chart.

    role is "winner", "contender", "riser" or "faller". Winners come from the
    fitted leaderboard (the order the leaderboards use); the rest show how
    much the field moved from its MLAIB starting point.
    """
    ranking = rankings.fitted_ranking(content_type)
    current = {obj.pk: i for i, obj in enumerate(ranking, start=1)}
    starting = rankings.mlaib_ranks(content_type)
    by_pk = {obj.pk: obj for obj in ranking}
    change = {pk: starting[pk] - r for pk, r in current.items() if pk in starting}

    chosen, seen = [], set()

    def take(pks, role, limit):
        n = 0
        for pk in pks:
            if n >= limit:
                break
            if pk in seen or pk not in by_pk:
                continue
            seen.add(pk)
            chosen.append((by_pk[pk], role, current[pk], starting.get(pk)))
            n += 1

    order = [obj.pk for obj in ranking]
    take(order[:CHART_WINNERS], "winner", CHART_WINNERS)
    take(order[CHART_WINNERS:], "contender", CHART_CONTENDERS)
    risers = sorted((pk for pk in change if current[pk] <= CHART_WINDOW and change[pk] > 0),
                    key=lambda pk: (-change[pk], current[pk]))
    take(risers, "riser", CHART_RISERS)
    fallers = sorted((pk for pk in change if starting[pk] <= CHART_WINDOW and change[pk] < 0),
                     key=lambda pk: (change[pk], starting[pk]))
    take(fallers, "faller", CHART_FALLERS)
    return chosen


def _get_top_chart_series(content_type):
    """Chart series (winners first) with each item's full Elo history."""
    selection = _chart_selection(content_type)
    pks = [item.pk for item, *_ in selection]
    matchups = (
        LLMMatchup.objects
        .filter(content_type=content_type)
        .filter(Q(item_a_id__in=pks) | Q(item_b_id__in=pks))
        .order_by('created_at', 'id')
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
    series = []
    for item, role, rank, mlaib_rank in selection:
        history = histories[item.pk] or [item.elo_rating]
        series.append({
            'name': item.name if content_type == 'author' else item.title,
            'by': item.author.name if content_type == 'work' else None,
            'pk': item.pk,
            'role': role,
            'rank': rank,
            'mlaib_rank': mlaib_rank,
            'change': None if mlaib_rank is None else mlaib_rank - rank,
            # Rounded: the page embeds every point of ~30 histories.
            'history': [round(v, 1) for v in history],
        })
    return series


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