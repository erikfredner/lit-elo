"""Poll OpenRouter batches submitted by ``submit_llm_batch`` and apply results.

For each finished batch: parse every vote, combine each pairing's votes into a
fractional score for item A, apply it to the aggregate, per-model and
per-maker Elo ladders, and store one ``LLMMatchup`` per pairing. Raw results
are saved to ``data/batches/<id>.json`` because OpenRouter deletes them after
30 days.

Usage:
    python manage.py collect_llm_batches            # check once
    python manage.py collect_llm_batches --wait     # poll until every batch is final
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core import judging, openrouter
from core.models import Author, LLMBatch, LLMMatchup, Work
from core.ratings import RatingBook

RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "batches"


class Command(BaseCommand):
    help = "Collect finished OpenRouter batches and apply their results to Elo ratings."

    def add_arguments(self, parser):
        parser.add_argument("--wait", action="store_true", help="Keep polling until all pending batches are final.")
        parser.add_argument("--poll-interval", type=int, default=60, help="Seconds between polls with --wait (default: 60).")

    def handle(self, *args, **opts):
        applied_before = LLMBatch.objects.filter(applied=True).count()
        while True:
            pending = list(LLMBatch.objects.filter(applied=False).exclude(
                status__in=["failed", "expired", "cancelled"]).order_by("submitted_at"))
            if not pending:
                self.stdout.write("No pending batches.")
                break
            for batch in pending:
                self._check(batch)
            still = LLMBatch.objects.filter(applied=False).exclude(status__in=["failed", "expired", "cancelled"]).count()
            if not opts["wait"] or still == 0:
                if still:
                    self.stdout.write(f"{still} batch(es) still running.")
                break
            self.stdout.write(f"{still} batch(es) still running; sleeping {opts['poll_interval']}s…")
            self.stdout.flush()
            time.sleep(opts["poll_interval"])
        if LLMBatch.objects.filter(applied=True).count() > applied_before:
            from core import bt

            bt.refit_all()
            self.stdout.write("Refit Bradley–Terry ratings.")
        self._summary()

    def _check(self, batch: LLMBatch) -> None:
        data = openrouter.get_batch(batch.openrouter_id)
        status = data.get("status", batch.status)
        batch.status = status
        batch.provider = data.get("provider") or batch.provider
        counts = data.get("request_counts") or {}
        batch.failed_count = counts.get("failed", 0) or 0
        if data.get("usage"):
            batch.usage = data["usage"]
            batch.cost_usd = data["usage"].get("cost")
        if data.get("error"):
            batch.error = json.dumps(data["error"])[:2000]
        if status in openrouter.FINAL_STATUSES and batch.finished_at is None:
            batch.finished_at = timezone.now()
        batch.save()

        label = f"{batch.model_slug} {batch.content_type}"
        if status != "completed":
            self.stdout.write(f"  {label}: {status} {counts}")
            return
        results = data.get("results")
        if results is None:
            self.stdout.write(f"  {label}: completed, results not ready yet")
            return

        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / f"{batch.openrouter_id}.json").write_text(json.dumps(data, ensure_ascii=False))
        n, parse_failures = apply_results(batch, results)
        cost = f"${batch.cost_usd:.4f}" if batch.cost_usd is not None else "?"
        self.stdout.write(self.style.SUCCESS(
            f"  {label}: applied {n} matchups ({parse_failures} unusable votes, {batch.failed_count} failed requests), cost {cost}"
        ))

    def _summary(self) -> None:
        rows = LLMBatch.objects.all()
        total = sum(b.cost_usd or 0 for b in rows)
        self.stdout.write(f"Total recorded batch cost: ${total:.4f} across {rows.count()} batch(es).")


@transaction.atomic
def apply_results(batch: LLMBatch, results: list[dict]) -> tuple[int, int]:
    """Turn one batch's results into LLMMatchups and apply them to every ladder.

    Returns (matchups applied, unusable votes). Marks the batch applied.
    """
    orders = judging.vote_orders(batch.votes_per_pairing)
    pairs = batch.manifest["pairs"]
    votes: dict[int, list[dict]] = defaultdict(list)
    failures = 0
    for r in results:
        cid = r.get("custom_id", "")
        try:
            p_part, v_part = cid.split("-")
            pi, vi = int(p_part[1:]), int(v_part[1:])
        except ValueError:
            failures += 1
            continue
        body = ((r.get("response") or {}).get("body")) or {}
        choice = (body.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content")
        vote = judging.vote_record(orders[vi], content, choice.get("logprobs")) if r.get("error") is None else None
        if vote is None:
            failures += 1
            continue
        votes[pi].append(vote)

    model_cls = Author if batch.content_type == "author" else Work
    pks = {pk for pair in pairs for pk in pair}
    items = {i.pk: i for i in model_cls.objects.filter(pk__in=pks)}
    book = RatingBook(batch.content_type, items)
    matchups = []
    for pi, (pk_a, pk_b) in enumerate(pairs):
        vs = votes.get(pi)
        if not vs or pk_a not in items or pk_b not in items:
            continue
        score = judging.score_from_votes(vs)
        upd = book.apply(pk_a, pk_b, score, batch.model_slug)
        matchups.append(LLMMatchup(
            content_type=batch.content_type,
            item_a_id=pk_a,
            item_b_id=pk_b,
            winner="A" if score >= 0.5 else "B",
            elo_a_before=upd.elo_a_before,
            elo_b_before=upd.elo_b_before,
            elo_a_after=upd.elo_a_after,
            elo_b_after=upd.elo_b_after,
            model_used=batch.model_slug,
            score_a=round(score, 4),
            votes=vs,
            batch=batch,
        ))
    book.save()
    LLMMatchup.objects.bulk_create(matchups)
    batch.applied = True
    batch.applied_at = timezone.now()
    batch.save(update_fields=["applied", "applied_at"])
    return len(matchups), failures
