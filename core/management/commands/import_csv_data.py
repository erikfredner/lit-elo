"""Management command: import authors and works from data/authors.csv and data/works.csv.

Existing records are matched by name (authors) or title+author (works) and skipped,
so ELO ratings and LLMMatchup history for already-loaded items are preserved.

Usage:
    python manage.py import_csv_data
    python manage.py import_csv_data --dry-run
    python manage.py import_csv_data --top-pct 10   # top 10% instead of 5%
    python manage.py import_csv_data --top-pct 100  # import everything
    python manage.py import_csv_data --works data/validated_works.csv

--top-pct N  Import the top N% of authors and works by mlaib_record_count
             (default: 5). The threshold is the minimum count of the top-N%
             cohort; all records at or above that count are included, so ties
             at the boundary are never broken by ordering.
--works FILE Use a custom works CSV instead of data/works.csv. Only authors
             referenced in that file are eligible for import; the --top-pct
             threshold is computed from that author subset. A 'genres' column
             (semicolon-separated) populates Work.form if present.
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Author, Work

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"

_DEFAULT_TOP_PCT = 5


class Command(BaseCommand):
    help = "Import authors and works from data/authors.csv and data/works.csv."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be imported without writing to the database.",
        )
        parser.add_argument(
            "--top-pct",
            type=int,
            default=_DEFAULT_TOP_PCT,
            metavar="N",
            help=(
                f"Import the top N%% of authors and works by mlaib_record_count "
                f"(default: {_DEFAULT_TOP_PCT}). All records tied at the boundary "
                f"are included."
            ),
        )
        parser.add_argument(
            "--works",
            type=Path,
            default=None,
            metavar="FILE",
            help=(
                "Custom works CSV to import instead of data/works.csv. Only authors "
                "referenced in this file are imported; the --top-pct threshold is "
                "applied within that set. A 'genres' column (semicolon-separated) "
                "populates Work.form if present."
            ),
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        top_pct: float = options["top_pct"] / 100.0
        custom_works: bool = options["works"] is not None

        authors_csv = _DATA_DIR / "authors.csv"
        works_csv = options["works"] if custom_works else _DATA_DIR / "works.csv"

        for path in (authors_csv, works_csv):
            if not path.exists():
                raise CommandError(f"File not found: {path}")

        if custom_works:
            self.stdout.write(f"Using custom works file: {works_csv}")

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

        # ── Pass 2: compute thresholds and determine which records to include ─
        # When --works is given, restrict the author candidate pool to authors
        # referenced in the works file; threshold is computed within that pool.
        if custom_works:
            eligible_author_ids = set(all_work_rows.keys()) & set(all_author_rows.keys())
        else:
            eligible_author_ids = set(all_author_rows.keys())

        author_counts = [
            c for aid in eligible_author_ids
            if (c := _parse_int(all_author_rows[aid].get("mlaib_record_count"))) is not None
        ]
        if not author_counts:
            raise CommandError("No authors with a valid mlaib_record_count found.")
        author_threshold = _quantile_threshold(author_counts, top_pct)

        work_counts = [
            c for rows in all_work_rows.values()
            for row in rows
            if (c := _parse_int(row.get("mlaib_record_count"))) is not None
        ]
        if not work_counts:
            raise CommandError("No works with a valid mlaib_record_count found.")
        work_threshold = _quantile_threshold(work_counts, top_pct)

        included_author_ids = {
            aid for aid in eligible_author_ids
            if (_parse_int(all_author_rows[aid].get("mlaib_record_count")) or 0)
            >= author_threshold
        }
        excluded_count = len(eligible_author_ids) - len(included_author_ids)

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

        self.stdout.write(
            f"Authors: {len(all_author_rows)} in CSV, "
            f"top-{options['top_pct']}% threshold = {author_threshold} records, "
            f"{len(existing_names)} already in DB, "
            f"{len(new_authors)} to import "
            f"({excluded_count} excluded)."
        )

        if not dry_run and new_authors:
            with transaction.atomic():
                created = Author.objects.bulk_create(
                    new_authors, batch_size=500, ignore_conflicts=True
                )
            self.stdout.write(self.style.SUCCESS(f"  Imported {len(created)} authors."))

        # ── Works: import qualifying works by included authors ────────────────
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
        skipped_low_count = 0

        for author_id, work_rows in all_work_rows.items():
            author_name = csv_id_to_name.get(author_id)
            author = author_lookup.get(author_name) if author_name else None

            if author is None:
                skipped_no_author += len(work_rows)
                continue

            for row in work_rows:
                work_count = _parse_int(row.get("mlaib_record_count")) or 0
                if work_count < work_threshold:
                    skipped_low_count += 1
                    continue
                title = row["title"].strip()
                if (title.lower(), author.pk) in existing_works:
                    continue
                genres_str = (row.get("genres") or "").strip()
                form = genres_str.split(";")[0].strip()[:64] if genres_str else ""
                new_works.append(Work(
                    title=title,
                    author=author,
                    publication_year=_parse_int(row.get("year")),
                    mlaib_record_count=_parse_int(row.get("mlaib_record_count")),
                    form=form,
                ))

        skip_parts = []
        if skipped_low_count:
            skip_parts.append(f"{skipped_low_count} skipped — below threshold")
        if skipped_no_author:
            skip_parts.append(f"{skipped_no_author} skipped — author not in DB")
        self.stdout.write(
            f"Works: {total_csv_works} in CSV, "
            f"top-{options['top_pct']}% threshold = {work_threshold} records, "
            f"{len(existing_works)} already in DB, "
            f"{len(new_works)} to import"
            + (f" ({'; '.join(skip_parts)})." if skip_parts else ".")
        )

        if not dry_run and new_works:
            with transaction.atomic():
                created = Work.objects.bulk_create(
                    new_works, batch_size=500, ignore_conflicts=True
                )
            self.stdout.write(self.style.SUCCESS(f"  Imported {len(created)} works."))

        if dry_run:
            self.stdout.write("(dry run — no changes written)")
        else:
            from django.core.management import call_command
            self.stdout.write("Seeding MLAIB ELO ratings...")
            call_command("seed_mlaib_elo", stdout=self.stdout)


# ── helpers ───────────────────────────────────────────────────────────────────

def _quantile_threshold(counts: list[int], top_pct: float) -> int:
    """Return the minimum count to be in the top top_pct fraction of counts.

    Uses ceiling so the cohort is never smaller than requested. All records
    tied at the boundary count are included, so the final set may be slightly
    larger than top_pct — this is intentional to avoid ordering artifacts.
    """
    sorted_desc = sorted(counts, reverse=True)
    n = max(1, math.ceil(len(sorted_desc) * top_pct))
    return sorted_desc[n - 1]


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
