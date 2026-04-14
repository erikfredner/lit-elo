"""Management command: run LLM-based ELO matchups for authors or works.

Usage:
    python manage.py run_llm_elo --mode authors --count 50
    python manage.py run_llm_elo --mode works   --count 50
    python manage.py run_llm_elo --mode authors --count 10 --dry-run
"""

from __future__ import annotations

import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal, Union

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from pydantic import BaseModel

from core.elo import update as elo_update
from core.models import Author, LLMMatchup, Work

# Pairing parameters (match generate_pairings.py)
_ELO_TOLERANCE = 0.20          # prefer matches within ±20 % of item A's ELO
_EXPLORE_PROB = 0.05            # 5 % chance of ignoring the band
_MAX_PAIR_ATTEMPTS_FACTOR = 40  # max attempts = count × this

_DEFAULT_MODEL = "gpt-5.4-nano"
_DEFAULT_CONCURRENCY = 10

_PROMPT_DIR = Path(__file__).resolve().parents[3] / "prompts"
_AUTHOR_SYSTEM_PROMPT = _PROMPT_DIR / "authors-system-v4.md"
_WORK_SYSTEM_PROMPT = _PROMPT_DIR / "works-system-v1.md"

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
            help="Path to system prompt file. Defaults to prompts/authors-system-v4.md or prompts/works-system-v1.md.",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=None,
            help="Random seed for reproducible pairings.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print prompts and token estimates without calling the API.",
        )

    # ── main entry point ──────────────────────────────────────────────────────

    def handle(self, *args, **options):
        mode: str = options["mode"]
        count: int = options["count"]
        llm_model: str = options["model"]
        concurrency: int = options["concurrency"]
        dry_run: bool = options["dry_run"]
        seed: int | None = options["seed"]

        if not dry_run:
            _ensure_api_key()

        system_prompt = _load_system_prompt(options["system_prompt"], mode)
        items = _load_items(mode)
        if len(items) < 2:
            raise CommandError(f"Not enough {mode} in the database to generate pairings.")

        rng = random.Random(seed)
        pairings = _generate_pairings(items, count, rng)
        self.stdout.write(f"Generated {len(pairings)} pairings for {mode}.")

        if dry_run:
            _print_dry_run(pairings, system_prompt, llm_model, mode, self.stdout)
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


def _load_system_prompt(path: Path | None, mode: str) -> str:
    p = path or (_AUTHOR_SYSTEM_PROMPT if mode == "authors" else _WORK_SYSTEM_PROMPT)
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CommandError(f"Cannot read system prompt: {exc}")


def _load_items(mode: str) -> list[Item]:
    if mode == "authors":
        return list(Author.objects.order_by("-elo_rating", "name"))
    return list(Work.objects.select_related("author").order_by("-elo_rating", "title"))


def _build_user_prompt(item_a: Item, item_b: Item, mode: str) -> str:
    if mode == "authors":
        label_a = item_a.name          # type: ignore[union-attr]
        label_b = item_b.name          # type: ignore[union-attr]
    else:
        label_a = f"{item_a.title} — {item_a.author.name}"  # type: ignore[union-attr]
        label_b = f"{item_b.title} — {item_b.author.name}"  # type: ignore[union-attr]
    return f"1: {label_a}\n2: {label_b}"


def _choose_second(item_a: Item, items: list[Item], rng: random.Random) -> Item:
    target = item_a.elo_rating
    tolerance = max(abs(target) * _ELO_TOLERANCE, 1.0)
    in_band = [x for x in items if x.pk != item_a.pk and (target - tolerance) <= x.elo_rating <= (target + tolerance)]
    others = [x for x in items if x.pk != item_a.pk]
    if in_band and rng.random() >= _EXPLORE_PROB:
        return rng.choice(in_band)
    return rng.choice(others)


def _generate_pairings(items: list[Item], count: int, rng: random.Random) -> list[tuple[Item, Item]]:
    pairings: list[tuple[Item, Item]] = []
    seen: set[tuple[int, int]] = set()
    max_attempts = count * _MAX_PAIR_ATTEMPTS_FACTOR
    attempts = 0

    while len(pairings) < count:
        attempts += 1
        if attempts > max_attempts:
            raise CommandError(
                f"Could not generate {count} unique pairings after {max_attempts} attempts."
            )
        item_a = rng.choice(items)
        item_b = _choose_second(item_a, items, rng)

        # Treat (a, b) and (b, a) as the same matchup
        key = (min(item_a.pk, item_b.pk), max(item_a.pk, item_b.pk))
        if key in seen:
            continue
        seen.add(key)
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
