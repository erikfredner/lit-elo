"""Management command: run LLM-based ELO matchups for authors or works.

Usage:
    python manage.py run_llm_elo --mode authors --count 50
    python manage.py run_llm_elo --mode works   --count 50
    python manage.py run_llm_elo --mode authors --count 10 --dry-run
"""

from __future__ import annotations

import math
import os
import random
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal, Union

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from pydantic import BaseModel

from core.elo import update as elo_update
from core.models import Author, LLMMatchup, Work

# Pairing parameters
_ELO_DIVISOR = 200              # exp(-elo_diff / 200): soft continuous proximity decay
_MAX_PAIR_ATTEMPTS_FACTOR = 40  # max attempts = count × this

_DEFAULT_MODEL = "gpt-5.4-nano"
_DEFAULT_CONCURRENCY = 10

_PROMPT_DIR = Path(__file__).resolve().parents[3] / "prompts"
_SYSTEM_PROMPT = _PROMPT_DIR / "system-prompt.md"

Item = Union[Author, Work]


class _Verdict(BaseModel):
    verdict: Literal[1, 2]


class Command(BaseCommand):
    help = "Run LLM canonicity matchups and update ELO ratings in the database."

    # ── argument parsing ──────────────────────────────────────────────────────

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode",
            choices=["authors", "works"],
            required=True,
            help="Whether to compare authors or works.",
        )
        parser.add_argument(
            "--count",
            type=int,
            default=50,
            help="Number of matchups to run (default: 50).",
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
            help=f"Number of parallel API threads (default: {_DEFAULT_CONCURRENCY}).",
        )
        parser.add_argument(
            "--system-prompt",
            type=Path,
            default=None,
            help="Path to system prompt file. Defaults to prompts/system-prompt.md.",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=None,
            help="Random seed for reproducible pairings.",
        )
        parser.add_argument(
            "--reps",
            type=int,
            default=1,
            help="Number of times to repeat the run, re-reading ELO ratings from DB between reps (default: 1).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print prompts and token estimates without calling the API.",
        )
        parser.add_argument(
            "--exclude-overrepresented",
            action="store_true",
            help=(
                "Exclude items whose total comparison count exceeds mean + 1 stdev "
                "before generating pairings."
            ),
        )

    # ── main entry point ──────────────────────────────────────────────────────

    def handle(self, *args, **options):
        mode: str = options["mode"]
        count: int = options["count"]
        reps: int = options["reps"]
        llm_model: str = options["model"]
        concurrency: int = options["concurrency"]
        dry_run: bool = options["dry_run"]
        seed: int | None = options["seed"]
        exclude_overrepresented: bool = options["exclude_overrepresented"]

        if not dry_run:
            _ensure_api_key()

        system_prompt = _load_system_prompt(options["system_prompt"])

        # Single shared RNG so each rep draws a different slice of the sequence,
        # but the whole run remains reproducible when --seed is given.
        rng = random.Random(seed)

        for rep in range(1, reps + 1):
            if reps > 1:
                self.stdout.write(f"\n── Rep {rep}/{reps} ──")

            # Reload items and matchup index each rep so ELO rankings and pair
            # history reflect any changes written by the previous rep.
            items = _load_items(mode)
            if len(items) < 2:
                raise CommandError(f"Not enough {mode} in the database to generate pairings.")

            pair_counts, games_played = _load_matchup_index(mode)

            if exclude_overrepresented:
                counts = [games_played.get(item.pk, 0) for item in items]
                stdev = statistics.pstdev(counts)
                if stdev == 0:
                    self.stdout.write(
                        "--exclude-overrepresented: all items have equal play counts; skipping filter."
                    )
                else:
                    mean = statistics.mean(counts)
                    threshold = mean + stdev
                    before = len(items)
                    items = [item for item in items if games_played.get(item.pk, 0) <= threshold]
                    excluded = before - len(items)
                    self.stdout.write(
                        f"--exclude-overrepresented: excluded {excluded} item(s) above threshold "
                        f"{threshold:.1f} (mean={mean:.1f}, stdev={stdev:.1f})."
                    )
                    if len(items) < 2:
                        raise CommandError(
                            f"After excluding overrepresented items, fewer than 2 {mode} remain."
                        )

            total_comparisons = sum(pair_counts.values())
            self.stdout.write(
                f"Loaded {len(pair_counts)} historical pairs ({total_comparisons} total comparisons)."
            )

            pairings = _generate_pairings(items, count, rng, pair_counts, games_played)
            self.stdout.write(f"Generated {len(pairings)} pairings for {mode}.")

            if dry_run:
                _print_dry_run(pairings, system_prompt, llm_model, mode, self.stdout)
                if reps > 1:
                    self.stdout.write("(subsequent reps would use updated ELO ratings)")
                return

            # Run API calls concurrently, then apply ELO sequentially
            verdicts = _evaluate_concurrently(
                pairings, system_prompt, llm_model, concurrency, mode, self.stderr
            )
            _apply_and_save(pairings, verdicts, mode, llm_model, self.stdout, self.style)

    # ── public for testing ────────────────────────────────────────────────────

    @staticmethod
    def build_user_prompt(item_a: Item, item_b: Item, mode: str) -> str:
        return _build_user_prompt(item_a, item_b, mode)


