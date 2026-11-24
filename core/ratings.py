"""Rating scopes and Elo application across the aggregate and per-model ladders.

Every matchup updates three ladders:

* the aggregate rating on ``Author.elo_rating`` / ``Work.elo_rating``
  (all models pooled),
* ``ItemRating`` with scope ``model:<slug>`` (just that model), and
* ``ItemRating`` with scope ``maker:<maker>`` (all models from that maker).

Matchups judged under a non-default system prompt (an experiment) update none
of these, only their own ``model:<slug>@<prompt>`` ladder.

Each ladder starts an item at its prior (``mlaib_elo`` if set, otherwise
``DEFAULT_ELO_RATING``), so the per-model ladders begin from the same place as
the aggregate one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import DEFAULT_ELO_RATING
from .elo import update as elo_update


# ── scope helpers ─────────────────────────────────────────────────────────────

def model_maker(model_slug: str) -> str:
    """``"anthropic/claude-sonnet-5.5:batch"`` → ``"anthropic"``."""
    return model_slug.split("/", 1)[0] if "/" in model_slug else "openai"


def base_model_slug(model_slug: str) -> str:
    """Strip routing/catalog variants: ``"openai/gpt-6-luna:batch"`` → ``"openai/gpt-6-luna"``."""
    return model_slug.split(":", 1)[0]


def scopes_for(model_slug: str, variant: str | None = None) -> list[str]:
    """Scoped ladders a matchup feeds; ``variant`` is a non-default prompt's name."""
    slug = base_model_slug(model_slug)
    if variant:
        return [f"model:{slug}@{variant}"]
    return [f"model:{slug}", f"maker:{model_maker(slug)}"]


def is_variant_scope(scope: str) -> bool:
    return "@" in scope


def scope_key(scope: str) -> str:
    """URL-safe key for a scope: ``model:openai/gpt-6-luna`` → ``model-openai--gpt-6-luna``."""
    kind, _, value = scope.partition(":")
    return f"{kind}-{value.replace('/', '--')}"


def scope_from_key(key: str) -> str:
    kind, _, value = key.partition("-")
    if kind not in ("model", "maker") or not value:
        raise ValueError(f"Bad scope key: {key!r}")
    return f"{kind}:{value.replace('--', '/')}"


_MAKER_LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "z-ai": "Z.ai",
    "deepseek": "DeepSeek",
    "minimax": "MiniMax",
    "meta-llama": "Meta",
    "mistralai": "Mistral",
    "qwen": "Qwen",
    "x-ai": "xAI",
    "moonshotai": "Moonshot",
    "typesafe": "TypeSafe",
}


def scope_label(scope: str) -> str:
    kind, _, value = scope.partition(":")
    if kind == "maker":
        return _MAKER_LABELS.get(value, value)
    return value.split("/", 1)[-1]


def maker_label(maker: str) -> str:
    return _MAKER_LABELS.get(maker, maker)


def prior_rating(item) -> float:
    return item.mlaib_elo if item.mlaib_elo is not None else DEFAULT_ELO_RATING


# ── applying matchups ─────────────────────────────────────────────────────────

@dataclass
class MatchupUpdate:
    elo_a_before: float
    elo_b_before: float
    elo_a_after: float
    elo_b_after: float


