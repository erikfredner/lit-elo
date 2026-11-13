"""Management command: analyze ELO convergence across historical run_llm_elo batches.

Groups LLMMatchup records into batches (by time gap or fixed size) and reports
the upset rate, significant upset rate, and mean |ΔELO| per batch. Both upset
metrics should converge toward the self-inconsistency noise floor as ELO ratings
stabilize. When they stop declining and hover near the noise floor, further runs
are no longer informative.

Significant upsets (Sig. Upsets) are upsets where the ELO gap between the two
items is >= sigma (default: population SD of all elo_before values for the mode).
This separates genuinely surprising reversals (1000 vs 1600) from near-coin-flip
results (1050 vs 1060).

Note on pairing bias: the run_llm_elo pairing algorithm uses exp(-Δ/200) to
strongly prefer close-ELO matches. Close matches have expected win probabilities
near 50% for both sides, which biases the raw upset rate toward ~50% regardless
of true convergence. Sig. Upsets is a more informative convergence signal because
it counts only upsets where the ELO gap is large enough to be meaningful.

Usage:
    python manage.py analyze_convergence
    python manage.py analyze_convergence --mode authors --batch-size 50
    python manage.py analyze_convergence --inconsistency-rate 0.13 --output-csv /tmp/out.csv
    python manage.py analyze_convergence --sigma 200
"""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from django.core.management.base import BaseCommand, CommandError

from core.models import LLMMatchup

_DEFAULT_TIME_GAP = 300      # seconds between runs to split batches
_DEFAULT_INCONSISTENCY = 0.13
_STABLE_THRESHOLD = 0.03     # within 3pp of noise floor = "stable near noise floor"
_DECLINING_THRESHOLD = 0.02  # dropped >2pp from previous batch = "declining"


# ── data structures ───────────────────────────────────────────────────────────

@dataclass
class _Row:
    """One LLMMatchup record, only the fields we need."""
    elo_a_before: float
    elo_b_before: float
    elo_a_after: float
    winner: str          # 'A' or 'B'
    created_at: datetime


@dataclass
class _BatchStats:
    batch_num: int
    date_str: str
    n: int
    n_decidable: int
    n_upsets: int
    n_sig_upsets: int      # upsets where elo_diff >= sigma
    mean_abs_delta: float

    @property
    def upset_rate(self) -> Optional[float]:
        return self.n_upsets / self.n_decidable if self.n_decidable else None

    @property
    def sig_upset_rate(self) -> Optional[float]:
        return self.n_sig_upsets / self.n_decidable if self.n_decidable else None


# ── loading ───────────────────────────────────────────────────────────────────

def _load_rows(mode: str, model_filter: Optional[str]) -> list[_Row]:
    content_type = "author" if mode == "authors" else "work"
    qs = LLMMatchup.objects.filter(content_type=content_type).order_by("created_at")
    if model_filter:
        qs = qs.filter(model_used=model_filter)
    return [
        _Row(
            elo_a_before=m.elo_a_before,
            elo_b_before=m.elo_b_before,
            elo_a_after=m.elo_a_after,
            winner=m.winner,
            created_at=m.created_at,
        )
        for m in qs.values_list(
            "elo_a_before", "elo_b_before", "elo_a_after", "winner", "created_at",
            named=True,
        )
    ]


# ── sigma computation ─────────────────────────────────────────────────────────

def _compute_sigma(rows: list[_Row]) -> float:
    """Population SD of ELO ratings across all matchup participants."""
    vals = [r.elo_a_before for r in rows] + [r.elo_b_before for r in rows]
    return statistics.pstdev(vals) if len(vals) >= 2 else 0.0


# ── batching ──────────────────────────────────────────────────────────────────

def _split_by_time_gap(rows: list[_Row], time_gap: int) -> list[list[_Row]]:
    if not rows:
        return []
    batches: list[list[_Row]] = [[rows[0]]]
    for prev, cur in zip(rows, rows[1:]):
        gap = (cur.created_at - prev.created_at).total_seconds()
        if gap >= time_gap:
            batches.append([])
        batches[-1].append(cur)
    return batches


