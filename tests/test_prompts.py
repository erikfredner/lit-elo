"""System prompts: registration, linking to batches/matchups, and keeping experiments off published ladders."""

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from core import bt, judging
from core.models import Author, FittedRating, ItemRating, LLMBatch, LLMMatchup, SystemPrompt
from core.rankings import available_scopes
from core.ratings import rebuild_ratings, scopes_for

SLUG = "typesafe/jev-1.14"


def _answer(choice):
    return {"more_canonical": {"type": "choice", "choice": choice, "probabilities": {"1": 0.5, "2": 0.5}}}


class ResolvePromptTests(TestCase):
    def test_default_comes_from_migration(self):
        p = judging.resolve_system_prompt()
        assert p.name == "professor" and p.is_default and p.variant is None
        assert p.text == judging.load_system_prompt(judging.PROMPT_DIR / "professor.md")

    def test_registers_variant_from_file_once(self):
        p = judging.resolve_system_prompt("plain")
        assert p.text == "Which is more canonical?" and not p.is_default and p.variant == "plain"
        assert judging.resolve_system_prompt("plain").pk == p.pk

    def test_changed_text_and_duplicate_text_are_rejected(self):
        SystemPrompt.objects.create(name="plain", text="Something else")
        with pytest.raises(ValueError, match="different text"):
            judging.resolve_system_prompt("plain")
        with pytest.raises(ValueError, match="already registered"):
            judging.resolve_system_prompt("copy", judging.PROMPT_DIR / "professor.md")
        retest = judging.resolve_system_prompt("copy", judging.PROMPT_DIR / "professor.md", replicate=True)
        assert retest.variant == "copy" and retest.text == judging.resolve_system_prompt().text
        with pytest.raises(ValueError, match="No prompt"):
            judging.resolve_system_prompt("no-such-prompt")


def test_variant_scopes():
    assert scopes_for(SLUG, "plain") == [f"model:{SLUG}@plain"]
    assert scopes_for(SLUG) == [f"model:{SLUG}", "maker:typesafe"]


class VariantRunTests(TestCase):
    def setUp(self):
        self.melville = Author.objects.create(name="Herman Melville", mlaib_elo=1500, elo_rating=1500)
        self.halleck = Author.objects.create(name="Fitz-Greene Halleck", mlaib_elo=1000, elo_rating=1000)
        self.deciders = [{"id": SLUG, "architecture": {"output_modalities": ["decisions"]},
                          "supported_parameters": [], "pricing": {"prompt": "0.000000042", "completion": "0"}}]
        # A published (default-prompt) source batch to replay.
        self.source = LLMBatch.objects.create(
            openrouter_id="src", model_slug="anthropic/claude-haiku-5.5", content_type="author",
            status="completed", applied=True, request_count=2000, votes_per_pairing=2,
            manifest={"pairs": [[self.halleck.pk, self.melville.pk]]},
            system_prompt=SystemPrompt.objects.get(is_default=True))

    def _run(self, *extra):
        seen = []

        def fake_decide(model, body):
            q = body["questions"]["more_canonical"]
            seen.append(q["instructions"])
            pick = "1" if q["criteria"]["1"] == "Herman Melville" else "2"
            return {"answers": _answer(pick), "usage": {"cost": 0.00001}, "provider": "TypeSafe"}

        models = lambda output_modalities=None: self.deciders if output_modalities == "decisions" else []
        with patch("core.openrouter.list_models", side_effect=models), \
             patch("core.openrouter.decide", side_effect=fake_decide), \
             patch("core.management.commands.collect_llm_batches.RAW_DIR") as raw:
            raw.__truediv__.return_value.write_text = lambda *_: None
            call_command("submit_llm_batch", "--mode", "authors", "--models", SLUG,
                         "--replay-model", "anthropic/claude-haiku-5.5", "--sync", *extra, stdout=StringIO())
        return seen

    def test_variant_is_linked_and_kept_off_published_ladders(self):
        seen = self._run("--prompt", "plain")
        assert seen == ["Which is more canonical?"] * 2
        plain = SystemPrompt.objects.get(name="plain")
        batch = LLMBatch.objects.get(model_slug=SLUG)
        assert batch.system_prompt == plain
        assert batch.request_options["system_prompt"] == "Which is more canonical?"

        m = LLMMatchup.all_objects.get(model_used=SLUG)
        assert m.system_prompt == plain and m.score_a == 0.0  # Halleck (A) lost both votes
        assert not LLMMatchup.objects.filter(model_used=SLUG).exists()
        assert list(batch.matchups.all()) == [m]  # related manager sees every prompt

        # Aggregate and published scopes untouched; only the variant ladder moved.
        assert Author.objects.get(pk=self.halleck.pk).elo_rating == 1000
        assert set(ItemRating.objects.values_list("scope", flat=True)) == {f"model:{SLUG}@plain"}
        assert m.elo_a_before == 1000 and m.elo_a_after < 1000
        assert available_scopes("author") == []

        # The fit rates the experiment in its own scope only.
        bt.refit("author")
        assert set(FittedRating.objects.values_list("scope", flat=True)) == {f"model:{SLUG}@plain"}

        # Rebuild keeps it out of the aggregate too.
        rebuild_ratings("author")
        assert Author.objects.get(pk=self.halleck.pk).elo_rating == 1000
        assert set(ItemRating.objects.values_list("scope", flat=True)) == {f"model:{SLUG}@plain"}

        # Resuming the replay under the same prompt sends nothing; the default prompt still can.
        assert self._run("--prompt", "plain") == []
        assert len(self._run()) == 2
        assert LLMMatchup.objects.filter(model_used=SLUG).count() == 1
        assert Author.objects.get(pk=self.halleck.pk).elo_rating < 1000

    def test_variant_batches_are_not_replay_sources(self):
        self._run("--prompt", "plain")
        LLMBatch.objects.filter(model_slug=SLUG).update(request_count=2000)
        out = StringIO()
        haiku = [{"id": "anthropic/claude-haiku-5.5", "supported_parameters": [], "pricing": {}}]
        with patch("core.openrouter.list_models", return_value=haiku):
            with pytest.raises(Exception, match="no applied authors batches"):
                call_command("submit_llm_batch", "--mode", "authors", "--models", "anthropic/claude-haiku-5.5",
                             "--replay-model", SLUG, "--sync", "--dry-run", stdout=out)
