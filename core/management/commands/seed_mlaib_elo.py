"""Management command: compute MLAIB-derived ELO estimates and seed the database.

Reads data/authors.csv and data/works.csv, z-scores the mlaib_record_count
column, min-max scales to [800, 1600], and writes mlaib_record_count + mlaib_elo
back to every matched Author/Work. For items still at the default ELO (1200.0)
it also sets elo_rating = mlaib_elo so unrated items start from an informed prior.

Usage:
    python manage.py seed_mlaib_elo
    python manage.py seed_mlaib_elo --dry-run
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Author, Work
from core.constants import DEFAULT_ELO_RATING

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
_ELO_MIN = 800.0
_ELO_MAX = 1600.0


class Command(BaseCommand):
    help = "Compute MLAIB ELO estimates and seed Author/Work records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print counts without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]

        authors_csv = _DATA_DIR / "authors.csv"
        works_csv = _DATA_DIR / "works.csv"
        for path in (authors_csv, works_csv):
            if not path.exists():
                raise CommandError(f"File not found: {path}")

        self._seed_authors(authors_csv, dry_run)
        self._seed_works(works_csv, authors_csv, dry_run)

        if dry_run:
            self.stdout.write("(dry run — no changes written)")

    # ── authors ───────────────────────────────────────────────────────────────

    def _seed_authors(self, csv_path: Path, dry_run: bool) -> None:
        rows = _read_author_rows(csv_path)
        elo_map = _compute_elo_map(rows)  # name → (count, elo)

        db_authors = {a.name: a for a in Author.objects.all()}
        to_update: list[Author] = []
        elo_seeded = 0

        for name, (count, mlaib_elo) in elo_map.items():
            author = db_authors.get(name)
            if author is None:
                continue
            author.mlaib_record_count = count
            author.mlaib_elo = mlaib_elo
            if author.elo_rating == DEFAULT_ELO_RATING:
                author.elo_rating = mlaib_elo
                elo_seeded += 1
            to_update.append(author)

        self.stdout.write(
            f"Authors: {len(elo_map)} in CSV, {len(to_update)} matched in DB, "
            f"{elo_seeded} ELO ratings seeded from MLAIB."
        )
        if not dry_run and to_update:
            with transaction.atomic():
                Author.objects.bulk_update(
                    to_update, ["mlaib_record_count", "mlaib_elo", "elo_rating"]
                )
            self.stdout.write(self.style.SUCCESS(f"  Updated {len(to_update)} authors."))

    # ── works ─────────────────────────────────────────────────────────────────

    def _seed_works(self, works_csv: Path, authors_csv: Path, dry_run: bool) -> None:
        # Build author_id → name map from the authors CSV
        csv_id_to_name: dict[int, str] = {}
        with authors_csv.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = _author_name(row)
                csv_id_to_name[int(row["author_id"])] = name

        rows = _read_work_rows(works_csv, csv_id_to_name)
        elo_map = _compute_elo_map(rows)  # (title_lower, author_name) → (count, elo)

        # Build DB lookup: (title_lower, author_name) → Work
        db_works = {
            (w.title.lower(), w.author.name): w
            for w in Work.objects.select_related("author").all()
        }
        to_update: list[Work] = []
        elo_seeded = 0

        for key, (count, mlaib_elo) in elo_map.items():
            work = db_works.get(key)
            if work is None:
                continue
            work.mlaib_record_count = count
            work.mlaib_elo = mlaib_elo
            if work.elo_rating == DEFAULT_ELO_RATING:
                work.elo_rating = mlaib_elo
                elo_seeded += 1
            to_update.append(work)

        self.stdout.write(
            f"Works: {len(elo_map)} in CSV, {len(to_update)} matched in DB, "
            f"{elo_seeded} ELO ratings seeded from MLAIB."
        )
        if not dry_run and to_update:
            with transaction.atomic():
                Work.objects.bulk_update(
                    to_update, ["mlaib_record_count", "mlaib_elo", "elo_rating"]
                )
            self.stdout.write(self.style.SUCCESS(f"  Updated {len(to_update)} works."))


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


def _compute_elo_map(rows: list) -> dict:
    """
    Given a list of (key, count), compute z-scores and min-max scale to
    [_ELO_MIN, _ELO_MAX]. Returns {key: (count, elo)}.
    """
    if not rows:
        return {}
    counts = [count for _, count in rows]
    if len(counts) < 2:
        return {key: (count, (_ELO_MIN + _ELO_MAX) / 2) for key, count in rows}

    mean = statistics.mean(counts)
    stdev = statistics.stdev(counts)
    if stdev == 0:
        mid = (_ELO_MIN + _ELO_MAX) / 2
        return {key: (count, mid) for key, count in rows}

    zscores = [(key, count, (count - mean) / stdev) for key, count in rows]
    min_z = min(z for _, _, z in zscores)
    max_z = max(z for _, _, z in zscores)
    scale = (_ELO_MAX - _ELO_MIN) / (max_z - min_z) if max_z != min_z else 1.0

    return {
        key: (count, _ELO_MIN + (z - min_z) * scale)
        for key, count, z in zscores
    }
