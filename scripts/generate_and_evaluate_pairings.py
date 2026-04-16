#!/usr/bin/env python3
"""Generate new author pairings, evaluate them, and update Elo ratings."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence


AUTHOR_ELO_PATTERN = re.compile(r"author_elo_(\d+)\.csv$")
PAIRINGS_PATTERN = re.compile(r"pairings_(\d+)\.csv$")


def find_latest_author_elo(elo_dir: Path) -> Path:
    latest_index = -1
    latest_path: Path | None = None

    if not elo_dir.exists():
        raise FileNotFoundError(f"ELO directory not found: {elo_dir}")

    for path in elo_dir.iterdir():
        match = AUTHOR_ELO_PATTERN.match(path.name)
        if not match:
            continue
        index = int(match.group(1))
        if index > latest_index:
            latest_index = index
            latest_path = path

    if latest_path is None:
        raise FileNotFoundError(f"No author_elo_N.csv files found in {elo_dir}")

    return latest_path


def next_pairings_path(output_dir: Path) -> Path:
    max_existing = 0

    if output_dir.exists():
        for path in output_dir.iterdir():
            match = PAIRINGS_PATTERN.match(path.name)
            if match:
                max_existing = max(max_existing, int(match.group(1)))

    next_index = max_existing + 1
    return output_dir / f"pairings_{next_index}.csv"


def parse_pairings_index(pairings_path: Path) -> int:
    match = PAIRINGS_PATTERN.match(pairings_path.name)
    if not match:
        raise ValueError(
            f"Unable to determine round number from pairings filename: {pairings_path.name}"
        )
    return int(match.group(1))


def run_generate_pairings(
    *,
    input_path: Path,
    output_dir: Path,
    pairings_count: int,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    expected_output = next_pairings_path(output_dir)

    script_path = Path(__file__).resolve().parent / "generate_pairings.py"
    cmd = [
        sys.executable,
        str(script_path),
        "--count",
        str(pairings_count),
        "--input",
        str(input_path),
        "--output-dir",
        str(output_dir),
    ]
    subprocess.run(cmd, check=True)

    if not expected_output.exists():
        raise RuntimeError(
            "Pairings generation completed but expected output file is missing: "
            f"{expected_output}"
        )

    return expected_output


def run_evaluate_pairings(pairings_path: Path, extra_args: Sequence[str]) -> None:
    script_path = Path(__file__).resolve().parent / "evaluate_pairings.py"
    cmd = [sys.executable, str(script_path), str(pairings_path)]

    if extra_args:
        cmd.extend(extra_args)

    subprocess.run(cmd, check=True)


def run_update_author_elo(pairings_path: Path, *, elo_dir: Path) -> Path:
    round_index = parse_pairings_index(pairings_path)
    if round_index <= 0:
        raise ValueError(
            "Pairings filename must include an index greater than zero to update Elo"
        )

    expected_output = elo_dir / f"author_elo_{round_index}.csv"
    script_path = Path(__file__).resolve().parent / "update_author_elo.py"
    cmd = [
        sys.executable,
        str(script_path),
        "--pairings",
        str(pairings_path),
        "--elo-dir",
        str(elo_dir),
    ]
    subprocess.run(cmd, check=True)

    if not expected_output.exists():
        raise RuntimeError(
            "Author Elo update completed but expected output file is missing: "
            f"{expected_output}"
        )

    return expected_output


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-n",
        "--pairings-count",
        type=int,
        default=50,
        help="Number of new pairings to generate (default: 50)",
    )
    parser.add_argument(
        "--elo-dir",
        type=Path,
        default=Path("data/elo"),
        help="Directory containing author_elo_N.csv files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/pairings"),
        help="Directory where pairing CSV files should be written",
    )
    parser.add_argument(
        "extra_eval_args",
        nargs=argparse.REMAINDER,
        help="Additional arguments passed to evaluate_pairings.py after '--'",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    extra_args = args.extra_eval_args or []
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]

    author_elo_path = find_latest_author_elo(args.elo_dir)
    print(f"Using author ELO file: {author_elo_path}")

    pairings_path = run_generate_pairings(
        input_path=author_elo_path,
        output_dir=args.output_dir,
        pairings_count=args.pairings_count,
    )
    print(f"Generated pairings file: {pairings_path}")

    run_evaluate_pairings(pairings_path, extra_args)
    print("Evaluation completed")

    updated_elo_path = run_update_author_elo(pairings_path, elo_dir=args.elo_dir)
    print(f"Updated Elo file: {updated_elo_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
