#!/usr/bin/env python3
"""Update author Elo ratings using a pairings results file."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.constants import DEFAULT_ELO_RATING
from core.elo import update as elo_update


PAIRINGS_FILENAME_RE = re.compile(r"pairings_(\d+)\.csv$")


@dataclass(frozen=True)
class Pairing:
    author_one: str
    author_two: str
    result: float  # 1.0 if author_one wins, 0.0 if author_two wins, 0.5 if draw


def parse_round_index(pairings_path: Path) -> int:
    """Extract the numeric suffix from a pairings filename."""
    match = PAIRINGS_FILENAME_RE.search(pairings_path.name)
    if not match:
        raise ValueError(
            f"Unable to determine round number from pairings filename: {pairings_path.name}"
        )
    return int(match.group(1))


def load_previous_ratings(csv_path: Path) -> Dict[str, float]:
    """Read previous Elo ratings."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Previous Elo file does not exist: {csv_path}")

    ratings: Dict[str, float] = {}

    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        if "subjectAuthor" not in fieldnames or "elo" not in fieldnames:
            raise ValueError(
                "Previous Elo CSV must contain 'subjectAuthor' and 'elo' columns"
            )

        for row_number, row in enumerate(reader, start=2):
            name = (row.get("subjectAuthor") or "").strip()
            elo_value = (row.get("elo") or "").strip()
            if not name or not elo_value:
                continue
            try:
                rating = float(elo_value)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid Elo value '{elo_value}' for author '{name}' on line {row_number}"
                ) from exc
            ratings[name] = rating

    return ratings


def parse_result_field(raw_value: str, row_number: int) -> float:
    """Convert a llm_verdict field to a numeric Elo result."""
    value = (raw_value or "").strip().lower()
    if value in {"1", "1.0", "author_1", "a", "win_1"}:
        return 1.0
    if value in {"2", "2.0", "author_2", "b", "win_2"}:
        return 0.0
    if value in {"0", "0.0", "draw", "tie", "0.5", "half"}:
        return 0.5
    try:
        numeric = float(value)
    except ValueError as exc:
        raise ValueError(
            f"Unrecognised llm_verdict '{raw_value}' on line {row_number}; expected 1, 2, or draw"
        ) from exc
    if numeric == 1.0:
        return 1.0
    if numeric == 2.0:
        return 0.0
    if 0.0 <= numeric <= 1.0:
        return numeric
    raise ValueError(
        f"Unsupported numeric llm_verdict '{raw_value}' on line {row_number}"
    )


def load_pairings(csv_path: Path) -> List[Pairing]:
    """Read pairings and their outcomes from a CSV file."""
    pairings: List[Pairing] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        required = {"author_1", "author_2", "llm_verdict"}
        missing = required - set(fieldnames)
        if missing:
            raise ValueError(
                f"Pairings CSV is missing required columns: {', '.join(sorted(missing))}"
            )

        for row_number, row in enumerate(reader, start=2):
            author_one = (row.get("author_1") or "").strip()
            author_two = (row.get("author_2") or "").strip()
            verdict_raw = row.get("llm_verdict")
            if not author_one or not author_two or verdict_raw is None:
                raise ValueError(
                    f"Incomplete pairing data on line {row_number}: {row}"
                )
            result = parse_result_field(str(verdict_raw), row_number)
            pairings.append(Pairing(author_one, author_two, result))

    return pairings


def apply_pairings(
    ratings: Dict[str, float],
    pairings: Sequence[Pairing],
    baseline: Dict[str, float],
    default_rating: float = DEFAULT_ELO_RATING,
) -> None:
    """Apply pairings to the ratings mapping in place."""

    for pairing in pairings:
        if pairing.author_one not in ratings:
            ratings[pairing.author_one] = default_rating
            baseline.setdefault(pairing.author_one, default_rating)
        if pairing.author_two not in ratings:
            ratings[pairing.author_two] = default_rating
            baseline.setdefault(pairing.author_two, default_rating)

        new_rating_one, new_rating_two = elo_update(
            ratings[pairing.author_one], ratings[pairing.author_two], pairing.result
        )
        ratings[pairing.author_one] = new_rating_one
        ratings[pairing.author_two] = new_rating_two


def compute_diffs(ratings: Dict[str, float], baseline: Dict[str, float]) -> Dict[str, float]:
    """Calculate rating deltas relative to the baseline."""
    diffs: Dict[str, float] = {}
    for name, rating in ratings.items():
        previous = baseline.get(name, DEFAULT_ELO_RATING)
        diffs[name] = rating - previous
    return diffs


def sort_authors(
    ratings: Dict[str, float],
    diffs: Dict[str, float],
) -> List[Tuple[str, float, float]]:
    """Return authors sorted by diff (desc) then Elo (desc)."""
    rows: List[Tuple[str, float, float]] = []
    for name in ratings:
        rows.append((name, ratings[name], diffs.get(name, 0.0)))
    rows.sort(key=lambda item: (-item[2], -item[1], item[0]))
    return rows


def write_ratings(
    ratings: Dict[str, float],
    diffs: Dict[str, float],
    output_path: Path,
) -> None:
    """Write updated ratings to a CSV file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["subjectAuthor", "elo", "diff"])
        for name, rating, diff in sort_authors(ratings, diffs):
            writer.writerow([name, f"{round(rating):.0f}", f"{diff:+.2f}"])


def build_argument_parser() -> argparse.ArgumentParser:
    data_dir = Path(__file__).resolve().parent.parent / "data"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pairings",
        type=Path,
        required=True,
        help="Path to the pairings CSV (e.g. data/pairings/pairings_1.csv)",
    )
    parser.add_argument(
        "--previous",
        type=Path,
        help="Path to the previous Elo CSV. If omitted, derived from the pairings round number.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Where to write the updated Elo CSV. Defaults alongside the previous file with the next index.",
    )
    parser.add_argument(
        "--default-elo",
        type=float,
        default=DEFAULT_ELO_RATING,
        help=f"Starting Elo for authors missing from the previous file (default: {DEFAULT_ELO_RATING})",
    )
    parser.add_argument(
        "--elo-dir",
        type=Path,
        default=data_dir / "elo",
        help="Directory containing Elo CSV files. Used when deriving previous/output paths.",
    )
    return parser


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    round_index = parse_round_index(args.pairings)
    if round_index == 0:
        raise ValueError("Pairings round must be greater than zero to derive previous Elo file")

    if args.previous is not None:
        previous_path = args.previous
    else:
        previous_path = args.elo_dir / f"author_elo_{round_index - 1}.csv"

    if args.output is not None:
        output_path = args.output
    else:
        output_path = args.elo_dir / f"author_elo_{round_index}.csv"

    return previous_path, output_path


def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()

    previous_path, output_path = resolve_paths(args)
    ratings = load_previous_ratings(previous_path)
    baseline = dict(ratings)
    pairings = load_pairings(args.pairings)
    apply_pairings(ratings, pairings, baseline, default_rating=args.default_elo)
    diffs = compute_diffs(ratings, baseline)
    write_ratings(ratings, diffs, output_path)


if __name__ == "__main__":
    main()
