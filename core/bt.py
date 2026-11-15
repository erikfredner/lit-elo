"""Bradley–Terry fit over LLMMatchup history.

Unlike the Elo ladders, the fit uses every matchup at once, so the result does
not depend on the order games were played, and each item gets a standard
error. Model: P(A beats B) = sigmoid(theta_A - theta_B). A fractional
``result_a`` (0.5 for a split) is used as a soft label. A weak zero-mean
Gaussian prior (sd ``sigma`` on the logit scale) keeps the fit identified and
reins in items that have won or lost every game.

Ratings are reported on the Elo scale: ``1200 + 400/ln(10) * theta``.
Uncertainty comes from the Laplace approximation (inverse Hessian at the
optimum): ``se`` per item, and rank intervals from draws of the joint
posterior, so correlated items (ones mostly compared with each other) get
honest rank ranges.

``refit(content_type)`` fits every scope (all judges pooled, each model, each
maker) and replaces the ``FittedRating`` rows that the site displays.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize

ELO_SCALE = 400 / np.log(10)
DEFAULT_SIGMA = 2.0


@dataclass
class Fit:
    ids: list[int]          # item pks, index-aligned with theta/se
    theta: np.ndarray       # logit-scale strengths
    se: np.ndarray | None   # standard errors (None if not computed)
    cov: np.ndarray | None = None  # posterior covariance (with_se only)

    def rating(self) -> dict[int, float]:
        return {pk: 1200 + ELO_SCALE * t for pk, t in zip(self.ids, self.theta)}

    def top(self, n: int) -> list[int]:
        order = np.argsort(-self.theta, kind="stable")[:n]
        return [self.ids[k] for k in order]


def fit(ids: list[int], games: list[tuple[int, int, float]], sigma: float = DEFAULT_SIGMA,
        with_se: bool = False) -> Fit:
    """Fit strengths for ``ids`` from ``(pk_a, pk_b, score_a)`` games."""
    idx = {pk: k for k, pk in enumerate(ids)}
    n = len(ids)
    games = [g for g in games if g[0] in idx and g[1] in idx]
    ia = np.array([idx[a] for a, _, _ in games], dtype=int)
    ib = np.array([idx[b] for _, b, _ in games], dtype=int)
    s = np.array([sc for _, _, sc in games], dtype=float)

    def objective(theta):
        p = 1 / (1 + np.exp(-(theta[ia] - theta[ib])))
        nll = -np.sum(s * np.log(p + 1e-12) + (1 - s) * np.log(1 - p + 1e-12))
        nll += np.sum(theta**2) / (2 * sigma**2)
        g = p - s
        grad = np.bincount(ia, g, n) - np.bincount(ib, g, n) + theta / sigma**2
        return nll, grad

    theta = minimize(objective, np.zeros(n), jac=True, method="L-BFGS-B").x
    se = None
    if with_se:
        p = 1 / (1 + np.exp(-(theta[ia] - theta[ib])))
        w = p * (1 - p)
        H = np.diag(np.bincount(ia, w, n) + np.bincount(ib, w, n) + 1 / sigma**2)
        np.add.at(H, (ia, ib), -w)
        np.add.at(H, (ib, ia), -w)
        cov = np.linalg.inv(H)
        cov = (cov + cov.T) / 2
        se = np.sqrt(np.diag(cov))
        return Fit(list(ids), theta, se, cov)
    return Fit(list(ids), theta, se)


def rank_intervals(f: Fit, draws: int = 2000, level: float = 0.95, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """1-based (lo, hi) leaderboard positions covering ``level`` of posterior draws."""
    if f.cov is None:
        raise ValueError("fit(..., with_se=True) is required for rank intervals")
    rng = np.random.default_rng(seed)
    n = len(f.ids)
    L = np.linalg.cholesky(f.cov + 1e-10 * np.eye(n))
    samples = f.theta + rng.standard_normal((draws, n)) @ L.T
    order = np.argsort(-samples, axis=1)
    ranks = np.empty_like(order)
    ranks[np.arange(draws)[:, None], order] = np.arange(1, n + 1)
    tail = (1 - level) / 2 * 100
    lo = np.floor(np.percentile(ranks, tail, axis=0)).astype(int)
    hi = np.ceil(np.percentile(ranks, 100 - tail, axis=0)).astype(int)
    return lo, hi


def fit_content_type(content_type: str, model_slug: str | None = None, **kw) -> Fit:
    """Fit one content type from stored matchups (all judges pooled unless ``model_slug``)."""
    from .models import Author, LLMMatchup, Work

    model_cls = Author if content_type == "author" else Work
    ids = list(model_cls.objects.values_list("pk", flat=True))
    qs = LLMMatchup.objects.filter(content_type=content_type)
    if model_slug:
        qs = qs.filter(model_used=model_slug)
    games = [(m.item_a_id, m.item_b_id, m.result_a) for m in qs.only("item_a_id", "item_b_id", "score_a", "winner")]
    return fit(ids, games, **kw)


def refit(content_type: str, sigma: float = DEFAULT_SIGMA) -> dict[str, int]:
    """Refit every scope for one content type and replace its FittedRating rows.

    Returns {scope: items fitted}.
    """
    from collections import defaultdict

    from django.db import transaction

    from .models import FittedRating, LLMMatchup
    from .ratings import scopes_for

    games_by_scope: dict[str, list[tuple[int, int, float]]] = defaultdict(list)
    qs = LLMMatchup.objects.filter(content_type=content_type).only(
        "item_a_id", "item_b_id", "score_a", "winner", "model_used")
    for m in qs.iterator():
        g = (m.item_a_id, m.item_b_id, m.result_a)
        for scope in ["all", *scopes_for(m.model_used)]:
            games_by_scope[scope].append(g)

    rows, summary = [], {}
    for scope, games in sorted(games_by_scope.items()):
        played: dict[int, int] = defaultdict(int)
        for a, b, _ in games:
            played[a] += 1
            played[b] += 1
        ids = sorted(played)
        if len(ids) < 2:
            continue
        f = fit(ids, games, sigma=sigma, with_se=True)
        lo, hi = rank_intervals(f)
        order = np.argsort(-f.theta, kind="stable")
        rank = np.empty(len(ids), dtype=int)
        rank[order] = np.arange(1, len(ids) + 1)
        for k, pk in enumerate(ids):
            rows.append(FittedRating(
                content_type=content_type, scope=scope, item_id=pk,
                rating=1200 + ELO_SCALE * f.theta[k], se=ELO_SCALE * f.se[k],
                rank=int(rank[k]), rank_lo=int(min(lo[k], rank[k])), rank_hi=int(max(hi[k], rank[k])),
                games=played[pk],
            ))
        summary[scope] = len(ids)
    with transaction.atomic():
        FittedRating.objects.filter(content_type=content_type).delete()
        FittedRating.objects.bulk_create(rows, batch_size=2000)
    return summary


def refit_all() -> dict[str, dict[str, int]]:
    return {ct: refit(ct) for ct in ("author", "work")}
