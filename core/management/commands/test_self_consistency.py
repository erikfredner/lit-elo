"""Management command: empirically test LLM self-consistency on ELO matchup judgments.

Generates a fixed set of random pairings (reproducible via --seed), calls the LLM
--reps times per pairing, and reports how often the model agrees with itself.

Usage:
    python manage.py test_self_consistency                            # 500 pairs × both modes × 5 reps
    python manage.py test_self_consistency --mode authors --count 10 --dry-run
    python manage.py test_self_consistency --count 100 --reps 3 --mode works
"""

from __future__ import annotations

import random
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

from django.core.management.base import BaseCommand, CommandError
from pydantic import BaseModel

from core.management.commands.run_llm_elo import (
    Item,
    _DEFAULT_MODEL,
    _build_user_prompt,
    _ensure_api_key,
    _load_items,
    _load_system_prompt,
)

_DEFAULT_COUNT = 1000
_DEFAULT_REPS = 5
_DEFAULT_CONCURRENCY = 50
_DEFAULT_SEED = 42
_DEFAULT_EXAMPLES = 5
_MAX_PAIR_ATTEMPTS_FACTOR = 40

# Re-declare locally so this module doesn't depend on the internal name surviving.
class _Verdict(BaseModel):
    verdict: Literal[1, 2]


# ── pairing generation ────────────────────────────────────────────────────────

def _generate_random_pairings(
    items: list[Item],
    count: int,
    rng: random.Random,
) -> list[tuple[Item, Item]]:
    """Uniform random sampling — no ELO-based weighting."""
    n = len(items)
    batch_seen: set[tuple[int, int]] = set()
    max_attempts = count * _MAX_PAIR_ATTEMPTS_FACTOR
    attempts = 0
    pairings: list[tuple[Item, Item]] = []

    while len(pairings) < count:
        attempts += 1
        if attempts > max_attempts:
            raise CommandError(
                f"Could not generate {count} unique pairings after {max_attempts} attempts "
                f"(only {n} items in DB)."
            )
        i, j = rng.sample(range(n), 2)
        item_a, item_b = items[i], items[j]
        key = (min(item_a.pk, item_b.pk), max(item_a.pk, item_b.pk))
        if key in batch_seen:
            continue
        batch_seen.add(key)
        pairings.append((item_a, item_b))

    return pairings


# ── API call ──────────────────────────────────────────────────────────────────

def _call_api_rep(
    pairing_idx: int,
    rep_idx: int,
    item_a: Item,
    item_b: Item,
    mode: str,
    system_prompt: str,
    llm_model: str,
    client,
) -> tuple[int, int, int]:
    """Returns (pairing_idx, rep_idx, verdict)."""
    user_prompt = _build_user_prompt(item_a, item_b, mode)
    response = client.responses.parse(
        model=llm_model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        text_format=_Verdict,
    )
    if response.status != "completed":
        raise RuntimeError(
            f"Incomplete response for pairing {pairing_idx} rep {rep_idx}: "
            f"{response.incomplete_details}"
        )
    parsed = response.output_parsed
    if parsed is None:
        raise RuntimeError(f"No parsed verdict for pairing {pairing_idx} rep {rep_idx}.")
    return pairing_idx, rep_idx, parsed.verdict


def _run_all_reps(
    pairings: list[tuple[Item, Item]],
    mode: str,
    system_prompt: str,
    llm_model: str,
    concurrency: int,
    reps: int,
    stderr,
) -> dict[int, list[int]]:
    """Returns pairing_idx → list of verdicts (one per rep)."""
    from openai import OpenAI

    client = OpenAI()
    total_calls = len(pairings) * reps
    completed = 0
    results: dict[int, list[int]] = {i: [] for i in range(len(pairings))}

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(
                _call_api_rep, i, r, a, b, mode, system_prompt, llm_model, client
            ): (i, r)
            for i, (a, b) in enumerate(pairings)
            for r in range(reps)
        }
        for future in as_completed(futures):
            pairing_idx, rep_idx, verdict = future.result()
            results[pairing_idx].append(verdict)
            completed += 1
            stderr.write(f"  {completed}/{total_calls} calls completed\r")

    stderr.write("\n")
    return results


# ── statistics ────────────────────────────────────────────────────────────────

def _consistency_score(verdicts: list[int]) -> float:
    """Fraction of verdicts matching the majority (0.0–1.0)."""
    if not verdicts:
        return 0.0
    n = len(verdicts)
    count_1 = verdicts.count(1)
    count_2 = verdicts.count(2)
    return max(count_1, count_2) / n


def _compute_stats(
    pairings: list[tuple[Item, Item]],
    verdicts_by_idx: dict[int, list[int]],
    reps: int,
) -> tuple[list[float], dict[int, int]]:
    """
    Returns:
        scores: per-pairing consistency scores (index-aligned with pairings)
        distribution: {numerator → count} e.g. {5: 241, 4: 178, 3: 81}
    """
    scores = [_consistency_score(verdicts_by_idx[i]) for i in range(len(pairings))]
    # numerator = round(score * reps)
    dist: dict[int, int] = Counter(round(s * reps) for s in scores)
    return scores, dist


