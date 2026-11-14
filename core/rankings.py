"""Per-model / per-maker ranking queries used by the views."""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from django.db.models import Q

from .models import Author, ItemRating, LLMMatchup, Work
from .ratings import maker_label, model_maker, scope_key, scope_label


def content_type_for(mode: str) -> str:
    return "author" if mode in ("authors", "author") else "work"


def available_scopes(content_type: str) -> list[dict]:
    """Scopes that have ratings for this content type, makers first then models."""
    scopes = (
        ItemRating.objects.filter(content_type=content_type)
        .values_list("scope", flat=True)
        .distinct()
    )
    out = []
    for scope in sorted(set(scopes)):
        kind, _, value = scope.partition(":")
        out.append({
            "scope": scope,
            "key": scope_key(scope),
            "kind": kind,
            "label": scope_label(scope),
            "maker": value if kind == "maker" else model_maker(value),
        })
    out.sort(key=lambda s: (s["kind"] != "maker", s["maker"], s["label"]))
    return out


def scope_nav(content_type: str, current_scope: str | None) -> dict:
    """Context for the judge picker shown above leaderboards."""
    scopes = available_scopes(content_type)
    return {
        "scope_makers": [s for s in scopes if s["kind"] == "maker"],
        "scope_models": [s for s in scopes if s["kind"] == "model"],
        "current_scope": current_scope,
    }


def scoped_ranking(content_type: str, scope: str) -> list:
    """Items rated in ``scope``, best first, each annotated with ``scope_elo``/``scope_games``."""
    rows = list(
        ItemRating.objects.filter(content_type=content_type, scope=scope, games__gt=0)
        .order_by("-elo_rating", "item_id")
        .values_list("item_id", "elo_rating", "games")
    )
    pks = [r[0] for r in rows]
    if content_type == "author":
        lookup = Author.objects.in_bulk(pks)
    else:
        lookup = Work.objects.select_related("author").in_bulk(pks)
    out = []
    for pk, elo, games in rows:
        item = lookup.get(pk)
        if item is None:
            continue
        item.scope_elo = elo
        item.scope_games = games
        out.append(item)
    return out


def item_scope_ratings(content_type: str, pk: int) -> list[dict]:
    """Per-scope ratings for one item, with its position on that scope's leaderboard."""
    rows = ItemRating.objects.filter(content_type=content_type, item_id=pk, games__gt=0)
    out = []
    for r in rows:
        qs = ItemRating.objects.filter(content_type=content_type, scope=r.scope, games__gt=0)
        # Same tie-break as scoped_ranking (-elo_rating, item_id).
        rank = qs.filter(
            Q(elo_rating__gt=r.elo_rating) | Q(elo_rating=r.elo_rating, item_id__lt=pk)
        ).count() + 1
        total = qs.count()
        kind, _, value = r.scope.partition(":")
        out.append({
            "scope": r.scope,
            "key": scope_key(r.scope),
            "kind": kind,
            "label": scope_label(r.scope),
            "maker": maker_label(value if kind == "maker" else model_maker(value)),
            "elo": r.elo_rating,
            "games": r.games,
            "rank": rank,
            "total": total,
        })
    out.sort(key=lambda d: (d["kind"] != "model", d["label"]))
    return [d for d in out if d["kind"] == "model"]


def judge_stats() -> list[dict]:
    """Per-model diagnostics over matchups that carry vote records.

    * decisiveness: mean |score − 0.5| × 2 (0 = always 50/50, 1 = always certain)
    * first-position rate: share of votes picking whichever item was shown first
    * order consistency: share of pairings whose AB and BA votes pick the same item
    * consensus agreement: share of shared pairings where the model's winner
      matches the mean score of the *other* models on that pairing
    """
    matchups = list(
        LLMMatchup.objects.filter(votes__isnull=False).values(
            "content_type", "item_a_id", "item_b_id", "model_used", "score_a", "votes"
        )
    )
    by_model: dict[str, list[dict]] = defaultdict(list)
    by_pair: dict[tuple, dict[str, float]] = defaultdict(dict)
    for m in matchups:
        by_model[m["model_used"]].append(m)
        key = (m["content_type"], m["item_a_id"], m["item_b_id"])
        by_pair[key][m["model_used"]] = m["score_a"]

    stats = []
    for model, ms in by_model.items():
        votes = [v for m in ms for v in m["votes"]]
        first = sum(1 for v in votes if v["verdict"] == 1)
        consistent = total_pairs = 0
        for m in ms:
            ab = [v for v in m["votes"] if v["order"] == "AB"]
            ba = [v for v in m["votes"] if v["order"] == "BA"]
            if ab and ba:
                total_pairs += 1
                if (ab[0]["p_a"] >= 0.5) == (ba[0]["p_a"] >= 0.5):
                    consistent += 1
        agree = shared = 0
        for m in ms:
            key = (m["content_type"], m["item_a_id"], m["item_b_id"])
            others = [s for k, s in by_pair[key].items() if k != model]
            if not others:
                continue
            consensus = sum(others) / len(others)
            if consensus == 0.5 or m["score_a"] == 0.5:
                continue
            shared += 1
            agree += (m["score_a"] > 0.5) == (consensus > 0.5)
        confs = [v["confidence"] for v in votes if v.get("confidence") is not None]
        stats.append({
            "model": model,
            "label": scope_label(f"model:{model}"),
            "maker": maker_label(model_maker(model)),
            "key": scope_key(f"model:{model}"),
            "matchups": len(ms),
            "authors": sum(1 for m in ms if m["content_type"] == "author"),
            "works": sum(1 for m in ms if m["content_type"] == "work"),
            "votes": len(votes),
            "decisiveness": sum(abs(m["score_a"] - 0.5) * 2 for m in ms) / len(ms) if ms else None,
            "mean_confidence": sum(confs) / len(confs) if confs else None,
            "first_rate": first / len(votes) if votes else None,
            "order_consistency": consistent / total_pairs if total_pairs else None,
            "consensus_agreement": agree / shared if shared else None,
        })
    stats.sort(key=lambda s: (s["maker"], s["label"]))
    return stats


def pairwise_agreement(models: list[str]) -> list[list[float | None]]:
    """Matrix of winner agreement rates between models on shared pairings."""
    rows = LLMMatchup.objects.filter(score_a__isnull=False).values_list(
        "content_type", "item_a_id", "item_b_id", "model_used", "score_a"
    )
    by_pair: dict[tuple, dict[str, float]] = defaultdict(dict)
    for ct, a, b, model, score in rows:
        by_pair[(ct, a, b)][model] = score
    idx = {m: i for i, m in enumerate(models)}
    agree = [[0] * len(models) for _ in models]
    total = [[0] * len(models) for _ in models]
    for scores in by_pair.values():
        for m1, m2 in combinations(sorted(scores), 2):
            if m1 not in idx or m2 not in idx:
                continue
            i, j = idx[m1], idx[m2]
            same = (scores[m1] > 0.5) == (scores[m2] > 0.5)
            for x, y in ((i, j), (j, i)):
                total[x][y] += 1
                agree[x][y] += same
    return [
        [None if i == j or not total[i][j] else agree[i][j] / total[i][j] for j in range(len(models))]
        for i in range(len(models))
    ]
