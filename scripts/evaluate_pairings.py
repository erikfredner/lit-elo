#!/usr/bin/env python3
"""Generate OpenAI verdicts for author pairings."""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Literal, Sequence

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel


class Verdict(BaseModel):
    """Structured model output representing a single pairing verdict."""

    verdict: Literal[1, 2]


OUTPUT_COLUMNS = ("llm_verdict",)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        type=Path,
        help="Path to a pairings_N.csv file to update in place.",
    )
    parser.add_argument(
        "--system-prompt",
        type=Path,
        default=Path("prompts/system-prompt.md"),
        help="Path to the system prompt file.",
    )
    parser.add_argument(
        "--model",
        default="gpt-5",
        help="OpenAI model identifier to use.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N rows (useful for testing).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=os.cpu_count() or 1,
        help="Number of parallel worker processes.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum number of retries for a single request.",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=1.0,
        help="Base seconds for exponential backoff between retries.",
    )
    return parser.parse_args(argv)


def load_system_prompt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"Unable to read system prompt: {exc}")


def ensure_api_key() -> None:
    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Missing OPENAI_API_KEY environment variable.")


def build_user_prompt(row: dict[str, str]) -> str:
    author_1 = row.get("author_1", "").strip()
    author_2 = row.get("author_2", "").strip()
    if not author_1 or not author_2:
        raise ValueError("Row is missing author names.")
    return f"1: {author_1}\n2: {author_2}"


_CLIENT: OpenAI | None = None
_SYSTEM_PROMPT: str = ""
_MODEL: str = ""
_MAX_RETRIES: int = 3
_RETRY_BACKOFF: float = 1.0


def init_worker(system_prompt: str, model: str, max_retries: int, retry_backoff: float) -> None:
    ensure_api_key()

    global _CLIENT, _SYSTEM_PROMPT, _MODEL, _MAX_RETRIES, _RETRY_BACKOFF
    _CLIENT = OpenAI()
    _SYSTEM_PROMPT = system_prompt
    _MODEL = model
    _MAX_RETRIES = max(1, max_retries)
    _RETRY_BACKOFF = max(0.0, retry_backoff)


def is_rate_limit_error(exc: Exception) -> bool:
    if exc.__class__.__name__ == "RateLimitError":
        return True
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    message = str(exc).lower()
    return "rate limit" in message or "too many requests" in message


def request_verdict(idx: int, row: dict[str, str]) -> tuple[int, int]:
    if _CLIENT is None:
        raise RuntimeError("OpenAI client is not initialized in worker process.")

    user_prompt = build_user_prompt(row)

    for attempt in range(_MAX_RETRIES):
        try:
            response = _CLIENT.responses.parse(
                model=_MODEL,
                input=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                text_format=Verdict,
            )
        except Exception as exc:  # noqa: BLE001
            if is_rate_limit_error(exc) and attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BACKOFF * (2**attempt) + random.uniform(0, _RETRY_BACKOFF)
                time.sleep(wait)
                continue
            raise

        for item in getattr(response, "output", []) or []:
            content_list = getattr(item, "content", []) or []
            for content in content_list:
                if getattr(content, "type", None) == "refusal":
                    reason = getattr(content, "refusal", "unspecified reason")
                    raise RuntimeError(f"Model refused the request: {reason}")

        if response.status != "completed":
            details = getattr(response, "incomplete_details", None)
            raise RuntimeError(f"OpenAI response incomplete: {details}")

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise RuntimeError("OpenAI response missing parsed verdict.")

        return idx, parsed.verdict

    raise RuntimeError("Exceeded maximum retries for OpenAI request.")


def score_rows(
    rows: list[tuple[int, dict[str, str]]],
    *,
    model: str,
    system_prompt: str,
    concurrency: int,
    max_retries: int,
    retry_backoff: float,
) -> dict[int, int]:
    if not rows:
        return {}

    row_lookup = dict(rows)
    verdict_map: dict[int, int] = {}

    with ProcessPoolExecutor(
        max_workers=max(1, concurrency),
        initializer=init_worker,
        initargs=(system_prompt, model, max_retries, retry_backoff),
    ) as executor:
        futures = {
            executor.submit(request_verdict, idx, row): idx for idx, row in rows
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                row_idx, verdict = future.result()
            except Exception as exc:  # noqa: BLE001
                row = row_lookup[idx]
                raise RuntimeError(
                    f"Failed to score row {idx} ({row.get('author_1')} vs {row.get('author_2')}): {exc}"
                ) from exc

            verdict_map[row_idx] = verdict
            print(f"Row {row_idx}: verdict {verdict}", file=sys.stderr)

    return verdict_map


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if not args.input.exists():
        raise SystemExit(f"Input CSV not found: {args.input}")

    output_path = args.input

    ensure_api_key()
    system_prompt = load_system_prompt(args.system_prompt)

    with args.input.open(newline="", encoding="utf-8") as input_handle:
        reader = csv.DictReader(input_handle)
        if reader.fieldnames is None:
            raise SystemExit("Input CSV is missing headers.")

        fieldnames = list(reader.fieldnames)
        fieldnames += [column for column in OUTPUT_COLUMNS if column not in fieldnames]

        all_rows: list[tuple[int, dict[str, str]]] = []
        rows_to_score: list[tuple[int, dict[str, str]]] = []
        for idx, row in enumerate(reader, start=1):
            all_rows.append((idx, row))
            if args.limit is not None and idx > args.limit:
                continue
            rows_to_score.append((idx, row))

    llm_verdict_map = score_rows(
        rows_to_score,
        model=args.model,
        system_prompt=system_prompt,
        concurrency=max(1, args.concurrency),
        max_retries=max(1, args.max_retries),
        retry_backoff=max(0.0, args.retry_backoff),
    )

    with output_path.open("w", newline="", encoding="utf-8") as output_handle:
        writer = csv.DictWriter(output_handle, fieldnames=fieldnames)
        writer.writeheader()

        for idx, row in all_rows:
            if idx in llm_verdict_map:
                llm_verdict = llm_verdict_map[idx]
                row.update(
                    llm_verdict=str(llm_verdict),
                )
            writer.writerow(row)

    print(f"Updated input file {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
