"""Django integration tests for the import_csv_data management command.

Tests verify that mlaib_elo is computed and stored correctly on newly imported
Author records, and that existing records are not modified.
"""

import tempfile
import textwrap
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from core.models import Author


def _write_csvs(tmpdir: Path, authors_rows: list[dict], works_rows: list[dict] | None = None) -> None:
    """Write minimal authors.csv and works.csv to tmpdir."""
    authors_path = tmpdir / "authors.csv"
    with authors_path.open("w", encoding="utf-8") as f:
        f.write("author_id,first_name,last_name,birth,death,mlaib_record_count,viaf_id\n")
        for row in authors_rows:
            f.write(
                "{author_id},{first_name},{last_name},{birth},{death},{mlaib_record_count},{viaf_id}\n".format(
                    author_id=row.get("author_id", ""),
                    first_name=row.get("first_name", ""),
                    last_name=row.get("last_name", ""),
                    birth=row.get("birth", ""),
                    death=row.get("death", ""),
                    mlaib_record_count=row.get("mlaib_record_count", ""),
                    viaf_id=row.get("viaf_id", ""),
                )
            )
    works_path = tmpdir / "works.csv"
    with works_path.open("w", encoding="utf-8") as f:
        f.write("author_id,title,year,mlaib_record_count\n")
        for row in (works_rows or []):
            f.write(
                "{author_id},{title},{year},{mlaib_record_count}\n".format(**row)
            )


def _run(tmpdir: Path, **kwargs):
    """Call import_csv_data with _DATA_DIR patched to tmpdir."""
    out = StringIO()
    defaults = dict(min_count=0, dry_run=False, stdout=out, stderr=StringIO())
    defaults.update(kwargs)
    with patch("core.management.commands.import_csv_data._DATA_DIR", tmpdir):
        call_command("import_csv_data", **defaults)
    return out.getvalue()


class ImportCsvDataMlaiEloTests(TestCase):
    def test_mlaib_elo_set_for_new_authors(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 10},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
                {"author_id": 3, "first_name": "Carol", "last_name": "C", "mlaib_record_count": 200},
            ])
            _run(tmpdir)
        self.assertEqual(Author.objects.filter(mlaib_elo__isnull=False).count(), 3)

    def test_min_count_author_gets_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 10},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
                {"author_id": 3, "first_name": "Carol", "last_name": "C", "mlaib_record_count": 200},
            ])
            _run(tmpdir)
        author = Author.objects.get(name="Alice A")
        self.assertAlmostEqual(author.mlaib_elo, 0.0)

    def test_max_count_author_gets_3000(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 10},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
                {"author_id": 3, "first_name": "Carol", "last_name": "C", "mlaib_record_count": 200},
            ])
            _run(tmpdir)
        author = Author.objects.get(name="Carol C")
        self.assertAlmostEqual(author.mlaib_elo, 3000.0)

    def test_single_author_mlaib_elo_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 500},
            ])
            _run(tmpdir)
        author = Author.objects.get(name="Alice A")
        self.assertIsNone(author.mlaib_elo)

    def test_all_same_count_gets_midpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 100},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 100},
                {"author_id": 3, "first_name": "Carol", "last_name": "C", "mlaib_record_count": 100},
            ])
            _run(tmpdir)
        for author in Author.objects.all():
            self.assertAlmostEqual(author.mlaib_elo, 1500.0)

    def test_existing_author_not_overwritten(self):
        Author.objects.create(name="Alice A", mlaib_elo=999.0, mlaib_record_count=10)
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 10},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
            ])
            _run(tmpdir)
        author = Author.objects.get(name="Alice A")
        self.assertAlmostEqual(author.mlaib_elo, 999.0)

    def test_missing_count_yields_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": ""},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
            ])
            _run(tmpdir)
        author = Author.objects.get(name="Alice A")
        self.assertIsNone(author.mlaib_elo)

    def test_dry_run_creates_no_authors(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 10},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
            ])
            _run(tmpdir, dry_run=True)
        self.assertEqual(Author.objects.count(), 0)

    def test_mlaib_elo_within_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write_csvs(tmpdir, [
                {"author_id": 1, "first_name": "Alice", "last_name": "A", "mlaib_record_count": 5},
                {"author_id": 2, "first_name": "Bob", "last_name": "B", "mlaib_record_count": 50},
                {"author_id": 3, "first_name": "Carol", "last_name": "C", "mlaib_record_count": 500},
                {"author_id": 4, "first_name": "Dave", "last_name": "D", "mlaib_record_count": 5000},
            ])
            _run(tmpdir)
        for author in Author.objects.filter(mlaib_elo__isnull=False):
            self.assertGreaterEqual(author.mlaib_elo, 0.0)
            self.assertLessEqual(author.mlaib_elo, 3000.0)
