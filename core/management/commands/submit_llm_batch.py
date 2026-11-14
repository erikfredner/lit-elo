"""Submit LLM canonicity matchups to the OpenRouter Batch API.

One batch is created per (model, content type). By default every model judges
the *same* pairings, so their verdicts are directly comparable. Results are
applied later by ``collect_llm_batches``.

Usage:
    python manage.py submit_llm_batch --mode both --count 25
    python manage.py submit_llm_batch --mode works --count 100 --models openai/gpt-6-luna,z-ai/glm-5.3-flash
    python manage.py submit_llm_batch --mode authors --count 10 --dry-run
    python manage.py submit_llm_batch --mode both --games-per-item 5 --max-cost 10   # one Scenario A round
"""

from __future__ import annotations

import random

from django.core.management.base import BaseCommand, CommandError

from core import judging, openrouter
from core.management.commands.run_llm_elo import _generate_pairings, _load_items, _load_matchup_index
from core.models import LLMBatch

DEFAULT_MODELS = [
    "anthropic/claude-haiku-5.5",
    "openai/gpt-6-luna",
]

# Measured on the first test batches: ~160 prompt tokens per request with the
# default system prompt. Completion tokens vary a lot with reasoning.
_EST_PROMPT_TOKENS = 170


class Command(BaseCommand):
    help = "Submit LLM canonicity matchups to the OpenRouter Batch API (one batch per model and mode)."

    def add_arguments(self, parser):
        parser.add_argument("--mode", choices=["authors", "works", "both"], required=True)
        parser.add_argument("--count", type=int, default=25, help="Pairings per mode (default: 25).")
        parser.add_argument(
            "--games-per-item",
            type=float,
            default=None,
            help="Size each mode's batch so every item averages this many new games "
            "(pairings = items * N / 2). Overrides --count.",
        )
        parser.add_argument(
            "--models",
            default=",".join(DEFAULT_MODELS),
            help="Comma-separated OpenRouter model slugs (base slug; ':batch' is optional).",
        )
        parser.add_argument(
            "--votes-per-pairing",
            type=int,
            default=2,
            help="Votes per pairing, alternating presentation order AB/BA (default: 2).",
        )
        parser.add_argument("--seed", type=int, default=None)
        parser.add_argument(
            "--separate-pairings",
            action="store_true",
            help="Draw different pairings for each model instead of sharing one set.",
        )
        parser.add_argument("--max-tokens", type=int, default=2048)
        parser.add_argument(
            "--reasoning-effort",
            default="low",
            help="Reasoning effort passed to OpenRouter (e.g. none, minimal, low, medium). 'omit' sends nothing.",
        )
        parser.add_argument(
            "--est-completion-tokens",
            type=int,
            default=300,
            help="Completion tokens per request assumed for the cost estimate (default: 300).",
        )
        parser.add_argument(
            "--max-cost",
            type=float,
            default=2.0,
            help="Refuse to submit if the estimated total cost exceeds this many USD (default: 2).",
        )
        parser.add_argument("--system-prompt", default=None)
        parser.add_argument("--dry-run", action="store_true", help="Build requests and estimate cost; submit nothing.")

    def handle(self, *args, **opts):
        from pathlib import Path

        modes = ["authors", "works"] if opts["mode"] == "both" else [opts["mode"]]
        models = [openrouter_base(m) for m in opts["models"].split(",") if m.strip()]
        votes = opts["votes_per_pairing"]
        if votes < 1:
            raise CommandError("--votes-per-pairing must be >= 1")
        rng = random.Random(opts["seed"])
        system_prompt = judging.load_system_prompt(Path(opts["system_prompt"]) if opts["system_prompt"] else None)
        system_prompt = f"{system_prompt}\n\n{judging.OUTPUT_INSTRUCTIONS}"

        catalog = {m["id"]: m for m in openrouter.list_models()}
        caps: dict[str, dict] = {}
        for slug in models:
            entry = catalog.get(f"{slug}:batch")
            if entry is None:
                raise CommandError(f"{slug} has no ':batch' variant on OpenRouter.")
            params = set(entry.get("supported_parameters") or [])
            pricing = entry.get("pricing") or {}
            caps[slug] = {
                "structured": "structured_outputs" in params,
                "logprobs": "logprobs" in params and "top_logprobs" in params,
                "reasoning": "reasoning" in params,
                "prompt_price": float(pricing.get("prompt") or 0),
                "completion_price": float(pricing.get("completion") or 0),
            }

        # Pairings: shared across models unless --separate-pairings.
        plan: list[tuple[str, str, list]] = []  # (model, mode, pairings)
        for mode in modes:
            items = _load_items(mode)
            if len(items) < 2:
                raise CommandError(f"Not enough {mode} to pair.")
            pair_counts, games = _load_matchup_index(mode)
            count = opts["count"]
            if opts["games_per_item"] is not None:
                count = max(1, round(len(items) * opts["games_per_item"] / 2))
            self.stdout.write(f"{mode}: {len(items)} items, {count} pairings")
            shared = None if opts["separate_pairings"] else _generate_pairings(items, count, rng, pair_counts, games)
            for slug in models:
                pairings = shared or _generate_pairings(items, count, rng, pair_counts, games)
                plan.append((slug, mode, pairings))

        # Cost estimate.
        total_est = 0.0
        for slug, mode, pairings in plan:
            c = caps[slug]
            n = len(pairings) * votes
            est = n * (_EST_PROMPT_TOKENS * c["prompt_price"] + opts["est_completion_tokens"] * c["completion_price"])
            total_est += est
            flags = ", ".join(k for k in ("structured", "logprobs") if c[k]) or "plain-text"
            self.stdout.write(f"  {slug:32s} {mode:7s} {n:4d} requests  est ${est:.4f}  [{flags}]")
        self.stdout.write(f"Estimated total: ${total_est:.4f} (assuming {opts['est_completion_tokens']} completion tokens/request)")

        if opts["dry_run"]:
            slug, mode, pairings = plan[0]
            sample = build_requests(pairings[:1], mode, system_prompt, votes, caps[slug], opts)
            import json as _json
            self.stdout.write("\nSample request bodies:")
            self.stdout.write(_json.dumps(sample, indent=2, ensure_ascii=False))
            return
        if total_est > opts["max_cost"]:
            raise CommandError(f"Estimated cost ${total_est:.2f} exceeds --max-cost ${opts['max_cost']:.2f}.")

        for slug, mode, pairings in plan:
            requests = build_requests(pairings, mode, system_prompt, votes, caps[slug], opts)
            try:
                resp = openrouter.create_batch(slug, requests)
            except openrouter.OpenRouterError as exc:
                self.stderr.write(self.style.ERROR(f"  {slug} {mode}: {exc}"))
                continue
            LLMBatch.objects.create(
                openrouter_id=resp["id"],
                model_slug=slug,
                content_type="author" if mode == "authors" else "work",
                status=resp.get("status", "submitted"),
                provider=resp.get("provider") or "",
                request_count=len(requests),
                votes_per_pairing=votes,
                manifest={"pairs": [[a.pk, b.pk] for a, b in pairings]},
                request_options={
                    "max_tokens": opts["max_tokens"],
                    "reasoning_effort": opts["reasoning_effort"],
                    "structured": caps[slug]["structured"],
                    "logprobs": caps[slug]["logprobs"],
                    "system_prompt": system_prompt,
                },
            )
            self.stdout.write(self.style.SUCCESS(
                f"  submitted {slug} {mode}: {resp['id']} ({len(requests)} requests, provider {resp.get('provider')})"
            ))
        self.stdout.write("Run `python manage.py collect_llm_batches --wait` to apply results.")


def openrouter_base(slug: str) -> str:
    return slug.strip().split(":", 1)[0]


def build_requests(pairings, mode: str, system_prompt: str, votes: int, caps: dict, opts: dict) -> list[dict]:
    requests = []
    orders = judging.vote_orders(votes)
    for i, (a, b) in enumerate(pairings):
        for j, order in enumerate(orders):
            first, second = (a, b) if order == "AB" else (b, a)
            body: dict = {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": judging.build_user_prompt(first, second, mode)},
                ],
                "max_tokens": opts["max_tokens"],
            }
            # Models without strict schemas answer in plain text ("1"/"2").
            if caps["structured"]:
                body["response_format"] = judging.RESPONSE_FORMAT
            if caps["reasoning"] and opts["reasoning_effort"] != "omit":
                body["reasoning"] = {"effort": opts["reasoning_effort"]}
            if caps["logprobs"]:
                body["logprobs"] = True
                body["top_logprobs"] = 5
            requests.append({"custom_id": f"p{i}-v{j}", "body": body})
    return requests
