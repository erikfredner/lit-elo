"""Management command: import authors and works from data/authors.csv and data/works.csv.

Existing records are matched by name (authors) or title+author (works) and skipped,
so ELO ratings and LLMMatchup history for already-loaded items are preserved.

Usage:
    python manage.py import_csv_data
    python manage.py import_csv_data --dry-run
    python manage.py import_csv_data --min-count 5   # lower threshold
    python manage.py import_csv_data --min-count 0   # import everything
"""

from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Author, Work

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
            help="Only import records with mlaib_record_count >= N (default: 20).",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        min_count: int = options["min_count"]

        authors_csv = _DATA_DIR / "authors.csv"
        works_csv = _DATA_DIR / "works.csv"

        for path in (authors_csv, works_csv):
            if not path.exists():
                raise CommandError(f"File not found: {path}")

        # ── Authors ──────────────────────────────────────────────────────────
        # csv_id_to_name: maps CSV author_id → display name (for work resolution).
        # Only authors meeting the min_count threshold are eligible for import,
        # but all author_ids are recorded so works can look up their author name.
        csv_id_to_name: dict[int, str] = {}
        new_authors: list[Author] = []
        skipped_min_count_authors = 0
        existing_names = set(Author.objects.values_list("name", flat=True))

        with authors_csv.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = _author_name(row)
                csv_id_to_name[int(row["author_id"])] = name
                count = _parse_int(row.get("mlaib_record_count")) or 0
                if count < min_count:
                    skipped_min_count_authors += 1
                    continue
                if name in existing_names:
                    continue
                new_authors.append(Author(
                    name=name,
                    birth_year=_parse_int(row.get("birth")),
                    death_year=_parse_int(row.get("death")),
                    mlaib_record_count=_parse_int(row.get("mlaib_record_count")),
                    viaf_id=(row.get("viaf_id") or "").strip(),
                ))

        self.stdout.write(
            f"Authors: {len(csv_id_to_name)} in CSV, "
            f"{len(existing_names)} already in DB, "
            f"{len(new_authors)} to import"
            + (f" ({skipped_min_count_authors} below --min-count {min_count})." if skipped_min_count_authors else ".")
        )

        if not dry_run and new_authors:
            with transaction.atomic():
                created = Author.objects.bulk_create(
                    new_authors, batch_size=500, ignore_conflicts=True
                )
            self.stdout.write(self.style.SUCCESS(f"  Imported {len(created)} authors."))

        # ── Works ─────────────────────────────────────────────────────────────
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
        skipped_min_count_works = 0

        with works_csv.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                count = _parse_int(row.get("mlaib_record_count")) or 0
                if count < min_count:
                    skipped_min_count_works += 1
                    continue

                csv_author_id = _parse_int(row.get("author_id"))
                author_name = csv_id_to_name.get(csv_author_id) if csv_author_id else None
                author = author_lookup.get(author_name) if author_name else None

                if author is None:
                    skipped_no_author += 1
                    continue

                title = row["title"].strip()
                if (title.lower(), author.pk) in existing_works:
                    continue

                new_works.append(Work(
                    title=title,
                    author=author,
                    publication_year=_parse_int(row.get("year")),
                    mlaib_record_count=_parse_int(row.get("mlaib_record_count")),
                ))

        total_csv_works = sum(1 for _ in works_csv.open(encoding="utf-8")) - 1  # subtract header
        skipped_notes = []
        if skipped_min_count_works:
            skipped_notes.append(f"{skipped_min_count_works} below --min-count {min_count}")
        if skipped_no_author:
            skipped_notes.append(f"{skipped_no_author} author not found")
        self.stdout.write(
            f"Works: {total_csv_works} in CSV, "
            f"{len(existing_works)} already in DB, "
            f"{len(new_works)} to import"
            + (f" ({'; '.join(skipped_notes)} skipped)." if skipped_notes else ".")
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
