"""Management command: compute MLAIB-derived Elo priors and seed the database.

Reads data/authors.csv and data/works.csv and writes ``mlaib_record_count`` to
every matched Author/Work. Then, for every item with a count, it sets

    mlaib_elo = 1200 + slope * (ln(count) - mean ln(count))

where the mean is over that content type's items in the database. MLAIB
counts follow a power law, so a linear scale would crowd almost every item
just above the floor and leave the prior uninformative for all but a few
outliers. On the log scale the counts are roughly symmetric, and the judges'
fitted ratings rise roughly linearly in ln(count).

``slope`` is in Elo points per e-fold (×2.72) of MLAIB records; the default,
350 (about 810 per 10×), sits between what the November 2026 matchups
measured: 385 for authors, 329 for works. ``--calibrate`` measures it instead,
by regressing the pooled Bradley–Terry fit on ln(count).

Items with no matchups also get ``elo_rating = mlaib_elo``, so they start from
the prior. Items that have played keep their Elo; ``rebuild_ratings`` replays
every ladder from the new prior.

Usage:
    python manage.py seed_mlaib_elo
    python manage.py seed_mlaib_elo --calibrate --dry-run
"""

from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.constants import DEFAULT_ELO_RATING
from core.models import Author, LLMMatchup, Work

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
ELO_PER_LN_COUNT = 350.0
_MIN_CALIBRATION_ITEMS = 20


class Command(BaseCommand):
    help = "Compute MLAIB Elo priors (log-scaled record counts) and seed Author/Work records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--calibrate",
            action="store_true",
            help="Measure the slope (Elo per e-fold of records) from the Bradley–Terry fit of existing "
            f"matchups, per content type, instead of using {ELO_PER_LN_COUNT:.0f} (kept for a type with too few "
            f"played items).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print counts and the resulting prior without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]

        authors_csv = _DATA_DIR / "authors.csv"
        works_csv = _DATA_DIR / "works.csv"
        for path in (authors_csv, works_csv):
            if not path.exists():
                raise CommandError(f"File not found: {path}")

        csv_id_to_name: dict[int, str] = {}
        with authors_csv.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                csv_id_to_name[int(row["author_id"])] = _author_name(row)

        authors = list(Author.objects.all())
        works = list(Work.objects.select_related("author"))
        stale = False
        for label, ct, items, key, rows in (
            ("Authors", "author", authors, lambda a: a.name, _read_author_rows(authors_csv)),
            ("Works", "work", works, lambda w: (w.title.lower(), w.author.name),
             _read_work_rows(works_csv, csv_id_to_name)),
        ):
            stale |= self._seed(label, ct, items, {key(i): i for i in items}, rows, options["calibrate"], dry_run)

        if dry_run:
            self.stdout.write("(dry run — no changes written)")
        elif stale:
            self.stdout.write("Items that have played keep their Elo; run `python manage.py rebuild_ratings` "
                              "to replay every ladder from the new prior.")

    def _seed(self, label: str, content_type: str, items: list, by_key: dict, rows: list, calibrate: bool,
              dry_run: bool) -> bool:
        """Seed one content type; return whether any item with matchups got a new prior."""
        # A name (or title + author name) can have several CSV rows: homonyms, usually minor
        # namesakes with a handful of records. import_csv_data keeps only the top rows, so the
        # item in the DB is the one with the most records; letting a later namesake win gave
        # e.g. Arthur Miller 1 record instead of 982.
        best: dict = {}
        for key, count in rows:
            best[key] = max(count, best.get(key, count))
        matched = 0
        for key, count in best.items():
            item = by_key.get(key)
            if item is not None:
                item.mlaib_record_count = count
                matched += 1
        counts = {item.pk: item.mlaib_record_count for item in items if item.mlaib_record_count is not None}

        played = _played(content_type)
        slope = ELO_PER_LN_COUNT
        if calibrate:
            slope = _calibrated_slope(content_type, counts, played)
            if slope is None:
                calibrate, slope = False, ELO_PER_LN_COUNT
                self.stderr.write(self.style.WARNING(
                    f"{label}: --calibrate needs at least {_MIN_CALIBRATION_ITEMS} items with both a record count "
                    f"and matchups; using {ELO_PER_LN_COUNT:.0f}."))
        priors = compute_mlaib_elo(list(counts.values()), slope)
        prior_by_pk = dict(zip(counts, priors))

        seeded = stale = 0
        for item in items:
            new = prior_by_pk.get(item.pk)
            if new is not None and item.pk not in played:
                item.elo_rating = new
                seeded += 1
            elif item.pk in played and new != item.mlaib_elo:
                stale += 1
            item.mlaib_elo = new

        self.stdout.write(
            f"{label}: {len(rows)} in CSV, {matched} matched in DB; prior for {len(counts)} of {len(items)} items "
            f"at {slope:.0f} Elo per e-fold ({slope * math.log(10):.0f} per 10x){' (calibrated)' if calibrate else ''}."
        )
        if len(priors) > 1:
            q = statistics.quantiles(priors, n=4)
            self.stdout.write(f"  prior range {min(priors):.0f}–{max(priors):.0f}, middle half {q[0]:.0f}–{q[2]:.0f}; "
                              f"{seeded} unplayed items start there, {stale} played items' ladders started elsewhere.")
        if not dry_run and items:
            model_cls = Author if content_type == "author" else Work
            with transaction.atomic():
                model_cls.objects.bulk_update(items, ["mlaib_record_count", "mlaib_elo", "elo_rating"], batch_size=500)
            self.stdout.write(self.style.SUCCESS(f"  Updated {len(items)} {label.lower()}."))
        return stale > 0


