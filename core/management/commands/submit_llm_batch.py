"""Submit LLM canonicity matchups to the OpenRouter Batch API.

One batch is created per (model, content type). By default every model judges
the *same* pairings, so their verdicts are directly comparable. Results are
applied later by ``collect_llm_batches``.

With ``--sync`` the same requests go out as ordinary (non-batch) calls at the
standard price, about twice the batch price, and each model/mode is applied as
soon as it finishes. Sync runs are still recorded as ``LLMBatch`` rows (id
``sync-…``), so history, costs and ``rebuild_ratings`` treat them like batches.

Usage:
    python manage.py submit_llm_batch --mode both --count 25
    python manage.py submit_llm_batch --mode works --count 100 --models openai/gpt-6-luna,z-ai/glm-5.3-flash
    python manage.py submit_llm_batch --mode authors --count 10 --dry-run
    python manage.py submit_llm_batch --mode both --games-per-item 5 --max-cost 10   # one Scenario A round
    python manage.py submit_llm_batch --mode both --games-per-item 5 --max-cost 10 --sync   # same, non-batch
    python manage.py submit_llm_batch --mode authors --top 150 --games-per-item 10 --sync   # top-tier round
"""

from __future__ import annotations

import json
import random
import time
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

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
        parser.add_argument(
            "--top",
            type=int,
            default=None,
            help="Pair only the N highest items by a Bradley–Terry fit of all matchups (judges pooled), "
            "so the top tier plays itself. --games-per-item then applies to those N items.",
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
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Send non-batch requests (standard price, ~2x batch) and apply results immediately.",
        )
        parser.add_argument("--concurrency", type=int, default=16, help="Parallel requests with --sync (default: 16).")
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
            entry = catalog.get(slug if opts["sync"] else f"{slug}:batch")
            if entry is None:
                raise CommandError(f"{slug} is not on OpenRouter." if opts["sync"]
                                   else f"{slug} has no ':batch' variant on OpenRouter.")
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
            if opts["top"]:
                from core import bt

                keep = bt.fit_content_type("author" if mode == "authors" else "work").top(opts["top"])
                by_pk = {i.pk: i for i in items}
                items = [by_pk[pk] for pk in keep]
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
            if opts["sync"]:
                self._run_sync(slug, mode, pairings, requests, votes, caps[slug], system_prompt, opts)
                continue
            try:
                resp = openrouter.create_batch(slug, requests)
            except openrouter.OpenRouterError as exc:
                self.stderr.write(self.style.ERROR(f"  {slug} {mode}: {exc}"))
                continue
            LLMBatch.objects.create(
                openrouter_id=resp["id"],
                status=resp.get("status", "submitted"),
                provider=resp.get("provider") or "",
                **batch_fields(slug, mode, pairings, requests, votes, caps[slug], system_prompt, opts),
            )
            self.stdout.write(self.style.SUCCESS(
                f"  submitted {slug} {mode}: {resp['id']} ({len(requests)} requests, provider {resp.get('provider')})"
            ))
        if not opts["sync"]:
            self.stdout.write("Run `python manage.py collect_llm_batches --wait` to apply results.")

    def _run_sync(self, slug, mode, pairings, requests, votes, caps, system_prompt, opts) -> None:
        from django.utils import timezone

        from core.management.commands.collect_llm_batches import RAW_DIR, apply_results

        started = time.monotonic()
        results = openrouter.run_sync(slug, requests, opts["concurrency"])
        cost = openrouter.results_cost(results)
        errors = sum(r["error"] is not None for r in results)
        providers = {((r.get("response") or {}).get("body") or {}).get("provider") for r in results} - {None}
        # One transaction, so a concurrent collect_llm_batches never sees an unapplied sync row.
        with transaction.atomic():
            batch = LLMBatch.objects.create(
                openrouter_id=f"sync-{uuid.uuid4().hex[:20]}",
                status="completed",
                provider=",".join(sorted(providers)),
                failed_count=errors,
                cost_usd=cost,
                usage={"cost": cost, "mode": "sync"},
                finished_at=timezone.now(),
                **batch_fields(slug, mode, pairings, requests, votes, caps, system_prompt, opts),
            )
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            (RAW_DIR / f"{batch.openrouter_id}.json").write_text(
                json.dumps({"id": batch.openrouter_id, "mode": "sync", "results": results, "usage": {"cost": cost}},
                           ensure_ascii=False))
            n, unusable = apply_results(batch, results)
        self.stdout.write(self.style.SUCCESS(
            f"  sync {slug} {mode}: applied {n} matchups ({unusable} unusable votes, {errors} failed requests), "
            f"${cost:.4f}, {time.monotonic() - started:.0f}s"
        ))


def openrouter_base(slug: str) -> str:
    return slug.strip().split(":", 1)[0]


def batch_fields(slug, mode, pairings, requests, votes, caps, system_prompt, opts) -> dict:
    """LLMBatch fields shared by batch and sync submissions."""
    return {
        "model_slug": slug,
        "content_type": "author" if mode == "authors" else "work",
        "request_count": len(requests),
        "votes_per_pairing": votes,
        "manifest": {"pairs": [[a.pk, b.pk] for a, b in pairings]},
        "request_options": {
            "max_tokens": opts["max_tokens"],
            "reasoning_effort": opts["reasoning_effort"],
            "structured": caps["structured"],
            "logprobs": caps["logprobs"],
            "system_prompt": system_prompt,
            "sync": bool(opts.get("sync")),
        },
    }


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
