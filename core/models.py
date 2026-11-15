from django.db import models
from .constants import DEFAULT_ELO_RATING

class Author(models.Model):
    name            = models.CharField(max_length=128, unique=True, db_index=True)
    birth_year      = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    death_year      = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    elo_rating          = models.FloatField(default=DEFAULT_ELO_RATING, db_index=True)  # starter ELO
    mlaib_record_count  = models.PositiveIntegerField(null=True, blank=True)
    mlaib_elo           = models.FloatField(null=True, blank=True)
    viaf_id             = models.CharField(max_length=64, blank=True, db_index=True)
    wikipedia_url       = models.URLField(blank=True, default="")


    class Meta:
        ordering = ["-elo_rating", "name"]
        indexes = [
            models.Index(fields=['-elo_rating', 'name']),
            models.Index(fields=['birth_year', 'death_year']),
        ]

    def __str__(self):
        return str(self.name)
    
    def get_google_search_url(self):
        """Return a Google search URL for this author"""
        from urllib.parse import quote
        search_terms = [self.name]
        if self.birth_year:
            search_terms.append(str(self.birth_year))
        query = " ".join(search_terms)
        return f"https://www.google.com/search?q={quote(query)}&udm=14"

    def get_viaf_url(self):
        """Return the VIAF record URL for this author, or '' if no viaf_id."""
        if self.viaf_id:
            return f"https://viaf.org/viaf/{self.viaf_id}"
        return ""


class Work(models.Model):
    title           = models.CharField(max_length=256, db_index=True)
    author          = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="works", db_index=True)
    publication_year= models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    form            = models.CharField(max_length=64, blank=True, help_text="e.g., novel, poem, play", db_index=True)
    elo_rating          = models.FloatField(default=DEFAULT_ELO_RATING, db_index=True)
    mlaib_record_count  = models.PositiveIntegerField(null=True, blank=True)
    mlaib_elo           = models.FloatField(null=True, blank=True)


    class Meta:
        unique_together = [("title", "author")]
        ordering = ["-elo_rating", "title"]
        indexes = [
            models.Index(fields=['-elo_rating', 'title']),
            models.Index(fields=['author', '-elo_rating']),
            models.Index(fields=['form', '-elo_rating']),
            models.Index(fields=['publication_year']),
        ]

    def __str__(self):
        return f"{self.title} ({self.author.name})"
    
    def get_google_search_url(self):
        """Return a Google search URL for this work"""
        from urllib.parse import quote
        search_terms = [self.title, self.author.name]
        query = " ".join(search_terms)
        return f"https://www.google.com/search?q={quote(query)}&udm=14"


class LLMMatchup(models.Model):
    """Persists the result of a single LLM canonicity judgment."""

    content_type = models.CharField(
        max_length=10,
        choices=[('author', 'Author'), ('work', 'Work')],
        db_index=True,
    )
    item_a_id = models.PositiveIntegerField(db_index=True)  # Author or Work PK
    item_b_id = models.PositiveIntegerField(db_index=True)
    winner = models.CharField(max_length=1, choices=[('A', 'A wins'), ('B', 'B wins')])
    elo_a_before = models.FloatField()
    elo_b_before = models.FloatField()
    elo_a_after = models.FloatField()
    elo_b_after = models.FloatField()
    model_used = models.CharField(max_length=128, db_index=True)
    # Result for A in [0, 1]: mean P(A wins) across votes. Binary votes count as
    # 1/0, so two order-swapped votes give 1, 0.5 or 0. Null for legacy
    # run_llm_elo matchups, where the result is implied by ``winner``.
    score_a = models.FloatField(null=True, blank=True)
    # Raw per-vote records: [{"order": "AB"|"BA", "verdict": 1|2,
    # "confidence": 50-100|None, "p_logprob": float|None, "p_a": float}, ...]
    # (confidence is None for binary votes, from November 2026 on)
    votes = models.JSONField(null=True, blank=True)
    batch = models.ForeignKey("LLMBatch", null=True, blank=True, on_delete=models.SET_NULL, related_name="matchups")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    @property
    def result_a(self) -> float:
        """Score for item A used in the Elo update."""
        if self.score_a is not None:
            return self.score_a
        return 1.0 if self.winner == "A" else 0.0

    class Meta:
        indexes = [
            models.Index(fields=['content_type', 'item_a_id']),
            models.Index(fields=['content_type', 'item_b_id']),
            models.Index(fields=['content_type', '-created_at']),
        ]




