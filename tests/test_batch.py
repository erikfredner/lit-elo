"""Tests for OpenRouter batch judging: vote parsing, scoring, ladders, collection, per-model views."""

import json
from io import StringIO
from unittest.mock import patch

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
