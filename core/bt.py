"""Bradley–Terry fit over LLMMatchup history.

Unlike the Elo ladders, the fit uses every matchup at once, so the result does
not depend on the order games were played, and each item gets a standard
error. Model: P(A beats B) = sigmoid(theta_A - theta_B). A fractional
``result_a`` (0.5 for a split) is used as a soft label. A weak zero-mean
Gaussian prior (sd ``sigma`` on the logit scale) keeps the fit identified and
reins in items that have won or lost every game.

Ratings are reported on the Elo scale: ``1200 + 400/ln(10) * theta``.
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
        se = np.sqrt(np.diag(np.linalg.inv(H)))
    return Fit(list(ids), theta, se)


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
