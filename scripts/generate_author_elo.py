#!/usr/bin/env python3
"""Compute scaled Elo ratings for authors based on work counts."""

from __future__ import annotations

import argparse
import csv
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

ELO_MIN = 1000.0
ELO_MAX = 3000.0


@dataclass(frozen=True)
class AuthorRating:
    name: str
    count: int
    zscore: float
    elo: float


def load_counts(csv_path: Path) -> List[tuple[str, int]]:
    """Read author counts from the provided CSV file."""
    records: List[tuple[str, int]] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            name = row.get("subjectAuthor")
            count_field = row.get("count")
            if not name or not count_field:
                continue
            try:
                count = int(count_field)
            except ValueError:
                continue
            records.append((name, count))
    if not records:
        raise ValueError(f"No valid author records found in {csv_path}")
    return records


def compute_zscores(records: Iterable[tuple[str, int]]) -> List[tuple[str, int, float]]:
    records_list = list(records)
    counts = [count for _, count in records_list]
    if len(counts) < 2:
        raise ValueError("Need at least two counts to compute z-scores")
    mean = statistics.mean(counts)
    stdev = statistics.stdev(counts)
    if stdev == 0:
        raise ValueError("Standard deviation is zero; cannot compute z-scores")
    with_zscores: List[tuple[str, int, float]] = []
    for name, count in records_list:
        zscore = (count - mean) / stdev
        with_zscores.append((name, count, zscore))
    return with_zscores


def scale_to_elo(zscores: Iterable[tuple[str, int, float]]) -> List[AuthorRating]:
    entries = list(zscores)
    z_values = [z for _, _, z in entries]
    min_z = min(z_values)
    max_z = max(z_values)
    if min_z == max_z:
        midpoint = (ELO_MIN + ELO_MAX) / 2
        return [AuthorRating(name=name, count=count, zscore=z, elo=midpoint) for name, count, z in entries]
    scale = (ELO_MAX - ELO_MIN) / (max_z - min_z)
    ratings: List[AuthorRating] = []
    for name, count, zscore in entries:
        elo = ELO_MIN + (zscore - min_z) * scale
        ratings.append(AuthorRating(name=name, count=count, zscore=zscore, elo=elo))
    return ratings


def write_output(ratings: Iterable[AuthorRating], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["subjectAuthor", "count", "zscore", "elo"])
        for rating in ratings:
            writer.writerow(
                [
                    rating.name,
                    rating.count,
                    f"{rating.zscore:.6f}",
                    f"{rating.elo:.2f}",
                ]
            )


def build_argument_parser() -> argparse.ArgumentParser:
    default_data_dir = Path(__file__).resolve().parent.parent / "data"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=default_data_dir / "mlaib_authors.csv",
        help="Path to the mlaib_authors.csv file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_data_dir / "elo" / "author_elo_0.csv",
        help="Where to write the Elo output CSV",
    )
    return parser


def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()

    records = load_counts(args.input)
    zscores = compute_zscores(records)
    ratings = scale_to_elo(zscores)
    write_output(ratings, args.output)


if __name__ == "__main__":
    main()
