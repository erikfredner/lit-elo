#!/usr/bin/env python3
"""Generate random author pairings for ELO simulations."""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

ELO_TOLERANCE = 0.2
EXPLORATION_PROBABILITY = 0.05


@dataclass(frozen=True)
class Author:
    name: str
    elo: float


def load_authors(csv_path: Path) -> List[Author]:
    authors: List[Author] = []

    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            name = row.get("subjectAuthor")
            elo_field = row.get("elo")
            if not name or not elo_field:
                continue
            try:
                elo = float(elo_field)
            except ValueError:
                continue
            authors.append(Author(name=name, elo=elo))

    if not authors:
        raise ValueError(f"No author records found in {csv_path}")

    return authors


def choose_author_two(
    author_one: Author,
    authors: Sequence[Author],
    rng: random.Random,
    elo_tolerance: float = ELO_TOLERANCE,
    exploration_probability: float = EXPLORATION_PROBABILITY,
) -> Author:
    """Pick Author 2, preferring similar ELO authors with occasional exploration."""

    target = author_one.elo
    tolerance = max(abs(target) * elo_tolerance, 1.0)
    lower = target - tolerance
    upper = target + tolerance

    in_band = [author for author in authors if author is not author_one and lower <= author.elo <= upper]
    off_band = [author for author in authors if author is not author_one]

    if not off_band:
        raise ValueError("Author list is too small to generate pairings")

    if in_band and rng.random() >= exploration_probability:
        return rng.choice(in_band)

    return rng.choice(off_band)


_GLOBAL_AUTHORS: Sequence[Author] = ()


def _worker_init(authors: Sequence[Author]) -> None:
    global _GLOBAL_AUTHORS
    _GLOBAL_AUTHORS = authors


def _generate_chunk(args: tuple[int, int, float, float]) -> List[tuple[Author, Author]]:
    chunk_size, seed, elo_tolerance, exploration_probability = args
    rng = random.Random(seed)
    chunk: List[tuple[Author, Author]] = []
    seen: set[tuple[str, str]] = set()
    attempts = 0
    max_attempts = max(chunk_size * 40, 1_000)

    while len(chunk) < chunk_size:
        attempts += 1
        if attempts > max_attempts:
            raise RuntimeError("Unable to generate enough unique pairings in worker chunk")

        author_one = rng.choice(_GLOBAL_AUTHORS)
        author_two = choose_author_two(
            author_one,
            _GLOBAL_AUTHORS,
            rng,
            elo_tolerance=elo_tolerance,
            exploration_probability=exploration_probability,
        )
        key = (author_one.name, author_two.name)
        if key in seen:
            continue

        seen.add(key)
        chunk.append((author_one, author_two))

    return chunk


def _generate_pairings_sequential(
    authors: Sequence[Author],
    pairings_count: int,
    rng: random.Random,
) -> List[tuple[Author, Author]]:
    pairings: List[tuple[Author, Author]] = []
    seen: set[tuple[str, str]] = set()
    max_attempts = pairings_count * 40
    attempts = 0

    while len(pairings) < pairings_count:
        attempts += 1
        if attempts > max_attempts:
            raise RuntimeError("Unable to generate the requested number of unique pairings")

        author_one = rng.choice(authors)
        author_two = choose_author_two(author_one, authors, rng)
        key = (author_one.name, author_two.name)
        if key in seen:
            continue

        seen.add(key)
        pairings.append((author_one, author_two))

    return pairings


def generate_pairings(
    authors: Sequence[Author],
    pairings_count: int,
    rng: random.Random,
    processes: int | None = None,
) -> List[tuple[Author, Author]]:
    if pairings_count <= 0:
        return []
    if len(authors) < 2:
        raise ValueError("At least two authors required to generate pairings")

    if processes is None:
        processes = min(mp.cpu_count() or 1, pairings_count, len(authors))
    processes = max(1, processes)

    if processes == 1:
        return _generate_pairings_sequential(authors, pairings_count, rng)

    chunk_size = max(1, (pairings_count + processes - 1) // processes)
    pairings: List[tuple[Author, Author]] = []
    seen: set[tuple[str, str]] = set()

    with mp.Pool(processes=processes, initializer=_worker_init, initargs=(authors,)) as pool:
        while len(pairings) < pairings_count:
            tasks = [
                pool.apply_async(
                    _generate_chunk,
                    args=((chunk_size, rng.randint(0, 2**31 - 1), ELO_TOLERANCE, EXPLORATION_PROBABILITY),),
                )
                for _ in range(processes)
            ]
            progress = False
            for task in tasks:
                chunk = task.get()
                for author_one, author_two in chunk:
                    key = (author_one.name, author_two.name)
                    if key in seen:
                        continue
                    seen.add(key)
                    pairings.append((author_one, author_two))
                    progress = True
                    if len(pairings) >= pairings_count:
                        break
                if len(pairings) >= pairings_count:
                    break
            if not progress:
                raise RuntimeError("Unable to generate the requested number of unique pairings")

    return pairings


def next_output_path(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    max_existing = 0
    pattern = re.compile(r"pairings_(\d+)\.csv$")
    for path in output_dir.iterdir():
        match = pattern.match(path.name)
        if match:
            max_existing = max(max_existing, int(match.group(1)))
    next_index = max_existing + 1
    return output_dir / f"pairings_{next_index}.csv"


def write_pairings(pairings: Iterable[tuple[Author, Author]], output_path: Path) -> None:
    fieldnames = [
        "author_1",
        "author_1_elo",
        "author_2",
        "author_2_elo",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for author_one, author_two in pairings:
            writer.writerow(
                {
                    "author_1": author_one.name,
                    "author_1_elo": f"{round(author_one.elo)}",
                    "author_2": author_two.name,
                    "author_2_elo": f"{round(author_two.elo)}",
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate random author pairings")
    parser.add_argument(
        "--count",
        "-n",
        type=int,
        default=10_000,
        help="Number of pairings to generate (default: 10000)",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/elo/author_elo_1.csv"),
        help="Path to the author dataset CSV",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/pairings"),
        help="Directory where pairing files will be written",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional random seed for reproducible results",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rng = random.Random(args.seed)

    authors = load_authors(args.input)
    pairings = generate_pairings(authors, args.count, rng)

    output_path = next_output_path(args.output_dir)
    write_pairings(pairings, output_path)
    print(f"Wrote {len(pairings)} pairings to {output_path}")


if __name__ == "__main__":
    main()