# ── formatting ────────────────────────────────────────────────────────────────

def _item_label(item: Item, mode: str) -> str:
    if mode == "authors":
        return item.name  # type: ignore[union-attr]
    return f"{item.title} — {item.author.name}"  # type: ignore[union-attr]


def _fmt_table_row(num: int, col_a: str, col_b: str, col_c: str, widths: tuple[int, int, int]) -> str:
    wa, wb, wc = widths
    return f"  {num:>3}.  {col_a:<{wa}}  {col_b:<{wb}}  {col_c:<{wc}}"


def _fmt_header(col_a: str, col_b: str, col_c: str, widths: tuple[int, int, int]) -> str:
    wa, wb, wc = widths
    return f"  {'#':>3}   {col_a:<{wa}}  {col_b:<{wb}}  {col_c:<{wc}}"


def _print_examples(
    label: str,
    indices: list[int],
    pairings: list[tuple[Item, Item]],
    verdicts_by_idx: dict[int, list[int]],
    mode: str,
    reps: int,
    rng: random.Random,
    n_examples: int,
    stdout,
) -> None:
    sample = rng.sample(indices, min(n_examples, len(indices)))
    if mode == "authors":
        h_a, h_b = "Author A", "Author B"
    else:
        h_a, h_b = "Work A", "Work B"

    rows = []
    for idx in sample:
        item_a, item_b = pairings[idx]
        vs = verdicts_by_idx[idx]
        count_1 = vs.count(1)
        count_2 = vs.count(2)
        label_a = _item_label(item_a, mode)
        label_b = _item_label(item_b, mode)
        if count_1 == reps:
            verdict_str = f"A ({count_1}-0)"
        elif count_2 == reps:
            verdict_str = f"B (0-{count_2})"
        else:
            verdict_str = f"A:{count_1}  B:{count_2}"
        rows.append((label_a, label_b, verdict_str))

    wa = max(len(h_a), max(len(r[0]) for r in rows))
    wb = max(len(h_b), max(len(r[1]) for r in rows))
    wc = max(7, max(len(r[2]) for r in rows))
    widths = (wa, wb, wc)

    stdout.write(f"\n{label}")
    stdout.write(_fmt_header(h_a, h_b, "Result", widths))
    stdout.write("  " + "-" * (5 + wa + wb + wc + 4))
    for num, (la, lb, v) in enumerate(rows, 1):
        stdout.write(_fmt_table_row(num, la, lb, v, widths))


def _report_mode(
    pairings: list[tuple[Item, Item]],
    verdicts_by_idx: dict[int, list[int]],
    mode: str,
    reps: int,
    n_examples: int,
    rng: random.Random,
    stdout,
) -> None:
    mode_label = "AUTHORS" if mode == "authors" else "WORKS"
    scores, dist = _compute_stats(pairings, verdicts_by_idx, reps)
    overall = statistics.mean(scores) if scores else 0.0

    stdout.write(f"\n{'='*60}")
    stdout.write(f"  {mode_label}  ({len(pairings)} pairings, {reps} reps each)")
    stdout.write(f"{'='*60}")

    stdout.write("\nConsistency distribution:")
    for numerator in sorted(dist.keys(), reverse=True):
        pct_agree = 100 * numerator / reps
        count = dist[numerator]
        pct_pairs = 100 * count / len(pairings)
        stdout.write(f"  {numerator}/{reps} ({pct_agree:5.1f}%):  {count:>5} pairings ({pct_pairs:.1f}%)")

    stdout.write(
        f"\nOverall self-consistency:  {100*overall:.1f}%  "
        f"|  Inconsistency rate:  {100*(1-overall):.1f}%"
    )

    # Group by tier
    by_tier: dict[int, list[int]] = {}
    for i, s in enumerate(scores):
        key = round(s * reps)
        by_tier.setdefault(key, []).append(i)

    # Perfectly consistent
    top_tier = reps
    if top_tier in by_tier and by_tier[top_tier]:
        _print_examples(
            f"\nPerfectly consistent examples ({top_tier}/{reps}) "
            f"— {min(n_examples, len(by_tier[top_tier]))} random samples:",
            by_tier[top_tier],
            pairings,
            verdicts_by_idx,
            mode,
            reps,
            rng,
            n_examples,
            stdout,
        )
    else:
        stdout.write(f"\n(No perfectly consistent pairings at {top_tier}/{reps}.)")

    # Least consistent
    bottom_tier = min(by_tier.keys())
    if bottom_tier < top_tier and by_tier[bottom_tier]:
        _print_examples(
            f"\nLeast consistent examples ({bottom_tier}/{reps}) "
            f"— {min(n_examples, len(by_tier[bottom_tier]))} random samples:",
            by_tier[bottom_tier],
            pairings,
            verdicts_by_idx,
            mode,
            reps,
            rng,
            n_examples,
            stdout,
        )
    else:
        stdout.write(f"\n(All pairings are perfectly consistent — no inconsistent examples.)")

    stdout.write("")


