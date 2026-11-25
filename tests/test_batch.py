"""Tests for OpenRouter batch judging: vote parsing, scoring, ladders, collection, per-model views."""

import json
from io import StringIO
from unittest.mock import patch

import numpy as np
import pytest
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core import judging
from core.models import Author, ItemRating, LLMBatch, LLMMatchup, Work
from core.ratings import RatingBook, rebuild_ratings, scope_from_key, scope_key, scopes_for


# ── judging (pure) ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("content,expected", [
    ('{"more_canonical": 1, "confidence": 80}', (1, 80)),
    ('```json\n{"more_canonical": 2, "confidence": 99}\n```', (2, 99)),
    ('Thinking... {"more_canonical": 2, "confidence": "70"}', (2, 70)),
    ('{"verdict": 1}', (1, None)),
    ('{"more_canonical": 1, "confidence": 120}', (1, 100)),
    ("2", (2, None)),
    ("", None),
    ("I cannot decide", None),
    ('{"more_canonical": 3, "confidence": 60}', None),
])
def test_parse_judgment(content, expected):
    assert judging.parse_judgment(content) == expected


def test_vote_record_uses_confidence_and_order():
    # Shown as AB, picks 1 (=A) with 80% → p_a 0.8
    assert judging.vote_record("AB", '{"more_canonical": 1, "confidence": 80}')["p_a"] == 0.8
    # Shown as BA, picks 1 (=B) with 80% → p_a 0.2
    assert judging.vote_record("BA", '{"more_canonical": 1, "confidence": 80}')["p_a"] == pytest.approx(0.2)
    # Shown as BA, picks 2 (=A) with 100% → clamped to P_MAX
    assert judging.vote_record("BA", '{"more_canonical": 2, "confidence": 100}')["p_a"] == judging.P_MAX


def test_binary_votes_are_unclamped_and_order_aware():
    assert judging.vote_record("AB", '{"more_canonical": 1}')["p_a"] == 1.0
    assert judging.vote_record("BA", '{"more_canonical": 1}')["p_a"] == 0.0
    assert judging.vote_record("BA", "2")["p_a"] == 1.0
    # Model follows presentation order (picks "1" both times) → 0.5
    votes = [judging.vote_record("AB", "1"), judging.vote_record("BA", "1")]
    assert judging.score_from_votes(votes) == 0.5


def test_binary_prompt_and_schema():
    assert "confidence" not in judging.OUTPUT_INSTRUCTIONS
    schema = judging.RESPONSE_FORMAT["json_schema"]["schema"]
    assert schema["required"] == ["more_canonical"]
    assert list(schema["properties"]) == ["more_canonical"]


def test_vote_record_prefers_logprobs():
    logprobs = {"content": [
        {"token": '{"', "logprob": 0},
        {"token": "more_canonical", "logprob": 0},
        {"token": '":', "logprob": 0},
        {"token": " 1", "logprob": -0.1, "top_logprobs": [
            {"token": " 1", "logprob": -0.2231},  # p ≈ 0.8
            {"token": " 2", "logprob": -1.6094},  # p ≈ 0.2
        ]},
    ]}
    v = judging.vote_record("AB", '{"more_canonical": 1, "confidence": 99}', logprobs)
    assert v["p_logprob"] == pytest.approx(0.8, abs=1e-3)
    assert v["p_a"] == pytest.approx(0.8, abs=1e-3)


def test_vote_orders_alternate():
    assert judging.vote_orders(3) == ["AB", "BA", "AB"]


def test_scope_helpers():
    assert scopes_for("anthropic/claude-sonnet-5.5:batch") == ["model:anthropic/claude-sonnet-5.5", "maker:anthropic"]
    for s in ("model:z-ai/glm-5.3-flash", "maker:z-ai"):
        assert scope_from_key(scope_key(s)) == s


# ── ladders ───────────────────────────────────────────────────────────────────

