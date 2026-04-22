"""Management command: update viaf_id on existing Author records from data/authors.csv.

Only updates authors whose viaf_id is currently blank and who have a non-blank
viaf_id in the CSV. Matches on normalized name (first + last).

Usage:
    python manage.py update_viaf_ids
    python manage.py update_viaf_ids --dry-run
"""

from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Author

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"


def _author_name(row: dict) -> str:
    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    if first and last:
        return f"{first} {last}"
    return last or first


class Command(BaseCommand):
    help = "Update viaf_id on existing Author records from data/authors.csv."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be updated without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        authors_csv = _DATA_DIR / "authors.csv"

        if not authors_csv.exists():
            raise CommandError(f"File not found: {authors_csv}")

        # Build name → viaf_id map from CSV (skip rows with no viaf_id)
        csv_viaf: dict[str, str] = {}
        with authors_csv.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                viaf_id = (row.get("viaf_id") or "").strip()
                if viaf_id:
                    csv_viaf[_author_name(row)] = viaf_id

        self.stdout.write(f"CSV: {len(csv_viaf)} authors with a VIAF ID.")

        # Find DB authors with no viaf_id whose name appears in the CSV map
        to_update: list[Author] = []
        for author in Author.objects.filter(viaf_id=""):
            viaf_id = csv_viaf.get(author.name)
            if viaf_id:
                author.viaf_id = viaf_id
                to_update.append(author)

        self.stdout.write(f"Authors to update: {len(to_update)}")

        if dry_run:
            for a in to_update[:20]:
                self.stdout.write(f"  {a.name} → {a.viaf_id}")
            if len(to_update) > 20:
                self.stdout.write(f"  … and {len(to_update) - 20} more")
            self.stdout.write("(dry run — no changes written)")
            return

        if to_update:
            with transaction.atomic():
                Author.objects.bulk_update(to_update, ["viaf_id"], batch_size=500)
            self.stdout.write(self.style.SUCCESS(f"Updated {len(to_update)} authors."))
        else:
            self.stdout.write("Nothing to update.")