# ── helpers ───────────────────────────────────────────────────────────────────

def _author_name(row: dict) -> str:
    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    if first and last:
        return f"{first} {last}"
    return last or first


def _read_author_rows(csv_path: Path) -> list[tuple[str, int]]:
    """Return list of (name, mlaib_record_count) from authors.csv."""
    rows: list[tuple[str, int]] = []
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = _author_name(row)
            try:
                count = int(row["mlaib_record_count"])
            except (KeyError, ValueError, TypeError):
                continue
            rows.append((name, count))
    return rows


def _read_work_rows(
    csv_path: Path, csv_id_to_name: dict[int, str]
) -> list[tuple[tuple[str, str], int]]:
    """Return list of ((title_lower, author_name), mlaib_record_count) from works.csv."""
    rows: list[tuple[tuple[str, str], int]] = []
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                count = int(row["mlaib_record_count"])
            except (KeyError, ValueError, TypeError):
                continue
            title = row.get("title", "").strip()
            author_id = int(row["author_id"]) if row.get("author_id") else None
            author_name = csv_id_to_name.get(author_id) if author_id else None
            if not title or not author_name:
                continue
            rows.append(((title.lower(), author_name), count))
    return rows


def _log_count(count: int) -> float:
    return math.log(max(count, 1))


def compute_mlaib_elo(counts: list, slope: float = ELO_PER_LN_COUNT) -> list:
    """Map MLAIB record counts (int or None) to Elo priors (float or None).

    ``1200 + slope * (ln(count) - mean ln(count))`` over the non-None counts,
    so the items given average 1200 on the log scale. Counts below 1 count as 1.
    """
    logs = [_log_count(c) for c in counts if c is not None]
    if not logs:
        return [None] * len(counts)
    center = statistics.fmean(logs)
    return [None if c is None else DEFAULT_ELO_RATING + slope * (_log_count(c) - center) for c in counts]


def _played(content_type: str) -> set[int]:
    """Items with at least one published matchup: their aggregate Elo has moved off the prior."""
    pks: set[int] = set()
    for a, b in LLMMatchup.objects.filter(content_type=content_type).values_list("item_a_id", "item_b_id"):
        pks.update((a, b))
    return pks


def _calibrated_slope(content_type: str, counts: dict[int, int], played: set[int]) -> float | None:
    """Elo per e-fold of records: least-squares slope of the pooled Bradley–Terry fit on ln(count).

    Uses the fit without the MLAIB prior, which would pull the answer toward the current prior.
    """
    from core import bt

    pks = [pk for pk in counts if pk in played]
    if len(pks) < _MIN_CALIBRATION_ITEMS:
        return None
    rating = bt.fit_content_type(content_type, prior=False).rating()
    xs = [_log_count(counts[pk]) for pk in pks]
    ys = [rating[pk] for pk in pks]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