class RatingBookTests(TestCase):
    def setUp(self):
        self.a = Author.objects.create(name="A", elo_rating=1300, mlaib_elo=1300)
        self.b = Author.objects.create(name="B", elo_rating=1100, mlaib_elo=1100)

    def test_apply_updates_all_ladders_from_prior(self):
        book = RatingBook("author", {self.a.pk: self.a, self.b.pk: self.b})
        upd = book.apply(self.a.pk, self.b.pk, 0.5, "openai/gpt-6-luna")
        book.save()
        assert upd.elo_a_before == 1300
        # A was favoured; a 0.5 result moves A down and B up.
        self.a.refresh_from_db()
        assert self.a.elo_rating < 1300
        rows = ItemRating.objects.filter(item_id=self.a.pk)
        assert {r.scope for r in rows} == {"model:openai/gpt-6-luna", "maker:openai"}
        assert all(r.elo_rating == pytest.approx(self.a.elo_rating) and r.games == 1 for r in rows)

    def test_rebuild_matches_incremental(self):
        book = RatingBook("author", {self.a.pk: self.a, self.b.pk: self.b})
        for model, score in (("openai/gpt-6-luna", 0.9), ("google/gemini-4-hydrogen", 0.3)):
            u = book.apply(self.a.pk, self.b.pk, score, model)
            LLMMatchup.objects.create(
                content_type="author", item_a_id=self.a.pk, item_b_id=self.b.pk,
                winner="A" if score > 0.5 else "B", score_a=score, model_used=model,
                elo_a_before=u.elo_a_before, elo_b_before=u.elo_b_before,
                elo_a_after=u.elo_a_after, elo_b_after=u.elo_b_after,
            )
        book.save()
        before = {(r.scope, r.item_id): r.elo_rating for r in ItemRating.objects.all()}
        agg = Author.objects.get(pk=self.a.pk).elo_rating
        rebuild_ratings("author")
        after = {(r.scope, r.item_id): r.elo_rating for r in ItemRating.objects.all()}
        assert before.keys() == after.keys()
        for k in before:
            assert after[k] == pytest.approx(before[k])
        assert Author.objects.get(pk=self.a.pk).elo_rating == pytest.approx(agg)


# ── collect_llm_batches ───────────────────────────────────────────────────────

def _result(cid, content):
    return {"custom_id": cid, "error": None, "response": {"status_code": 200, "body": {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}]}}}


class CollectBatchTests(TestCase):
    def setUp(self):
        self.a = Author.objects.create(name="Herman Melville", mlaib_elo=1500, elo_rating=1500)
        self.b = Author.objects.create(name="Fitz-Greene Halleck", mlaib_elo=1000, elo_rating=1000)
        self.batch = LLMBatch.objects.create(
            openrouter_id="batch-test", model_slug="z-ai/glm-5.3-flash", content_type="author",
            status="in_progress", request_count=2, votes_per_pairing=2,
            manifest={"pairs": [[self.b.pk, self.a.pk]]},
        )

    def _collect(self, payload):
        with patch("core.openrouter.get_batch", return_value=payload), \
             patch("core.management.commands.collect_llm_batches.RAW_DIR") as raw:
            raw.__truediv__.return_value.write_text = lambda *_: None
            out = StringIO()
            call_command("collect_llm_batches", stdout=out)
            return out.getvalue()

    def test_in_progress_does_nothing(self):
        self._collect({"status": "in_progress", "request_counts": {"total": 2}})
        assert LLMMatchup.objects.count() == 0
        assert not LLMBatch.objects.get().applied

    def test_completed_applies_fractional_score(self):
        # Pair is (Halleck=A, Melville=B). v0 shows AB: picks 2 (Melville) @90 → p_a 0.1.
        # v1 shows BA: picks 1 (Melville) @70 → p_a 0.3. Mean 0.2.
        self._collect({
            "status": "completed", "request_counts": {"total": 2, "completed": 2, "failed": 0},
            "usage": {"cost": 0.0001},
            "results": [
                _result("p0-v1", '{"more_canonical": 1, "confidence": 70}'),
                _result("p0-v0", '{"more_canonical": 2, "confidence": 90}'),
            ],
        })
        m = LLMMatchup.objects.get()
        assert m.score_a == pytest.approx(0.2)
        assert m.winner == "B"
        assert m.batch_id == self.batch.pk
        assert len(m.votes) == 2
        b = LLMBatch.objects.get()
        assert b.applied and b.cost_usd == 0.0001
        assert ItemRating.objects.filter(scope="model:z-ai/glm-5.3-flash").count() == 2
        # Re-running doesn't double-apply.
        self._collect({"status": "completed", "results": []})
        assert LLMMatchup.objects.count() == 1


