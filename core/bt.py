"""Bradley–Terry fit over LLMMatchup history.

Unlike the Elo ladders, the fit uses every matchup at once, so the result does
not depend on the order games were played, and each item gets a standard
error. Model: P(A beats B) = sigmoid(theta_A - theta_B). A fractional
``result_a`` (0.5 for a split) is used as a soft label.

Prior. Each item starts from what its MLAIB record count predicts:
``theta_i ~ N(b * x_i, tau^2)`` with ``x_i = (mlaib_elo_i - 1200) / ELO_SCALE``
(0 for an item without a count). ``b``, how much of the MLAIB spread the
judges bear out, is fitted with the strengths (its own prior N(1, 1));
``tau``, how far judges stray from MLAIB, is estimated by EM. Both come from
the games themselves (empirical Bayes), per scope. The prior matters for items
with few games and fades as games accumulate; an item with no games sits at
its prior. ``fit(..., prior=None)`` is the plain fit with a zero-mean prior
of sd ``sigma``, which keeps the fit identified and reins in items that have
won or lost every game.

Ratings are reported on the Elo scale: ``1200 + 400/ln(10) * theta``.
Uncertainty comes from the Laplace approximation (inverse Hessian at the
optimum, ``b`` and ``tau`` held fixed): ``se`` per item, and rank intervals
from draws of the joint posterior, so correlated items (ones mostly compared
with each other) get honest rank ranges.

``refit(content_type)`` fits every scope (all judges pooled, each model, each
maker, and each ``model:<slug>@<prompt>`` prompt experiment) and replaces the ``FittedRating`` rows that the site displays.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize

ELO_SCALE = 400 / np.log(10)
DEFAULT_SIGMA = 2.0
_MIN_EB_ITEMS = 10      # fewer played items than this: keep tau = sigma rather than estimate it
_EM_MAX_ITER = 100
_EM_TOL = 1e-3          # on tau (logit scale)


@dataclass
class Fit:
    ids: list[int]          # item pks, index-aligned with theta/se
    theta: np.ndarray       # logit-scale strengths
    se: np.ndarray | None   # standard errors (None if not computed)
    cov: np.ndarray | None = None  # posterior covariance (with_se only)
    slope: float | None = None     # b: MLAIB prior mean = b * x (prior fits only)
    tau: float | None = None       # prior sd around it, logit scale (prior fits only)

    def rating(self) -> dict[int, float]:
        return {pk: 1200 + ELO_SCALE * t for pk, t in zip(self.ids, self.theta)}

    def top(self, n: int) -> list[int]:
        order = np.argsort(-self.theta, kind="stable")[:n]
        return [self.ids[k] for k in order]


def fit(ids: list[int], games: list[tuple[int, int, float]], sigma: float = DEFAULT_SIGMA,
        with_se: bool = False, prior=None) -> Fit:
    """Fit strengths for ``ids`` from ``(pk_a, pk_b, score_a)`` games.

    ``prior``: per-item MLAIB prior on the logit scale, index-aligned with
    ``ids`` (see the module docstring); None for a zero-mean prior of sd ``sigma``.
    """
    idx = {pk: k for k, pk in enumerate(ids)}
    n = len(ids)
    games = [g for g in games if g[0] in idx and g[1] in idx]
    ia = np.array([idx[a] for a, _, _ in games], dtype=int)
    ib = np.array([idx[b] for _, b, _ in games], dtype=int)
    s = np.array([sc for _, _, sc in games], dtype=float)
    x = None if prior is None else np.asarray(prior, dtype=float)

    def map_fit(tau: float, z0: np.ndarray) -> np.ndarray:
        """MAP of theta (and b, with a prior), for prior sd ``tau``."""
        def objective(z):
            theta = z[:n]
            r = theta if x is None else theta - z[n] * x
            p = 1 / (1 + np.exp(-(theta[ia] - theta[ib])))
            nll = -np.sum(s * np.log(p + 1e-12) + (1 - s) * np.log(1 - p + 1e-12))
            nll += np.sum(r**2) / (2 * tau**2)
            g = p - s
            grad = np.bincount(ia, g, n) - np.bincount(ib, g, n) + r / tau**2
            if x is None:
                return nll, grad
            nll += (z[n] - 1) ** 2 / 2
            return nll, np.r_[grad, -np.sum(x * r) / tau**2 + (z[n] - 1)]
        return minimize(objective, z0, jac=True, method="L-BFGS-B").x

    def hessian(theta: np.ndarray, tau: float) -> np.ndarray:
        p = 1 / (1 + np.exp(-(theta[ia] - theta[ib])))
        w = p * (1 - p)
        H = np.diag(np.bincount(ia, w, n) + np.bincount(ib, w, n) + 1 / tau**2)
        np.add.at(H, (ia, ib), -w)
        np.add.at(H, (ib, ia), -w)
        return H

    tau, slope = sigma, None
    if x is None:
        theta = map_fit(tau, np.zeros(n))
    else:
        z = map_fit(tau, np.r_[x, 1.0])
        played = (np.bincount(ia, minlength=n) + np.bincount(ib, minlength=n)) > 0
        if played.sum() >= _MIN_EB_ITEMS:
            for _ in range(_EM_MAX_ITER):
                # EM: tau^2 = mean over played items of E[(theta - b x)^2] under the Laplace posterior.
                var = np.diag(np.linalg.inv(hessian(z[:n], tau)))
                new = float(np.sqrt(np.mean((z[:n] - z[n] * x)[played] ** 2 + var[played])))
                done = abs(new - tau) < _EM_TOL
                tau = new
                z = map_fit(tau, z)
                if done:
                    break
        theta, slope = z[:n], float(z[n])

    if not with_se:
        return Fit(list(ids), theta, None, slope=slope, tau=None if x is None else tau)
    cov = np.linalg.inv(hessian(theta, tau))
    cov = (cov + cov.T) / 2
    return Fit(list(ids), theta, np.sqrt(np.diag(cov)), cov, slope=slope, tau=None if x is None else tau)


def prior_vector(content_type: str, ids: list[int]) -> np.ndarray:
    """Logit-scale MLAIB prior for ``ids``: ``(mlaib_elo - 1200) / ELO_SCALE``, 0 without one."""
    from .models import Author, Work

    model_cls = Author if content_type == "author" else Work
    elo = dict(model_cls.objects.filter(pk__in=ids).values_list("pk", "mlaib_elo"))
    return np.array([0.0 if elo.get(pk) is None else (elo[pk] - 1200) / ELO_SCALE for pk in ids])


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


def fit_content_type(content_type: str, model_slug: str | None = None, prior: bool = True, **kw) -> Fit:
    """Fit every item of one content type from stored matchups (all judges pooled unless ``model_slug``).

    With ``prior`` (default) the MLAIB prior applies, so items with no games are
    placed by it; ``prior=False`` gives the zero-mean fit, e.g. to measure how
    ratings relate to MLAIB counts without assuming it.
    """
    from .models import Author, LLMMatchup, Work

    model_cls = Author if content_type == "author" else Work
    ids = list(model_cls.objects.values_list("pk", flat=True))
    qs = LLMMatchup.objects.filter(content_type=content_type)
    if model_slug:
        qs = qs.filter(model_used=model_slug)
    games = [(m.item_a_id, m.item_b_id, m.result_a) for m in qs.only("item_a_id", "item_b_id", "score_a", "winner")]
    return fit(ids, games, prior=prior_vector(content_type, ids) if prior else None, **kw)


def refit(content_type: str, sigma: float = DEFAULT_SIGMA) -> dict[str, int]:
    """Refit every scope for one content type and replace its FittedRating rows.

    Returns {scope: items fitted}.
    """
    from collections import defaultdict

    from django.db import transaction

    from .models import FittedRating, LLMMatchup, SystemPrompt
    from .ratings import scopes_for

    variants = {p.pk: p.variant for p in SystemPrompt.objects.all()}
    games_by_scope: dict[str, list[tuple[int, int, float]]] = defaultdict(list)
    qs = LLMMatchup.all_objects.filter(content_type=content_type).only(
        "item_a_id", "item_b_id", "score_a", "winner", "model_used", "system_prompt_id")
    for m in qs.iterator():
        g = (m.item_a_id, m.item_b_id, m.result_a)
        variant = variants.get(m.system_prompt_id)
        scopes = scopes_for(m.model_used, variant)
        for scope in scopes if variant else ["all", *scopes]:
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
        f = fit(ids, games, sigma=sigma, with_se=True, prior=prior_vector(content_type, ids))
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