# ── dry-run ───────────────────────────────────────────────────────────────────

def _print_dry_run(
    pairings: list[tuple[Item, Item]],
    system_prompt: str,
    llm_model: str,
    mode: str,
    reps: int,
    stdout,
) -> None:
    sys_tokens = max(1, len(system_prompt) // 4)
    total_calls = len(pairings) * reps
    stdout.write(f"\n{'='*60}")
    stdout.write(f"DRY RUN — model: {llm_model}")
    stdout.write(f"System prompt ({sys_tokens} est. tokens): {system_prompt!r}")
    stdout.write(f"{'='*60}")
    total_tokens = 0
    for i, (item_a, item_b) in enumerate(pairings):
        user_prompt = _build_user_prompt(item_a, item_b, mode)
        usr_tokens = max(1, len(user_prompt) // 4)
        call_tokens = sys_tokens + usr_tokens + 1
        total_tokens += call_tokens
        stdout.write(f"  [{i+1:3d}] ~{call_tokens} tokens | {user_prompt!r}")
    avg = total_tokens / len(pairings) if pairings else 0
    stdout.write(
        f"\nTotal: {len(pairings)} pairings × {reps} reps = {total_calls} calls, "
        f"~{total_tokens * reps} tokens (~{avg:.1f}/matchup)"
    )


# ── command ───────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = (
        "Empirically test LLM self-consistency: run the same pairings multiple times "
        "and report agreement rates."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=_DEFAULT_COUNT,
            help=f"Number of pairings per mode (default: {_DEFAULT_COUNT}; "
                 f"'both' mode runs this many for each).",
        )
        parser.add_argument(
            "--mode",
            choices=["authors", "works", "both"],
            default="both",
            help="Which entity type to test (default: both).",
        )
        parser.add_argument(
            "--reps",
            type=int,
            default=_DEFAULT_REPS,
            help=f"Repetitions per pairing (default: {_DEFAULT_REPS}).",
        )
        parser.add_argument(
            "--model",
            default=_DEFAULT_MODEL,
            help=f"OpenAI model to use (default: {_DEFAULT_MODEL}).",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=_DEFAULT_CONCURRENCY,
            help=f"Parallel API threads (default: {_DEFAULT_CONCURRENCY}).",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=_DEFAULT_SEED,
            help=f"Random seed for reproducible pairings (default: {_DEFAULT_SEED}).",
        )
        parser.add_argument(
            "--examples",
            type=int,
            default=_DEFAULT_EXAMPLES,
            help=f"Examples to show per consistency tier (default: {_DEFAULT_EXAMPLES}).",
        )
        parser.add_argument(
            "--system-prompt",
            type=Path,
            default=None,
            help="Path to system prompt file. Defaults to prompts/professor.md.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print pairings and token estimates without calling the API.",
        )

    def handle(self, *args, **options):
        count: int = options["count"]
        mode: str = options["mode"]
        reps: int = options["reps"]
        llm_model: str = options["model"]
        concurrency: int = options["concurrency"]
        seed: int = options["seed"]
        n_examples: int = options["examples"]
        dry_run: bool = options["dry_run"]

        if not dry_run:
            _ensure_api_key()

        system_prompt = _load_system_prompt(options["system_prompt"])
        modes = ["authors", "works"] if mode == "both" else [mode]

        total_calls = count * reps * len(modes)
        self.stdout.write(
            f"\n=== Self-Consistency Test ===\n"
            f"Model: {llm_model}  |  Seed: {seed}  |  {reps} reps per pairing\n"
            f"Pairings per mode: {count}  |  Total API calls: {total_calls}"
        )

        # Use two separate RNGs: one for pairing generation, one for example sampling.
        # Both are seeded deterministically so the whole run is reproducible.
        pairing_rng = random.Random(seed)
        sample_rng = random.Random(seed + 1)

        for m in modes:
            items = _load_items(m)
            if len(items) < 2:
                raise CommandError(f"Not enough {m} in the database.")

            pairings = _generate_random_pairings(items, count, pairing_rng)
            self.stdout.write(f"\nGenerated {len(pairings)} {m} pairings.")

            if dry_run:
                _print_dry_run(pairings, system_prompt, llm_model, m, reps, self.stdout)
                continue

            self.stderr.write(f"Running {len(pairings) * reps} API calls for {m}...\n")
            verdicts_by_idx = _run_all_reps(
                pairings, m, system_prompt, llm_model, concurrency, reps, self.stderr
            )

            _report_mode(
                pairings, verdicts_by_idx, m, reps, n_examples, sample_rng, self.stdout
            )