class LLMBatch(models.Model):
    """One OpenRouter batch submission: a single model judging one content type.

    ``manifest`` maps each request's ``custom_id`` to the pairing and the order
    the two items were shown in, so results can be applied once the batch
    finishes (possibly hours after submission).
    """

    STATUS_CHOICES = [
        ("submitted", "Submitted"),
        ("validating", "Validating"),
        ("in_progress", "In progress"),
        ("finalizing", "Finalizing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
        ("expired", "Expired"),
        ("cancelled", "Cancelled"),
    ]

    openrouter_id = models.CharField(max_length=128, unique=True)
    model_slug = models.CharField(max_length=128, db_index=True)
    content_type = models.CharField(max_length=10, choices=[("author", "Author"), ("work", "Work")])
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="submitted", db_index=True)
    provider = models.CharField(max_length=64, blank=True)
    request_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    votes_per_pairing = models.PositiveSmallIntegerField(default=2)
    manifest = models.JSONField(default=dict)
    request_options = models.JSONField(default=dict)
    cost_usd = models.FloatField(null=True, blank=True)
    usage = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    applied = models.BooleanField(default=False, db_index=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"{self.model_slug} {self.content_type} {self.openrouter_id} ({self.status})"


class ItemRating(models.Model):
    """Elo rating of an Author or Work within one scope.

    Scopes are ``"model:<openrouter slug>"`` (e.g. ``model:openai/gpt-6-luna``)
    and ``"maker:<maker>"`` (e.g. ``maker:openai``). The aggregate rating across
    all models stays on ``Author.elo_rating`` / ``Work.elo_rating``.
    """

    content_type = models.CharField(max_length=10, choices=[("author", "Author"), ("work", "Work")])
    item_id = models.PositiveIntegerField()
    scope = models.CharField(max_length=160)
    elo_rating = models.FloatField(default=DEFAULT_ELO_RATING)
    games = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("content_type", "scope", "item_id")]
        indexes = [
            models.Index(fields=["content_type", "scope", "-elo_rating"]),
            models.Index(fields=["content_type", "item_id"]),
        ]

    def __str__(self):
        return f"{self.content_type}:{self.item_id} [{self.scope}] {self.elo_rating:.0f}"


class FittedRating(models.Model):
    """Bradley–Terry rating of an Author or Work, refit from all matchups.

    ``scope`` is ``"all"`` (every judge pooled) or an ``ItemRating`` scope
    (``model:<slug>`` / ``maker:<maker>``). ``rating`` is on the Elo scale and
    ``se`` is its standard error in the same units. ``rank_lo``/``rank_hi``
    bound the item's leaderboard position (95% of posterior draws). Rows are
    replaced wholesale by ``core.bt.refit``; only items with games appear.
    """

    content_type = models.CharField(max_length=10, choices=[("author", "Author"), ("work", "Work")])
    scope = models.CharField(max_length=160)
    item_id = models.PositiveIntegerField()
    rating = models.FloatField()
    se = models.FloatField()
    rank = models.PositiveIntegerField()
    rank_lo = models.PositiveIntegerField()
    rank_hi = models.PositiveIntegerField()
    games = models.PositiveIntegerField()

    class Meta:
        unique_together = [("content_type", "scope", "item_id")]
        indexes = [models.Index(fields=["content_type", "scope", "rank"])]

    def __str__(self):
        return f"{self.content_type}:{self.item_id} [{self.scope}] {self.rating:.0f} ±{1.96 * self.se:.0f}"
