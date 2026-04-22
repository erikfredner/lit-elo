"""Management command: import authors and works from data/authors.csv and data/works.csv.

Existing records are matched by name (authors) or title+author (works) and skipped,
so ELO ratings and LLMMatchup history for already-loaded items are preserved.

Usage:
    python manage.py import_csv_data
    python manage.py import_csv_data --dry-run
    python manage.py import_csv_data --min-count 5   # lower threshold
    python manage.py import_csv_data --min-count 0   # import everything

--min-count N  Include authors with mlaib_record_count >= N, plus authors whose
               work has mlaib_record_count >= N. All works by any included author
               are imported regardless of per-work count.
"""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Author, Work

_MLAIB_ELO_MIN = 0.0
_MLAIB_ELO_MAX = 3000.0

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"


class Command(BaseCommand):
    help = "Import authors and works from data/authors.csv and data/works.csv."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be imported without writing to the database.",
        )
        parser.add_argument(
            "--min-count",
            type=int,
            default=20,
            metavar="N",
            help=(
                "Include authors with mlaib_record_count >= N, plus authors whose work "
                "has mlaib_record_count >= N. All works by any included author are "
                "imported regardless of per-work count (default: 20)."
            ),
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        min_count: int = options["min_count"]

        authors_csv = _DATA_DIR / "authors.csv"
        works_csv = _DATA_DIR / "works.csv"

        for path in (authors_csv, works_csv):
            if not path.exists():
                raise CommandError(f"File not found: {path}")

        # ── Pass 1: load CSVs into memory ────────────────────────────────────
        all_author_rows: dict[int, dict] = {}  # csv_author_id → row
        csv_id_to_name: dict[int, str] = {}    # csv_author_id → display name

        with authors_csv.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                author_id = int(row["author_id"])
                csv_id_to_name[author_id] = _author_name(row)
                all_author_rows[author_id] = row

        all_work_rows: dict[int, list[dict]] = defaultdict(list)  # csv_author_id → [rows]
        total_csv_works = 0

        with works_csv.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                total_csv_works += 1
                author_id = _parse_int(row.get("author_id"))
                if author_id is not None:
                    all_work_rows[author_id].append(row)

        # ── Pass 2: determine which authors to include ────────────────────────
        # An author is included if:
        #   (a) their own mlaib_record_count >= min_count, OR
        #   (b) at least one of their works has mlaib_record_count >= min_count
        author_passes_threshold: set[int] = set()
        for author_id, row in all_author_rows.items():
            count = _parse_int(row.get("mlaib_record_count")) or 0
            if count >= min_count:
                author_passes_threshold.add(author_id)

        author_lifted_by_work: set[int] = set()
        for author_id, work_rows in all_work_rows.items():
            if author_id in author_passes_threshold:
                continue
            for work_row in work_rows:
                count = _parse_int(work_row.get("mlaib_record_count")) or 0
                if count >= min_count:
                    author_lifted_by_work.add(author_id)
                    break

        included_author_ids = author_passes_threshold | author_lifted_by_work
        excluded_count = len(all_author_rows) - len(included_author_ids)

        # ── Authors: build and import ─────────────────────────────────────────
        existing_names = set(Author.objects.values_list("name", flat=True))
        new_authors: list[Author] = []

        for author_id in included_author_ids:
            row = all_author_rows[author_id]
            name = csv_id_to_name[author_id]
            if name in existing_names:
                continue
            new_authors.append(Author(
                name=name,
                birth_year=_parse_int(row.get("birth")),
                death_year=_parse_int(row.get("death")),
                mlaib_record_count=_parse_int(row.get("mlaib_record_count")),
                viaf_id=(row.get("viaf_id") or "").strip(),
            ))

        if new_authors:
            raw_counts = [a.mlaib_record_count for a in new_authors]
            elo_values = compute_mlaib_elo(raw_counts)
            for author, elo in zip(new_authors, elo_values):
                author.mlaib_elo = elo

        detail_parts = [f"{len(author_passes_threshold)} meet author threshold"]
        if author_lifted_by_work:
            detail_parts.append(f"{len(author_lifted_by_work)} added via high-count work")
        if excluded_count:
            detail_parts.append(f"{excluded_count} excluded")
        self.stdout.write(
            f"Authors: {len(all_author_rows)} in CSV, "
            f"{len(existing_names)} already in DB, "
            f"{len(new_authors)} to import "
            f"({'; '.join(detail_parts)})."
        )

        if not dry_run and new_authors:
            with transaction.atomic():
                created = Author.objects.bulk_create(
                    new_authors, batch_size=500, ignore_conflicts=True
                )
            self.stdout.write(self.style.SUCCESS(f"  Imported {len(created)} authors."))

        # ── Works: import ALL works by included authors ───────────────────────
        # Reload author lookup after import so new authors are included.
        author_lookup: dict[str, Author] = {
            a.name: a for a in Author.objects.all()
        }

        existing_works: set[tuple[str, int]] = {
            (title.lower(), author_id)
            for title, author_id in Work.objects.values_list("title", "author_id")
        }

        new_works: list[Work] = []
        skipped_no_author = 0

        for author_id, work_rows in all_work_rows.items():
            author_name = csv_id_to_name.get(author_id)
            author = author_lookup.get(author_name) if author_name else None

            if author is None:
                skipped_no_author += len(work_rows)
                continue

            for row in work_rows:
                title = row["title"].strip()
                if (title.lower(), author.pk) in existing_works:
                    continue
                new_works.append(Work(
                    title=title,
                    author=author,
                    publication_year=_parse_int(row.get("year")),
                    mlaib_record_count=_parse_int(row.get("mlaib_record_count")),
                ))

        self.stdout.write(
            f"Works: {total_csv_works} in CSV, "
            f"{len(existing_works)} already in DB, "
            f"{len(new_works)} to import"
            + (f" ({skipped_no_author} skipped — author not in DB)." if skipped_no_author else ".")
        )

        if not dry_run and new_works:
            with transaction.atomic():
                created = Work.objects.bulk_create(
                    new_works, batch_size=500, ignore_conflicts=True
                )
            self.stdout.write(self.style.SUCCESS(f"  Imported {len(created)} works."))

        if dry_run:
            self.stdout.write("(dry run — no changes written)")


# ── helpers ───────────────────────────────────────────────────────────────────

def _author_name(row: dict) -> str:
    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    if first and last:
        return f"{first} {last}"
    return last or first


def _parse_int(value: str | None) -> int | None:
    """Parse an integer, returning None for missing, non-numeric, or negative values.
    Negative values occur for B.C. dates, which PositiveSmallIntegerField cannot store."""
    try:
        n = int(value)
        return n if n > 0 else None
    except (ValueError, TypeError):
        return None


def compute_mlaib_elo(counts: list[int | None]) -> list[float | None]:
    """Z-score scale a list of MLAIB counts to the range [_MLAIB_ELO_MIN, _MLAIB_ELO_MAX].

    Returns a parallel list of float | None. Positions with None counts get None.
    If fewer than 2 valid counts exist, all positions return None (stdev undefined).
    If all valid counts are equal (stdev == 0), all valid positions return the midpoint.
    """
    valid = [(i, c) for i, c in enumerate(counts) if c is not None]
    result: list[float | None] = [None] * len(counts)
    if len(valid) < 2:
        return result
    valid_counts = [c for _, c in valid]
    mean = statistics.mean(valid_counts)
    stdev = statistics.stdev(valid_counts)
    if stdev == 0:
        midpoint = (_MLAIB_ELO_MIN + _MLAIB_ELO_MAX) / 2
        for i, _ in valid:
            result[i] = midpoint
        return result
    zscores = [(i, (c - mean) / stdev) for i, c in valid]
    z_values = [z for _, z in zscores]
    min_z, max_z = min(z_values), max(z_values)
    scale = (_MLAIB_ELO_MAX - _MLAIB_ELO_MIN) / (max_z - min_z)
    for i, z in zscores:
        result[i] = _MLAIB_ELO_MIN + (z - min_z) * scale
    return result
