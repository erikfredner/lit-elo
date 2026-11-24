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
    python manage.py submit_llm_batch --mode both --models NEW/model --replay-model anthropic/claude-haiku-5.5 \
        --sync --provider-sort price   # catch a new model up on the same pairings
    python manage.py submit_llm_batch --mode both --models typesafe/jev-1.14 \
        --replay-model anthropic/claude-haiku-5.5 --sync   # a decision model (sync only)
    python manage.py submit_llm_batch --mode both --models typesafe/jev-1.14 --prompt plain \
        --replay-model anthropic/claude-haiku-5.5 --sync   # prompt experiment: prompts/plain.md

Every batch and matchup records its ``SystemPrompt`` (``--prompt NAME`` reads
``prompts/NAME.md``; default: the default prompt). Under a non-default prompt
the results stay off the published ladders and are rated only in the scope
``model:<slug>@<prompt>``; with --replay-model, the source batches are the
default-prompt ones and resuming counts only runs under this prompt.

Decision models (output modality "decisions") have no chat endpoint: each vote
is one ``choice`` question on OpenRouter's Decisions API (see
``judging.build_decision_body``). They have no Batch API, so they need --sync.
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
# Measured on typesafe/jev-1.14: ~335 input tokens per decision, none billed for output.
_EST_DECISION_TOKENS = 340


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
        parser.add_argument(
            "--pair-by",
            choices=["fit", "elo"],
            default="fit",
            help="Ratings that choose each item's opponent by proximity: 'fit' (default) the pooled Bradley–Terry "
            "fit of all matchups, falling back to the Elo rating (the MLAIB prior) for items with no games; "
            "'elo' the live aggregate Elo ladder.",
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
        parser.add_argument(
            "--prompt",
            default=None,
            help="System prompt name: prompts/NAME.md, registered on first use (default: the default prompt). "
            "Non-default prompts are experiments, kept off the published ladders.",
        )
        parser.add_argument("--system-prompt", default=None, help="Path to a prompt file (named by its stem).")
        parser.add_argument(
            "--replicate",
            action="store_true",
            help="Allow --prompt to register text already registered under another name: a test–retest run "
            "of the same wording, kept off the published ladders like any experiment.",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Send non-batch requests (standard price, ~2x batch) and apply results immediately.",
        )
        parser.add_argument("--concurrency", type=int, default=16, help="Parallel requests with --sync (default: 16).")
        parser.add_argument(
            "--replay-model",
            default=None,
            help="Judge exactly the pairings another model judged (one run per that model's batch with more than "
            "1,000 requests, in order), so a new model catches up on the same comparisons. "
            "Ignores --count/--games-per-item/--top.",
        )
        parser.add_argument(
            "--provider-sort",
            choices=["price", "throughput", "latency"],
            default=None,
            help="Ask OpenRouter to route each request by this provider ranking (e.g. 'price' for the cheapest host).",
        )
        parser.add_argument(
            "--logprobs",
            action="store_true",
            help="Request token logprobs where supported and score votes from them (decision models: score from "
            "their reported probabilities). Off by default: votes are binary.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Build requests and estimate cost; submit nothing.")

    def handle(self, *args, **opts):
        from pathlib import Path

        modes = ["authors", "works"] if opts["mode"] == "both" else [opts["mode"]]
        models = [openrouter_base(m) for m in opts["models"].split(",") if m.strip()]
        votes = opts["votes_per_pairing"]
        if votes < 1:
            raise CommandError("--votes-per-pairing must be >= 1")
        rng = random.Random(opts["seed"])
        try:
            prompt = judging.resolve_system_prompt(
                opts["prompt"], Path(opts["system_prompt"]) if opts["system_prompt"] else None, opts["replicate"])
        except ValueError as exc:
            raise CommandError(str(exc))
        opts["_prompt"] = prompt
        self.stdout.write(f"System prompt: {prompt.name}{'' if prompt.is_default else ' (experiment: off the published ladders)'}"
                          f" — {prompt.text!r}")
        system_prompt = prompt.text
        decision_instructions = system_prompt  # decision models answer a choice question; no "Answer 1 or 2."
        system_prompt = f"{system_prompt}\n\n{judging.OUTPUT_INSTRUCTIONS}"

        catalog = {m["id"]: m for m in openrouter.list_models()}
        deciders = {m["id"]: m for m in openrouter.list_models("decisions")
                    if "decisions" in ((m.get("architecture") or {}).get("output_modalities") or [])}
        caps: dict[str, dict] = {}
        for slug in models:
            if slug in deciders:
                if not opts["sync"]:
                    raise CommandError(f"{slug} is a decision model, which has no Batch API; pass --sync.")
                pricing = deciders[slug].get("pricing") or {}
                caps[slug] = {
                    "decisions": True,
                    "instructions": decision_instructions,
                    "structured": False,
                    "logprobs": opts["logprobs"],  # i.e. score from the reported probabilities
                    "reasoning": False,
                    "prompt_price": float(pricing.get("prompt") or 0),
                    "completion_price": float(pricing.get("completion") or 0),
                }
                continue
            entry = catalog.get(slug if opts["sync"] else f"{slug}:batch")
            if entry is None:
                raise CommandError(f"{slug} is not on OpenRouter." if opts["sync"]
                                   else f"{slug} has no ':batch' variant on OpenRouter.")
            params = set(entry.get("supported_parameters") or [])
            pricing = entry.get("pricing") or {}
            caps[slug] = {
                "decisions": False,
                "structured": "structured_outputs" in params,
                "logprobs": opts["logprobs"] and "logprobs" in params and "top_logprobs" in params,
                "reasoning": "reasoning" in params,
                "prompt_price": float(pricing.get("prompt") or 0),
                "completion_price": float(pricing.get("completion") or 0),
            }

        # Pairings: shared across models unless --separate-pairings.
        plan: list[tuple[str, str, list]] = []  # (model, mode, pairings)
        if opts["replay_model"]:
            plan = self._replay_plan(openrouter_base(opts["replay_model"]), modes, models, votes, prompt)
        for mode in ([] if opts["replay_model"] else modes):
            items = _load_items(mode)
            pair_counts, games = _load_matchup_index(mode)
            fit = None
            if opts["top"] or (opts["pair_by"] == "fit" and games):
                from core import bt

                fit = bt.fit_content_type("author" if mode == "authors" else "work")
            if opts["top"]:
                keep = fit.top(opts["top"])
                by_pk = {i.pk: i for i in items}
                items = [by_pk[pk] for pk in keep]
            if len(items) < 2:
                raise CommandError(f"Not enough {mode} to pair.")
            ratings = None
            if opts["pair_by"] == "fit" and fit is not None:
                ratings = pairing_ratings(items, fit, games)
                unplayed = sum(not games.get(i.pk) for i in items)
                self.stdout.write(f"{mode}: pairing by the Bradley–Terry fit"
                                  + (f" ({unplayed} items with no games use their Elo prior)" if unplayed else ""))
            count = opts["count"]
            if opts["games_per_item"] is not None:
                count = max(1, round(len(items) * opts["games_per_item"] / 2))
            self.stdout.write(f"{mode}: {len(items)} items, {count} pairings")
            pair_args = (items, count, rng, pair_counts, games, ratings)
            shared = None if opts["separate_pairings"] else _generate_pairings(*pair_args)
            for slug in models:
                pairings = shared or _generate_pairings(*pair_args)
                plan.append((slug, mode, pairings))

        # Cost estimate.
        total_est = 0.0
        for slug, mode, pairings in plan:
            c = caps[slug]
            n = len(pairings) * votes
            if c["decisions"]:
                est = n * _EST_DECISION_TOKENS * c["prompt_price"]
            else:
                est = n * (_EST_PROMPT_TOKENS * c["prompt_price"] + opts["est_completion_tokens"] * c["completion_price"])
            total_est += est
            flags = ", ".join(k for k in ("decisions", "structured", "logprobs") if c[k]) or "plain-text"
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

        spent = 0.0
        for slug, mode, pairings in plan:
            requests = build_requests(pairings, mode, system_prompt, votes, caps[slug], opts)
            if opts["sync"]:
                # Sync runs bill as they go, so stop once actual spend passes --max-cost.
                if spent > opts["max_cost"]:
                    self.stderr.write(self.style.ERROR(
                        f"Stopping: spent ${spent:.2f}, over --max-cost ${opts['max_cost']:.2f}; remaining runs skipped."))
                    break
                spent += self._run_sync(slug, mode, pairings, requests, votes, caps[slug], system_prompt, opts)
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
        if opts["sync"]:
            from core import bt

            bt.refit_all()
            self.stdout.write("Refit Bradley–Terry ratings.")
        else:
            self.stdout.write("Run `python manage.py collect_llm_batches --wait` to apply results.")

    def _replay_plan(self, source: str, modes, models, votes, prompt) -> list[tuple[str, str, list]]:
        from django.db.models import Q

        from core.models import Author, Work

        published = Q(system_prompt__isnull=True) | Q(system_prompt__is_default=True)

        plan = []
        for mode in modes:
            ct = "author" if mode == "authors" else "work"
            batches = list(LLMBatch.objects.filter(
                published, model_slug=source, content_type=ct, applied=True, request_count__gt=1000).order_by("id"))
            if not batches:
                raise CommandError(f"{source} has no applied {mode} batches to replay.")
            done = {slug: LLMBatch.objects.filter(
                model_slug=slug, content_type=ct, applied=True, system_prompt=prompt,
                request_options__replay_model=source).count() for slug in models}
            qs = Author.objects if ct == "author" else Work.objects.select_related("author")
            items = qs.in_bulk({pk for b in batches for pair in b.manifest["pairs"] for pk in pair})
            for k, b in enumerate(batches):
                if b.votes_per_pairing != votes:
                    raise CommandError(f"{source} batch {b.openrouter_id} used {b.votes_per_pairing} votes per pairing; "
                                       f"pass --votes-per-pairing {b.votes_per_pairing}.")
                pairings = [(items[a], items[c]) for a, c in b.manifest["pairs"] if a in items and c in items]
                for slug in models:
                    # Resume: skip as many source batches as this model has already replayed.
                    if k < done[slug]:
                        self.stdout.write(f"{mode}: {slug} already replayed {source} batch {b.openrouter_id}; skipping")
                        continue
                    self.stdout.write(f"{mode}: replaying {source} batch {b.openrouter_id} ({len(pairings)} pairings) on {slug}")
                    plan.append((slug, mode, pairings))
        return plan

    def _run_sync(self, slug, mode, pairings, requests, votes, caps, system_prompt, opts) -> float:
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
        return cost


def openrouter_base(slug: str) -> str:
    return slug.strip().split(":", 1)[0]


def pairing_ratings(items, fit, games: dict[int, int]) -> dict[int, float]:
    """pk → rating used for opponent proximity: the fit for items with games, else the item's Elo.

    The Bradley–Terry fit is order-independent and tracks the published
    leaderboard; the K=32 Elo ladder lags it, more so the further its MLAIB
    starting point was off. An item with no games is unconstrained in the fit (it
    sits at 1200), so it keeps its Elo rating, which is its prior.
    """
    fitted = fit.rating()
    return {i.pk: fitted[i.pk] if games.get(i.pk) and i.pk in fitted else i.elo_rating for i in items}


def batch_fields(slug, mode, pairings, requests, votes, caps, system_prompt, opts) -> dict:
    """LLMBatch fields shared by batch and sync submissions."""
    return {
        "model_slug": slug,
        "content_type": "author" if mode == "authors" else "work",
        "request_count": len(requests),
        "votes_per_pairing": votes,
        "manifest": {"pairs": [[a.pk, b.pk] for a, b in pairings]},
        "system_prompt": opts.get("_prompt"),
        "request_options": {
            "max_tokens": opts["max_tokens"],
            "reasoning_effort": opts["reasoning_effort"],
            "structured": caps["structured"],
            "logprobs": caps["logprobs"],
            "system_prompt": caps["instructions"] if caps.get("decisions") else system_prompt,
            "decisions": bool(caps.get("decisions")),
            "sync": bool(opts.get("sync")),
            "provider_sort": opts.get("provider_sort"),
            "replay_model": opts.get("replay_model"),
        },
    }


def build_requests(pairings, mode: str, system_prompt: str, votes: int, caps: dict, opts: dict) -> list[dict]:
    requests = []
    orders = judging.vote_orders(votes)
    for i, (a, b) in enumerate(pairings):
        for j, order in enumerate(orders):
            first, second = (a, b) if order == "AB" else (b, a)
            if caps.get("decisions"):
                body = judging.build_decision_body(first, second, mode, caps["instructions"])
                requests.append({"custom_id": f"p{i}-v{j}", "body": body})
                continue
            body = {
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
            if opts.get("provider_sort"):
                body["provider"] = {"sort": opts["provider_sort"]}
            if caps["logprobs"]:
                body["logprobs"] = True
                body["top_logprobs"] = 5
            requests.append({"custom_id": f"p{i}-v{j}", "body": body})
    return requests