# ── helpers ───────────────────────────────────────────────────────────────────

def _ensure_api_key() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise CommandError("Missing OPENAI_API_KEY environment variable.")


def _load_system_prompt(path: Path | None) -> str:
    p = path or _SYSTEM_PROMPT
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CommandError(f"Cannot read system prompt: {exc}")


def _load_items(mode: str) -> list[Item]:
    if mode == "authors":
        return list(Author.objects.order_by("-elo_rating", "name"))
    return list(Work.objects.select_related("author").order_by("-elo_rating", "title"))


def _load_matchup_index(mode: str) -> tuple[dict[tuple[int, int], int], dict[int, int]]:
    """Return (pair_counts, games_played_per_pk) from all historical LLMMatchup rows."""
    content_type = "author" if mode == "authors" else "work"
    pair_counts: dict[tuple[int, int], int] = {}
    item_counts: dict[int, int] = {}
    for a, b in LLMMatchup.objects.filter(content_type=content_type).values_list(
        "item_a_id", "item_b_id"
    ):
        key = (min(a, b), max(a, b))
        pair_counts[key] = pair_counts.get(key, 0) + 1
        item_counts[a] = item_counts.get(a, 0) + 1
        item_counts[b] = item_counts.get(b, 0) + 1
    return pair_counts, item_counts


def _build_user_prompt(item_a: Item, item_b: Item, mode: str) -> str:
    if mode == "authors":
        label_a = item_a.name          # type: ignore[union-attr]
        label_b = item_b.name          # type: ignore[union-attr]
    else:
        label_a = f"{item_a.title} — {item_a.author.name}"  # type: ignore[union-attr]
        label_b = f"{item_b.title} — {item_b.author.name}"  # type: ignore[union-attr]
    return f"1: {label_a}\n2: {label_b}"


def _choose_second(
    item_a: Item,
    items: list[Item],
    rng: random.Random,
    games_played: dict[int, int],
    pair_counts: dict[tuple[int, int], int],
) -> Item:
    candidates = [x for x in items if x.pk != item_a.pk]
    weights = []
    for x in candidates:
        elo_diff = abs(item_a.elo_rating - x.elo_rating)
        elo_proximity = math.exp(-elo_diff / _ELO_DIVISOR)
        novelty_b = 1.0 / math.sqrt(games_played.get(x.pk, 0) + 1)
        pair_key = (min(item_a.pk, x.pk), max(item_a.pk, x.pk))
        pair_novelty = 1.0 / math.sqrt(pair_counts.get(pair_key, 0) + 1)
        weights.append(elo_proximity * novelty_b * pair_novelty)
    return rng.choices(candidates, weights=weights)[0]


def _generate_pairings(
    items: list[Item],
    count: int,
    rng: random.Random,
    pair_counts: dict[tuple[int, int], int] | None = None,
    games_played: dict[int, int] | None = None,
) -> list[tuple[Item, Item]]:
    pc = pair_counts or {}
    gp = games_played or {}
    all_possible = len(items) * (len(items) - 1) // 2

    # batch_seen prevents the same pair from appearing twice in one run.
    # Historical pairs are not hard-blocked; pair_counts handles them softly.
    batch_seen: set[tuple[int, int]] = set()
    max_attempts = count * _MAX_PAIR_ATTEMPTS_FACTOR
    attempts = 0
    pairings: list[tuple[Item, Item]] = []

    # Pre-compute novelty weights for item A — static for this batch
    novelty_weights_a = [1.0 / math.sqrt(gp.get(item.pk, 0) + 1) for item in items]

    while len(pairings) < count:
        attempts += 1
        if attempts > max_attempts:
            if len(batch_seen) >= all_possible:
                # Every possible pair is already in this batch; start a new cycle.
                batch_seen.clear()
                attempts = 0
            else:
                raise CommandError(
                    f"Could not generate {count} pairings after {max_attempts} attempts."
                )
        item_a = rng.choices(items, weights=novelty_weights_a)[0]
        item_b = _choose_second(item_a, items, rng, gp, pc)

        # Treat (a, b) and (b, a) as the same matchup
        key = (min(item_a.pk, item_b.pk), max(item_a.pk, item_b.pk))
        if key in batch_seen:
            continue
        batch_seen.add(key)
        pairings.append((item_a, item_b))

    return pairings