def _split_by_size(rows: list[_Row], size: int) -> list[list[_Row]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


# ── statistics ────────────────────────────────────────────────────────────────

def _batch_stats(batch_num: int, rows: list[_Row], sigma: float) -> _BatchStats:
    date_str = rows[0].created_at.strftime("%Y-%m-%d") if rows else "—"
    n_decidable = 0
    n_upsets = 0
    n_sig_upsets = 0
    deltas: list[float] = []

    for r in rows:
        delta = abs(r.elo_a_after - r.elo_a_before)
        deltas.append(delta)

        if r.elo_a_before == r.elo_b_before:
            continue  # tied ELO: exclude from upset count

        n_decidable += 1
        elo_diff = abs(r.elo_a_before - r.elo_b_before)
        expected_winner = "A" if r.elo_a_before > r.elo_b_before else "B"
        if r.winner != expected_winner:
            n_upsets += 1
            if sigma > 0 and elo_diff >= sigma:
                n_sig_upsets += 1

    mean_abs_delta = statistics.mean(deltas) if deltas else 0.0

    return _BatchStats(
        batch_num=batch_num,
        date_str=date_str,
        n=len(rows),
        n_decidable=n_decidable,
        n_upsets=n_upsets,
        n_sig_upsets=n_sig_upsets,
        mean_abs_delta=mean_abs_delta,
    )


# ── trend assessment ──────────────────────────────────────────────────────────

def _trend_label(stats: list[_BatchStats], inconsistency_rate: float) -> str:
    """Return a human-readable trend assessment based on the last two batches."""
    rates = [s.upset_rate for s in stats if s.upset_rate is not None]
    if not rates:
        return "insufficient data"
    current = rates[-1]
    floor = inconsistency_rate
    within_floor = abs(current - floor) <= _STABLE_THRESHOLD

    if within_floor:
        return (
            f"stable near noise floor (current {current:.1%} vs floor {floor:.1%}) "
            f"— further runs are mostly noise"
        )

    if len(rates) >= 2:
        prev = rates[-2]
        dropped = prev - current
        if dropped >= _DECLINING_THRESHOLD:
            gap = current - floor
            return (
                f"declining (current {current:.1%} vs floor {floor:.1%}, "
                f"gap {gap:.1%}) — further runs still informative"
            )
        else:
            return (
                f"plateau above noise floor (current {current:.1%} vs floor {floor:.1%}) "
                f"— more runs may help or ratings may be near true values"
            )

    return f"single batch — rerun after more matchups to assess trend"


# ── reporting ─────────────────────────────────────────────────────────────────

def _report_mode(
    stats: list[_BatchStats],
    mode_label: str,
    inconsistency_rate: float,
    sigma: float,
    output_csv: Optional[Path],
    stdout,
) -> None:
    total_matchups = sum(s.n for s in stats)

    stdout.write(f"\n{'='*80}")
    stdout.write(f"  ELO Convergence Analysis — {mode_label}")
    stdout.write(f"{'='*80}")
    stdout.write(
        f"Batches detected: {len(stats)}  |  Total matchups: {total_matchups:,}"
    )
    stdout.write(
        f"Noise floor ({inconsistency_rate:.1%} inconsistency rate): upset rate → ~{inconsistency_rate:.1%}"
    )
    stdout.write(
        f"Sigma (sig. upset threshold): {sigma:.1f} ELO pts  "
        f"(upsets where ELO gap ≥ {sigma:.0f})"
    )

    # Table header
    stdout.write(
        f"\n{'Batch':>5}  {'Date':<12}  {'Matches':>7}  "
        f"{'Upset%':>7}  {'Sig.Upset%':>10}  {'Mean |ΔELO|':>11}  {'Cumul.Upset%':>12}"
    )
    stdout.write("─" * 80)

    cumul_d = 0
    cumul_u = 0

    csv_rows = []
    for s in stats:
        cumul_d += s.n_decidable
        cumul_u += s.n_upsets
        cumul_rate = f"{cumul_u/cumul_d:.1%}" if cumul_d else "—"
        upset_str = f"{s.upset_rate:.1%}" if s.upset_rate is not None else "—"
        sig_str = f"{s.sig_upset_rate:.1%}" if s.sig_upset_rate is not None else "—"
        stdout.write(
            f"{s.batch_num:>5}  {s.date_str:<12}  {s.n:>7,}  "
            f"{upset_str:>7}  {sig_str:>10}  {s.mean_abs_delta:>11.2f}  {cumul_rate:>12}"
        )
        csv_rows.append({
            "batch": s.batch_num,
            "date": s.date_str,
            "matches": s.n,
            "decidable": s.n_decidable,
            "upsets": s.n_upsets,
            "upset_rate": f"{s.upset_rate:.4f}" if s.upset_rate is not None else "",
            "sig_upsets": s.n_sig_upsets,
            "sig_upset_rate": f"{s.sig_upset_rate:.4f}" if s.sig_upset_rate is not None else "",
            "mean_abs_delta_elo": f"{s.mean_abs_delta:.4f}",
            "cumul_upset_rate": f"{cumul_u/cumul_d:.4f}" if cumul_d else "",
        })

    stdout.write("─" * 80)
    stdout.write(
        f"{'':>5}  {'Noise floor':<12}  {'':>7}  "
        f"{'~'+f'{inconsistency_rate:.1%}':>7}  {'':>10}  {'':>11}  {'':>12}"
    )

    trend = _trend_label(stats, inconsistency_rate)
    stdout.write(f"\nTrend: {trend}")
    stdout.write(
        f"\nNote: Sig.Upset% = upsets where ELO gap ≥ {sigma:.0f} pts (1σ of ELO distribution).\n"
        f"      Raw Upset% is biased toward ~50% because the pairing algorithm favors\n"
        f"      close-ELO matches (exp(-Δ/200)); Sig.Upset% is a better convergence signal."
    )

    if output_csv and csv_rows:
        with open(output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
        stdout.write(f"CSV written to: {output_csv}")


# ── command ───────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = (
        "Analyze ELO convergence across historical run_llm_elo batches. "
        "Reports upset rate, significant upset rate, and mean |ΔELO| per batch; "
        "all should converge toward the self-inconsistency noise floor as ratings stabilize."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode",
            choices=["authors", "works", "both"],
            default="both",
            help="Which entity type to analyze (default: both).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help=(
                "Group every N matchups into one batch (chronological order). "
                "Default: auto-detect batches by time gap between runs."
            ),
        )
        parser.add_argument(
            "--time-gap",
            type=int,
            default=_DEFAULT_TIME_GAP,
            help=(
                f"Seconds of inactivity between matchups used to split batches "
                f"in auto-detect mode (default: {_DEFAULT_TIME_GAP})."
            ),
        )
        parser.add_argument(
            "--inconsistency-rate",
            type=float,
            default=_DEFAULT_INCONSISTENCY,
            help=(
                f"Known self-inconsistency rate from test_self_consistency "
                f"(default: {_DEFAULT_INCONSISTENCY}). Used to annotate the noise floor."
            ),
        )
        parser.add_argument(
            "--sigma",
            type=float,
            default=None,
            help=(
                "ELO difference threshold for 'significant upset' (default: "
                "population SD of all elo_before values for the mode). "
                "An upset is 'significant' when the lower-rated item wins despite "
                "an ELO gap >= sigma."
            ),
        )
        parser.add_argument(
            "--model",
            default=None,
            help="Filter to a specific model name (e.g. gpt-5.4-nano). Default: all models.",
        )
        parser.add_argument(
            "--output-csv",
            type=Path,
            default=None,
            help="Write per-batch stats to a CSV file (mode suffix appended if --mode both).",
        )

    def handle(self, *args, **options):
        mode: str = options["mode"]
        batch_size: Optional[int] = options["batch_size"]
        time_gap: int = options["time_gap"]
        inconsistency_rate: float = options["inconsistency_rate"]
        sigma_override: Optional[float] = options["sigma"]
        model_filter: Optional[str] = options["model"]
        output_csv: Optional[Path] = options["output_csv"]

        if not 0.0 < inconsistency_rate < 1.0:
            raise CommandError("--inconsistency-rate must be between 0 and 1.")
        if sigma_override is not None and sigma_override <= 0:
            raise CommandError("--sigma must be a positive number.")

        modes = ["authors", "works"] if mode == "both" else [mode]

        self.stdout.write(
            f"\nELO Convergence Analysis"
            + (f"  [model: {model_filter}]" if model_filter else "")
            + (f"  [batch-size: {batch_size}]" if batch_size else f"  [auto time-gap: {time_gap}s]")
        )

        for m in modes:
            rows = _load_rows(m, model_filter)
            if not rows:
                self.stdout.write(f"\nNo {m} matchups found in DB.")
                continue

            sigma = sigma_override if sigma_override is not None else _compute_sigma(rows)

            if batch_size:
                batches = _split_by_size(rows, batch_size)
            else:
                batches = _split_by_time_gap(rows, time_gap)

            if len(batches) < 2:
                self.stdout.write(
                    f"\nWarning: only {len(batches)} batch(es) of {m} matchups detected. "
                    f"Run more `run_llm_elo` sessions or lower --time-gap to see convergence."
                )

            all_stats = [_batch_stats(i + 1, b, sigma) for i, b in enumerate(batches)]

            csv_path = None
            if output_csv:
                csv_path = (
                    output_csv.with_stem(f"{output_csv.stem}_{m}")
                    if mode == "both"
                    else output_csv
                )

            _report_mode(
                stats=all_stats,
                mode_label=m.upper(),
                inconsistency_rate=inconsistency_rate,
                sigma=sigma,
                output_csv=csv_path,
                stdout=self.stdout,
            )