class RatingBook:
    """In-memory view of all ladders for one content type.

    Load once, apply many matchups, then ``save()`` writes everything back in
    bulk. Aggregate ratings are read from / written to the item objects passed
    in; scoped ratings live in ``ItemRating`` rows.
    """

    def __init__(self, content_type: str, items: dict[int, object]):
        from .models import ItemRating

        self.content_type = content_type
        self.items = items  # pk → Author/Work (aggregate rating on .elo_rating)
        self.scoped: dict[tuple[str, int], ItemRating] = {
            (r.scope, r.item_id): r
            for r in ItemRating.objects.filter(content_type=content_type)
        }
        self._dirty_items: set[int] = set()
        self._dirty_scoped: set[tuple[str, int]] = set()

    def _scoped(self, scope: str, pk: int):
        from .models import ItemRating

        key = (scope, pk)
        row = self.scoped.get(key)
        if row is None:
            row = ItemRating(
                content_type=self.content_type,
                item_id=pk,
                scope=scope,
                elo_rating=prior_rating(self.items[pk]),
                games=0,
            )
            self.scoped[key] = row
        return row

    def apply(self, pk_a: int, pk_b: int, score_a: float, model_slug: str,
              variant: str | None = None) -> MatchupUpdate:
        """Apply one result to all its ladders; return the aggregate before/after.

        With ``variant`` (a non-default prompt) only that prompt's model ladder
        moves, and its before/after are returned instead.
        """
        result = None
        if not variant:
            a, b = self.items[pk_a], self.items[pk_b]
            before_a, before_b = a.elo_rating, b.elo_rating
            a.elo_rating, b.elo_rating = elo_update(before_a, before_b, score_a)
            self._dirty_items.update((pk_a, pk_b))
            result = MatchupUpdate(before_a, before_b, a.elo_rating, b.elo_rating)

        for scope in scopes_for(model_slug, variant):
            ra, rb = self._scoped(scope, pk_a), self._scoped(scope, pk_b)
            before_a, before_b = ra.elo_rating, rb.elo_rating
            ra.elo_rating, rb.elo_rating = elo_update(before_a, before_b, score_a)
            ra.games += 1
            rb.games += 1
            self._dirty_scoped.update(((scope, pk_a), (scope, pk_b)))
            if result is None:
                result = MatchupUpdate(before_a, before_b, ra.elo_rating, rb.elo_rating)

        return result

    def save(self) -> None:
        from .models import Author, ItemRating, Work

        model_cls = Author if self.content_type == "author" else Work
        model_cls.objects.bulk_update([self.items[pk] for pk in self._dirty_items], ["elo_rating"])
        rows = [self.scoped[k] for k in self._dirty_scoped]
        new = [r for r in rows if r.pk is None]
        old = [r for r in rows if r.pk is not None]
        ItemRating.objects.bulk_create(new)
        ItemRating.objects.bulk_update(old, ["elo_rating", "games"])
        self._dirty_items.clear()
        self._dirty_scoped.clear()


def rebuild_ratings(content_type: str) -> int:
    """Recompute every ladder for one content type by replaying LLMMatchup history.

    Resets aggregate ratings to the prior, deletes all ``ItemRating`` rows, and
    replays matchups in ``created_at`` order, rewriting each matchup's
    before/after columns. Returns the number of matchups replayed.
    """
    from django.db import transaction

    from .models import Author, ItemRating, LLMMatchup, Work

    model_cls = Author if content_type == "author" else Work
    with transaction.atomic():
        items = {i.pk: i for i in model_cls.objects.all()}
        for item in items.values():
            item.elo_rating = prior_rating(item)
        ItemRating.objects.filter(content_type=content_type).delete()
        book = RatingBook(content_type, items)
        book._dirty_items.update(items)

        matchups = list(LLMMatchup.all_objects.filter(content_type=content_type)
                        .select_related("system_prompt").order_by("created_at", "id"))
        for m in matchups:
            if m.item_a_id not in items or m.item_b_id not in items:
                continue
            variant = m.system_prompt.variant if m.system_prompt else None
            upd = book.apply(m.item_a_id, m.item_b_id, m.result_a, m.model_used, variant)
            m.elo_a_before, m.elo_b_before = upd.elo_a_before, upd.elo_b_before
            m.elo_a_after, m.elo_b_after = upd.elo_a_after, upd.elo_b_after
        LLMMatchup.objects.bulk_update(
            matchups, ["elo_a_before", "elo_b_before", "elo_a_after", "elo_b_after"], batch_size=1000
        )
        book.save()
    return len(matchups)