class SyncSubmitTests(TestCase):
    def setUp(self):
        Author.objects.create(name="Herman Melville", mlaib_elo=1500, elo_rating=1500)
        Author.objects.create(name="Fitz-Greene Halleck", mlaib_elo=1000, elo_rating=1000)

    def test_sync_applies_immediately(self):
        catalog = [{"id": "openai/gpt-6-luna", "supported_parameters": ["structured_outputs"],
                    "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}}]

        def fake_completion(model, body):
            assert model == "openai/gpt-6-luna" and "response_format" in body
            return {"choices": [{"message": {"content": '{"more_canonical": 1}'}}],
                    "usage": {"cost": 0.00002}, "provider": "OpenAI"}

        with patch("core.openrouter.list_models", return_value=catalog), \
             patch("core.openrouter.chat_completion", side_effect=fake_completion), \
             patch("core.openrouter.create_batch") as create, \
             patch("core.management.commands.collect_llm_batches.RAW_DIR") as raw:
            raw.__truediv__.return_value.write_text = lambda *_: None
            call_command("submit_llm_batch", "--mode", "authors", "--count", "1",
                         "--models", "openai/gpt-6-luna", "--sync", stdout=StringIO())
        create.assert_not_called()
        b = LLMBatch.objects.get()
        assert b.openrouter_id.startswith("sync-") and b.applied and b.status == "completed"
        assert b.cost_usd == pytest.approx(0.00004) and b.provider == "OpenAI"
        assert b.request_options["sync"] is True
        m = LLMMatchup.objects.get()
        assert m.score_a == 0.5  # always "1" across both orders = followed presentation order
        assert m.batch_id == b.pk


# ── per-model views ───────────────────────────────────────────────────────────

class ScopedViewTests(TestCase):
    def setUp(self):
        self.a = Author.objects.create(name="Emily Dickinson", mlaib_elo=1400, elo_rating=1400)
        self.b = Author.objects.create(name="Lydia Sigourney", mlaib_elo=1300, elo_rating=1300)
        self.w = Work.objects.create(title="Moby-Dick", author=self.a)
        book = RatingBook("author", {self.a.pk: self.a, self.b.pk: self.b})
        u = book.apply(self.a.pk, self.b.pk, 0.85, "anthropic/claude-sonnet-5.5")
        book.save()
        LLMMatchup.objects.create(
            content_type="author", item_a_id=self.a.pk, item_b_id=self.b.pk, winner="A", score_a=0.85,
            model_used="anthropic/claude-sonnet-5.5", elo_a_before=u.elo_a_before, elo_b_before=u.elo_b_before,
            elo_a_after=u.elo_a_after, elo_b_after=u.elo_b_after,
            votes=[{"order": "AB", "verdict": 1, "confidence": 90, "p_logprob": None, "p_a": 0.9},
                   {"order": "BA", "verdict": 2, "confidence": 80, "p_logprob": None, "p_a": 0.8}],
        )

    def test_scoped_leaderboard(self):
        url = reverse("core:authors_lb_scoped", kwargs={"scope_key": "model-anthropic--claude-sonnet-5.5"})
        r = self.client.get(url)
        assert r.status_code == 200
        self.assertContains(r, "Emily Dickinson")
        self.assertContains(r, "According to claude-sonnet-5.5")

    def test_maker_leaderboard_and_picker(self):
        r = self.client.get(reverse("core:authors_lb"))
        self.assertContains(r, "maker-anthropic")
        r = self.client.get(reverse("core:authors_lb_scoped", kwargs={"scope_key": "maker-anthropic"}))
        assert r.status_code == 200

    def test_unknown_scope_404(self):
        r = self.client.get(reverse("core:authors_lb_scoped", kwargs={"scope_key": "model-nobody--x"}))
        assert r.status_code == 404

    def test_detail_shows_by_model(self):
        r = self.client.get(reverse("core:author_detail", kwargs={"pk": self.a.pk}))
        self.assertContains(r, "By model")
        self.assertContains(r, "claude-sonnet-5.5")

    def test_judges_page(self):
        r = self.client.get(reverse("core:judges"))
        assert r.status_code == 200
        self.assertContains(r, "claude-sonnet-5.5")
        self.assertContains(r, "100%")  # order consistency: both votes favour A

    def test_build_static(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            call_command("build_static", output=d, stdout=StringIO())
            p = Path(d) / "leaderboard/authors/by/model-anthropic--claude-sonnet-5.5/index.html"
            assert p.exists()
            assert "Emily Dickinson" in p.read_text()
            assert (Path(d) / "judges/index.html").exists()


# ── Bradley–Terry ─────────────────────────────────────────────────────────────

def test_bt_fit_orders_and_is_order_independent():
    from core import bt
    games = [(1, 2, 1.0)] * 8 + [(2, 3, 1.0)] * 8 + [(1, 3, 0.5)] * 2 + [(3, 2, 0.0)]
    f = bt.fit([1, 2, 3], games, with_se=True)
    assert f.top(3) == [1, 2, 3]
    assert (f.se > 0).all()
    g = bt.fit([1, 2, 3], list(reversed(games)))
    assert np.allclose(f.theta, g.theta, atol=1e-4)
    # Unknown pks in games are ignored.
    assert bt.fit([1, 2], games).top(1) == [1]
    assert f.slope is None and f.tau is None  # no prior given


def test_bt_mlaib_prior_is_estimated_and_places_unplayed_items():
    from core import bt
    rng = np.random.default_rng(0)
    n = 40
    x = np.linspace(-2, 2, n)                     # MLAIB prior, logit scale
    theta = 0.8 * x + rng.normal(0, 0.5, n)       # judges bear out 80% of it, give or take 0.5
    ids = list(range(1, n + 1))
    games = [(ids[i], ids[j], 1 / (1 + np.exp(theta[j] - theta[i]))) for i in range(n) for j in range(i + 1, n)] * 3
    f = bt.fit(ids + [99], games, with_se=True, prior=np.r_[x, 2.5])  # 99 never played
    assert 0.6 < f.slope < 1.0 and 0.3 < f.tau < 0.8
    assert f.rating()[99] == pytest.approx(1200 + bt.ELO_SCALE * f.slope * 2.5, abs=0.5)  # sits at its prior...
    assert f.se[-1] == pytest.approx(f.tau)                                      # ...with the prior's spread
    plain = bt.fit(ids, games)
    assert np.corrcoef(plain.theta, f.theta[:n])[0, 1] > 0.99  # plenty of games: the data decide


class FittedRatingTests(TestCase):
    """Leaderboards and detail pages switch to the Bradley–Terry fit once it exists."""

    def setUp(self):
        names = ["Herman Melville", "Emily Dickinson", "Lydia Sigourney", "Fitz-Greene Halleck"]
        # Elo order deliberately disagrees with the results below.
        self.authors = [Author.objects.create(name=n, mlaib_elo=1000 + 100 * i, elo_rating=1000 + 100 * i)
                        for i, n in enumerate(names)]
        a = self.authors
        results = [(0, 1, 1.0), (0, 2, 1.0), (0, 3, 1.0), (1, 2, 1.0), (1, 3, 1.0), (2, 3, 0.5)] * 3
        book = RatingBook("author", {x.pk: x for x in a})
        for i, j, sc in results:
            model = "openai/gpt-6-luna"
            u = book.apply(a[i].pk, a[j].pk, sc, model)
            LLMMatchup.objects.create(
                content_type="author", item_a_id=a[i].pk, item_b_id=a[j].pk, winner="A" if sc >= 0.5 else "B",
                score_a=sc, model_used=model, elo_a_before=u.elo_a_before, elo_b_before=u.elo_b_before,
                elo_a_after=u.elo_a_after, elo_b_after=u.elo_b_after)
        book.save()

    def test_refit_and_leaderboard(self):
        from core import bt
        from core.models import FittedRating

        summary = bt.refit("author")
        assert summary == {"all": 4, "maker:openai": 4, "model:openai/gpt-6-luna": 4}
        fits = {f.item_id: f for f in FittedRating.objects.filter(scope="all")}
        melville = fits[self.authors[0].pk]
        assert melville.rank == 1 and melville.rank_lo == 1 and melville.se > 0
        assert all(f.rank_lo <= f.rank <= f.rank_hi for f in fits.values())

        r = self.client.get(reverse("core:authors_lb"))
        html = r.content.decode()
        assert "Likely rank" in html and "interval-point" in html
        assert html.index("Herman Melville") < html.index("Emily Dickinson") < html.index("Fitz-Greene Halleck")

        r = self.client.get(reverse("core:author_detail", kwargs={"pk": self.authors[0].pk}))
        self.assertContains(r, "Rank&nbsp;#1")
        self.assertContains(r, "Rating&nbsp;")

        data = self.client.get(reverse("core:search_data")).json()
        assert data["authors"][0]["name"] == "Herman Melville" and data["authors"][0]["ci"] > 0

    def test_elo_fallback_without_fit(self):
        r = self.client.get(reverse("core:authors_lb"))
        html = r.content.decode()
        assert "Likely rank" not in html and '<th class="num">Elo</th>' in html
        by_elo = sorted(self.authors, key=lambda x: -Author.objects.get(pk=x.pk).elo_rating)
        assert html.index(by_elo[0].name) < html.index(by_elo[-1].name)


class RankChangeTests(TestCase):
    def test_mlaib_ranks_share_ties(self):
        from core import rankings
        a = Author.objects.create(name="A", mlaib_elo=1500)
        b = Author.objects.create(name="B", mlaib_elo=900)
        c = Author.objects.create(name="C", mlaib_elo=900)
        d = Author.objects.create(name="D", mlaib_elo=800)
        Author.objects.create(name="E", mlaib_elo=None)
        assert rankings.mlaib_ranks("author") == {a.pk: 1, b.pk: 2, c.pk: 2, d.pk: 4}

    def test_change_shown_on_leaderboard_and_detail(self):
        # Low MLAIB prior, but Elo puts Sigourney first.
        top = Author.objects.create(name="Lydia Sigourney", mlaib_elo=800, elo_rating=1700)
        Author.objects.create(name="Herman Melville", mlaib_elo=1600, elo_rating=1500)
        Author.objects.create(name="Emily Dickinson", mlaib_elo=1400, elo_rating=1400)
        r = self.client.get(reverse("core:authors_lb"))
        self.assertContains(r, 'title="Up 2 places from the MLAIB starting rank"')
        self.assertContains(r, 'title="Down 1 place from the MLAIB starting rank"', count=2)
        r = self.client.get(reverse("core:author_detail", kwargs={"pk": top.pk}))
        self.assertContains(r, "Started at #3")
        self.assertContains(r, "up to #1")


class HomeChartTests(TestCase):
    def test_chart_selects_winners_risers_and_fallers(self):
        from core import views
        # 12 authors: MLAIB order 0..11; Elo reverses the bottom half so some rise and some fall.
        for i in range(12):
            elo = 1000 + (12 - i) * 10 if i < 6 else 1200 + i * 10
            Author.objects.create(name=f"Author {i:02d}", mlaib_elo=1600 - i * 50, elo_rating=elo)
        with patch.object(views, "CHART_WINNERS", 2), patch.object(views, "CHART_CONTENDERS", 1), \
             patch.object(views, "CHART_RISERS", 2), patch.object(views, "CHART_FALLERS", 2):
            series = views._get_top_chart_series("author")
        roles = [s["role"] for s in series]
        assert roles[:2] == ["winner"] * 2 and roles.count("contender") == 1
        assert roles.count("riser") == 2 and roles.count("faller") == 2
        assert len({s["name"] for s in series}) == len(series)
        assert all(s["change"] > 0 for s in series if s["role"] == "riser")
        assert all(s["change"] < 0 for s in series if s["role"] == "faller")

        r = self.client.get(reverse("core:home"))
        self.assertContains(r, "Close behind:")
        self.assertContains(r, 'data-legend-index=')


def _catalog(slug, params=("structured_outputs", "logprobs", "top_logprobs", "reasoning")):
    return [{"id": slug, "supported_parameters": list(params), "pricing": {"prompt": "0.00000002", "completion": "0.0000001"}}]


class ReplayTests(TestCase):
    def setUp(self):
        self.a = Author.objects.create(name="Herman Melville", mlaib_elo=1500, elo_rating=1500)
        self.b = Author.objects.create(name="Emily Dickinson", mlaib_elo=1400, elo_rating=1400)
        self.c = Author.objects.create(name="Lydia Sigourney", mlaib_elo=1000, elo_rating=1000)
        for i, pairs in enumerate(([[self.a.pk, self.b.pk], [self.b.pk, self.c.pk]], [[self.a.pk, self.c.pk]])):
            LLMBatch.objects.create(openrouter_id=f"src-{i}", model_slug="anthropic/claude-haiku-5.5",
                                    content_type="author", status="completed", applied=True,
                                    request_count=2000, votes_per_pairing=2, manifest={"pairs": pairs})
        LLMBatch.objects.create(openrouter_id="src-test", model_slug="anthropic/claude-haiku-5.5",
                                content_type="author", status="completed", applied=True,
                                request_count=40, votes_per_pairing=2, manifest={"pairs": [[self.c.pk, self.a.pk]]})

    def test_replay_reuses_pairings_binary_and_provider_sort(self):
        slug = "deepseek/deepseek-v4.1-flash-1017"
        seen = []

        def fake_completion(model, body):
            seen.append(body)
            return {"choices": [{"message": {"content": '{"more_canonical": 1}'}}], "usage": {"cost": 0.00001}}

        with patch("core.openrouter.list_models", return_value=_catalog(slug)), \
             patch("core.openrouter.chat_completion", side_effect=fake_completion), \
             patch("core.management.commands.collect_llm_batches.RAW_DIR") as raw:
            raw.__truediv__.return_value.write_text = lambda *_: None
            call_command("submit_llm_batch", "--mode", "authors", "--models", slug,
                         "--replay-model", "anthropic/claude-haiku-5.5", "--sync", "--provider-sort", "price",
                         stdout=StringIO())
        new = list(LLMBatch.objects.filter(model_slug=slug).order_by("id"))
        assert [b.manifest["pairs"] for b in new] == [
            [[self.a.pk, self.b.pk], [self.b.pk, self.c.pk]], [[self.a.pk, self.c.pk]]]  # test batch skipped
        assert all(b.request_options["replay_model"] == "anthropic/claude-haiku-5.5" for b in new)
        assert len(seen) == 6
        assert all(body["provider"] == {"sort": "price"} and "logprobs" not in body for body in seen)
        assert LLMMatchup.objects.filter(model_used=slug).count() == 3

        # A second run resumes: every source batch is already replayed, so nothing is sent.
        seen.clear()
        with patch("core.openrouter.list_models", return_value=_catalog(slug)), \
             patch("core.openrouter.chat_completion", side_effect=fake_completion):
            call_command("submit_llm_batch", "--mode", "authors", "--models", slug,
                         "--replay-model", "anthropic/claude-haiku-5.5", "--sync", stdout=StringIO())
        assert seen == [] and LLMBatch.objects.filter(model_slug=slug).count() == 2


# ── decision models ───────────────────────────────────────────────────────────

def _answer(choice, p1):
    return {"more_canonical": {"type": "choice", "choice": choice,
                               "probabilities": {"1": p1, "2": 1 - p1}, "confidence": max(p1, 1 - p1)}}


def test_decision_vote_record_binary_probabilistic_and_refusal():
    # Shown BA, picks "2" (=A): binary p_a 1.0; the model's P(first) is kept for reference.
    v = judging.decision_vote_record("BA", _answer("2", 0.3))
    assert v["verdict"] == 2 and v["p_a"] == 1.0 and v["p_decision"] == 0.3
    assert judging.decision_vote_record("BA", _answer("2", 0.3), probabilistic=True)["p_a"] == pytest.approx(0.7)
    assert judging.decision_vote_record("AB", _answer("1", 1.0), probabilistic=True)["p_a"] == judging.P_MAX
    assert judging.decision_vote_record("AB", {"more_canonical": {"type": "refusal"}}) is None
    assert judging.decision_vote_record("AB", {}) is None


class DecisionModelTests(TestCase):
    def setUp(self):
        Author.objects.create(name="Herman Melville", mlaib_elo=1500, elo_rating=1500)
        Author.objects.create(name="Fitz-Greene Halleck", mlaib_elo=1000, elo_rating=1000)
        self.slug = "typesafe/jev-1.14"
        self.deciders = [{"id": self.slug, "architecture": {"output_modalities": ["decisions"]},
                          "supported_parameters": [], "pricing": {"prompt": "0.000000042", "completion": "0"}}]

    def _models(self, output_modalities=None):
        return self.deciders if output_modalities == "decisions" else []

    def test_sync_uses_decisions_api(self):
        seen = []

        def fake_decide(model, body):
            assert model == self.slug
            seen.append(body)
            q = body["questions"]["more_canonical"]
            assert q["type"] == "choice" and "Answer 1 or 2" not in q["instructions"]
            pick = "1" if q["criteria"]["1"] == "Herman Melville" else "2"
            return {"answers": _answer(pick, 0.9 if pick == "1" else 0.1),
                    "usage": {"cost": 0.000014}, "provider": "TypeSafe"}

        with patch("core.openrouter.list_models", side_effect=self._models), \
             patch("core.openrouter.decide", side_effect=fake_decide), \
             patch("core.openrouter.chat_completion") as chat, \
             patch("core.management.commands.collect_llm_batches.RAW_DIR") as raw:
            raw.__truediv__.return_value.write_text = lambda *_: None
            call_command("submit_llm_batch", "--mode", "authors", "--count", "1",
                         "--models", self.slug, "--sync", stdout=StringIO())
        chat.assert_not_called()
        assert len(seen) == 2
        b = LLMBatch.objects.get()
        assert b.request_options["decisions"] is True and b.provider == "TypeSafe"
        m = LLMMatchup.objects.get()
        melville = Author.objects.get(name="Herman Melville").pk
        assert m.score_a == (1.0 if m.item_a_id == melville else 0.0)  # Melville in both orders, binary
        assert all(v["p_decision"] is not None for v in m.votes)

    def test_decision_model_requires_sync(self):
        from django.core.management.base import CommandError

        with patch("core.openrouter.list_models", side_effect=self._models), pytest.raises(CommandError, match="--sync"):
            call_command("submit_llm_batch", "--mode", "authors", "--count", "1",
                         "--models", self.slug, stdout=StringIO())


# ── pairing by the fit ────────────────────────────────────────────────────────

class PairByFitTests(TestCase):
    def setUp(self):
        # The Elo ladder (still on the prior) has these backwards; the games say Melville is far ahead.
        self.melville = Author.objects.create(name="Herman Melville", mlaib_elo=900, elo_rating=900)
        self.halleck = Author.objects.create(name="Fitz-Greene Halleck", mlaib_elo=1000, elo_rating=1000)
        self.new = Author.objects.create(name="Unplayed Author", mlaib_elo=850, elo_rating=850)
        for _ in range(6):
            LLMMatchup.objects.create(
                content_type="author", item_a_id=self.melville.pk, item_b_id=self.halleck.pk, winner="A",
                score_a=1.0, elo_a_before=0, elo_b_before=0, elo_a_after=0, elo_b_after=0,
                model_used="anthropic/claude-haiku-5.5")

    def test_pairing_ratings_use_fit_which_places_unplayed_by_prior(self):
        from core import bt
        from core.management.commands.submit_llm_batch import pairing_ratings

        fit = bt.fit_content_type("author")
        r = pairing_ratings(list(Author.objects.all()), fit)
        assert r[self.melville.pk] > r[self.halleck.pk]  # the games, not the ladder
        assert r[self.new.pk] == fit.rating()[self.new.pk] < 1200  # unplayed, below-average prior

    def _dry_run(self, *extra):
        catalog = [{"id": "openai/gpt-6-luna:batch", "supported_parameters": [],
                    "pricing": {"prompt": "0", "completion": "0"}}]
        from core.management.commands import submit_llm_batch as cmd

        seen = []
        real = cmd._generate_pairings

        def spy(*args):
            seen.append(args[5])
            return real(*args)

        with patch("core.openrouter.list_models", return_value=catalog), patch.object(cmd, "_generate_pairings", spy):
            call_command("submit_llm_batch", "--mode", "authors", "--count", "1", "--models", "openai/gpt-6-luna",
                         "--dry-run", *extra, stdout=StringIO())
        return seen

    def test_submit_pairs_by_fit_by_default(self):
        [ratings] = self._dry_run()
        assert ratings[self.melville.pk] > ratings[self.halleck.pk]

    def test_pair_by_elo_uses_the_ladder(self):
        assert self._dry_run("--pair-by", "elo") == [None]
