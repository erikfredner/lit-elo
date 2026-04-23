"""Management command: import authors and works from data/authors.csv and data/works.csv.

Existing records are matched by name (authors) or title+author (works) and skipped,
so ELO ratings and LLMMatchup history for already-loaded items are preserved.

Usage:
    python manage.py import_csv_data
    python manage.py import_csv_data --dry-run
    python manage.py import_csv_data --min-count 10  # raise threshold
    python manage.py import_csv_data --min-count 0   # import everything
    python manage.py import_csv_data --works data/validated_works.csv

--min-count N  Include authors with mlaib_record_count >= N, plus authors whose
               work has mlaib_record_count >= N. Only works with mlaib_record_count
               >= 20 are imported. Ignored when --works is given (the works file
               itself is the quality gate).
--works FILE   Use a custom works CSV instead of data/works.csv. Only authors
               referenced in that file are eligible for import; the --min-count
               threshold still applies within that set. A 'genres' column
               (semicolon-separated) populates Work.form if present.
"""

from __future__ import annotations

import csv
from collections import defaultdict
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
            help=(
                "Include authors with mlaib_record_count >= N, plus authors whose work "
                "has mlaib_record_count >= N. Only works with mlaib_record_count >= 20 "
                "are imported (default: 20). Ignored when --works is provided."
            ),
        )
        parser.add_argument(
            "--works",
            type=Path,
            default=None,
            metavar="FILE",
            help=(
                "Custom works CSV to import instead of data/works.csv. Only authors "
                "referenced in this file are imported; --min-count is ignored. A "
                "'genres' column (semicolon-separated) populates Work.form if present."
            ),
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        min_count: int = options["min_count"]
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

        # ── Pass 2: determine which authors to include ────────────────────────
        if custom_works:
            # Limit the candidate pool to authors referenced in the works file,
            # then apply the same min-count threshold as the default path.
            works_file_author_ids = set(all_work_rows.keys()) & set(all_author_rows.keys())

            author_passes_threshold: set[int] = set()
            for author_id in works_file_author_ids:
                count = _parse_int(all_author_rows[author_id].get("mlaib_record_count")) or 0
                if count >= min_count:
                    author_passes_threshold.add(author_id)

            author_lifted_by_work: set[int] = set()
            for author_id in works_file_author_ids:
                if author_id in author_passes_threshold:
                    continue
                for work_row in all_work_rows[author_id]:
                    count = _parse_int(work_row.get("mlaib_record_count")) or 0
                    if count >= min_count:
                        author_lifted_by_work.add(author_id)
                        break

            included_author_ids = author_passes_threshold | author_lifted_by_work
            not_in_file = len(all_author_rows) - len(works_file_author_ids)
            below_threshold = len(works_file_author_ids) - len(included_author_ids)
            detail_parts = [f"{len(author_passes_threshold)} meet author threshold"]
            if author_lifted_by_work:
                detail_parts.append(f"{len(author_lifted_by_work)} added via high-count work")
            if not_in_file:
                detail_parts.append(f"{not_in_file} not in works file")
            if below_threshold:
                detail_parts.append(f"{below_threshold} below min-count threshold")
        else:
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
            detail_parts = [f"{len(author_passes_threshold)} meet author threshold"]
            if author_lifted_by_work:
                detail_parts.append(f"{len(author_lifted_by_work)} added via high-count work")
            if excluded_count:
                detail_parts.append(f"{excluded_count} excluded")

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

        # ── Works: import works by included authors with mlaib_record_count > 2 ──
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
                if work_count < 20:
                    skipped_low_count += 1
                    continue
                title = row["title"].strip()
                if (title.lower(), author.pk) in existing_works:
                    continue
                # Populate form from first genre in semicolon-separated genres column.
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
            skip_parts.append(f"{skipped_low_count} skipped — mlaib_record_count < 20")
        if skipped_no_author:
            skip_parts.append(f"{skipped_no_author} skipped — author not in DB")
        self.stdout.write(
            f"Works: {total_csv_works} in CSV, "
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