def _call_api(
    idx: int,
    item_a: Item,
    item_b: Item,
    mode: str,
    system_prompt: str,
    llm_model: str,
    client,
) -> tuple[int, int]:
    from openai import OpenAI  # imported here so dry-run never imports openai
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
        raise RuntimeError(f"Incomplete response for matchup {idx}: {response.incomplete_details}")
    parsed = response.output_parsed
    if parsed is None:
        raise RuntimeError(f"No parsed verdict for matchup {idx}.")
    return idx, parsed.verdict


def _evaluate_concurrently(
    pairings: list[tuple[Item, Item]],
    system_prompt: str,
    llm_model: str,
    concurrency: int,
    mode: str,
    stderr,
) -> dict[int, int]:
    from openai import OpenAI
    client = OpenAI()
    results: dict[int, int] = {}

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(_call_api, i, a, b, mode, system_prompt, llm_model, client): i
            for i, (a, b) in enumerate(pairings)
        }
        for future in as_completed(futures):
            idx = futures[future]
            idx, verdict = future.result()  # let exceptions propagate
            results[idx] = verdict
            stderr.write(f"  [{idx + 1}/{len(pairings)}] verdict={verdict}\n")

    return results


@transaction.atomic
def _apply_and_save(
    pairings: list[tuple[Item, Item]],
    verdicts: dict[int, int],
    mode: str,
    llm_model: str,
    stdout,
    style,
) -> None:
    content_type = "author" if mode == "authors" else "work"
    matchups: list[LLMMatchup] = []
    updated: dict[int, Item] = {}  # pk → item with updated elo_rating

    for i, (item_a, item_b) in enumerate(pairings):
        verdict = verdicts[i]
        result = 1.0 if verdict == 1 else 0.0
        winner = "A" if verdict == 1 else "B"

        elo_a_before = item_a.elo_rating
        elo_b_before = item_b.elo_rating
        new_a, new_b = elo_update(elo_a_before, elo_b_before, result)

        item_a.elo_rating = new_a
        item_b.elo_rating = new_b
        updated[item_a.pk] = item_a
        updated[item_b.pk] = item_b

        matchups.append(
            LLMMatchup(
                content_type=content_type,
                item_a_id=item_a.pk,
                item_b_id=item_b.pk,
                winner=winner,
                elo_a_before=elo_a_before,
                elo_b_before=elo_b_before,
                elo_a_after=new_a,
                elo_b_after=new_b,
                model_used=llm_model,
            )
        )

    model_cls = Author if mode == "authors" else Work
    model_cls.objects.bulk_update(list(updated.values()), ["elo_rating"])
    LLMMatchup.objects.bulk_create(matchups)

    stdout.write(
        style.SUCCESS(
            f"Saved {len(matchups)} matchups; updated {len(updated)} {content_type} ELO ratings."
        )
    )


def _token_estimate(text: str) -> int:
    """Rough estimate: ~4 characters per token."""
    return max(1, len(text) // 4)


def _print_dry_run(
    pairings: list[tuple[Item, Item]],
    system_prompt: str,
    llm_model: str,
    mode: str,
    stdout,
) -> None:
    sys_tokens = _token_estimate(system_prompt)
    stdout.write(f"\n{'='*60}")
    stdout.write(f"DRY RUN — model: {llm_model}")
    stdout.write(f"System prompt ({sys_tokens} est. tokens):")
    stdout.write(f"  {system_prompt!r}")
    stdout.write(f"{'='*60}\n")

    total_tokens = 0
    for i, (item_a, item_b) in enumerate(pairings):
        user_prompt = _build_user_prompt(item_a, item_b, mode)
        usr_tokens = _token_estimate(user_prompt)
        resp_tokens = 1
        call_tokens = sys_tokens + usr_tokens + resp_tokens
        total_tokens += call_tokens
        stdout.write(f"[{i + 1:3d}] ~{call_tokens:3d} tokens | {user_prompt!r}")

    avg = total_tokens / len(pairings) if pairings else 0
    stdout.write(
        f"\nTotal: ~{total_tokens} tokens across {len(pairings)} matchups (~{avg:.1f}/matchup)"
    )
