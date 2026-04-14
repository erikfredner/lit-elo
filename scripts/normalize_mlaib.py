#!/usr/bin/env python3
"""Normalize mlaib_authors.csv and mlaib_works.csv into clean relational tables.

Outputs:
  data/authors.csv  — author_id, last_name, first_name, birth, death,
                       dates_uncertain, mlaib_record_count
  data/works.csv    — work_id, title, year, year_uncertain, author_last_name,
                       author_id, mlaib_record_count
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class AuthorRow:
    author_id: int
    last_name: str
    first_name: str
    birth: Optional[int]
    death: Optional[int]
    dates_uncertain: bool
    mlaib_record_count: int


@dataclass
class WorkRow:
    work_id: int
    title: str
    year: Optional[int]
    year_uncertain: bool
    author_last_name: str
    author_id: Optional[int]
    mlaib_record_count: int


# ── Author date parsing ────────────────────────────────────────────────────────

def _year_from_part(part: str) -> Optional[int]:
    """Extract the base year from one side of a date range.

    Handles 'ca. 1340', '1340/5', '1340?', etc.
    """
    part = re.sub(r'^ca\.\s*', '', part.strip())
    part = part.rstrip('?')
    m = re.match(r'(\d+)', part)
    return int(m.group(1)) if m else None


def _parse_bc_ad(s: str) -> tuple[Optional[int], Optional[int]]:
    """Parse dates containing B.C./A.D. notation.

    Examples:
        '43 B.C.-18 A.D.'  → (-43, 18)
        '384-322 B.C.'      → (-384, -322)
        '70 B.C.-19 B.C.'  → (-70, -19)
        '495-406 B.C.'      → (-495, -406)
    """
    # Collect all (year, era) pairs; era is '' when not explicitly labelled.
    pairs = [(int(y), era) for y, era in re.findall(r'(\d+)(?:\s+(B\.C\.|A\.D\.))?', s) if y]

    if not pairs:
        return None, None
    if len(pairs) == 1:
        y, era = pairs[0]
        return (y * -1 if era == 'B.C.' else y), None

    birth_num, birth_era = pairs[0]
    death_num, death_era = pairs[1]

    # If death is B.C. and birth has no explicit era, infer birth is also B.C.
    if death_era == 'B.C.' and not birth_era:
        birth_era = 'B.C.'

    birth = birth_num * (-1 if birth_era == 'B.C.' else 1)
    death = death_num * (-1 if death_era == 'B.C.' else 1)
    return birth, death


def parse_dates(date_str: str) -> tuple[Optional[int], Optional[int], bool]:
    """Parse the date portion of a subjectAuthor field (without outer parens).

    Returns (birth, death, dates_uncertain).
    """
    s = date_str.strip()
    uncertain = bool(re.search(r'[?]|ca\.|/', s))

    # fl. (flourished) — no extractable birth/death
    if s.startswith('fl.'):
        return None, None, True

    # d. YYYY — death date only, birth unknown
    if s.startswith('d.'):
        m = re.search(r'(\d+)', s[2:])
        death = int(m.group(1)) if m else None
        return None, death, True

    # B.C./A.D. notation
    if 'B.C.' in s or 'A.D.' in s:
        birth, death = _parse_bc_ad(s)
        return birth, death, False

    # Living author: date string ends with '- ' or just '-'
    if re.search(r'-\s*$', s):
        birth_part = re.sub(r'-\s*$', '', s)
        birth = _year_from_part(birth_part)
        return birth, None, uncertain

    # Standard range: split on the dash that separates birth from death.
    # Lookbehind: must follow a digit or '?'.
    # Lookahead: must precede a digit or 'ca.' (handles 'ca. YYYY-ca. YYYY').
    parts = re.split(r'(?<=[\d?])-(?=\d|ca\.)', s, maxsplit=1)
    if len(parts) == 2:
        return _year_from_part(parts[0]), _year_from_part(parts[1]), uncertain

    # Fallback: extract whatever numeric years exist
    years = re.findall(r'\d{3,4}', s)
    if years:
        birth = int(years[0])
        death = int(years[-1]) if len(years) > 1 else None
        return birth, death, True

    return None, None, True


def parse_author_field(raw: str) -> dict:
    """Parse a subjectAuthor value into name components and dates.

    Expected format: 'LastName, FirstName(birth-death)'
    Single-name ancients: 'Dante(1265-1321)'
    """
    paren_idx = raw.rfind('(')
    if paren_idx == -1:
        name = raw.strip()
        birth = death = None
        uncertain = False
    else:
        name = raw[:paren_idx].strip()
        date_str = raw[paren_idx + 1:].rstrip(')')
        birth, death, uncertain = parse_dates(date_str)

    if ', ' in name:
        last_name, first_name = name.split(', ', 1)
    else:
        last_name, first_name = name, ''

    return {
        'last_name': last_name.strip(),
        'first_name': first_name.strip(),
        'birth': birth,
        'death': death,
        'dates_uncertain': uncertain,
    }


# ── Work field parsing ─────────────────────────────────────────────────────────

def _extract_work_year(year_str: str) -> tuple[Optional[int], bool]:
    """Return (earliest_year, uncertain) from a work year string.

    Handles: '1922', '1600-1601', '1799, 1805, 1850', 'ca. 1320',
             '1798, rev. 1817', '', etc.
    """
    if not year_str:
        return None, True

    uncertain = bool(re.search(r'ca\.|[?]', year_str))
    years = [int(y) for y in re.findall(r'\d{3,4}', year_str)]

    if not years:
        return None, True

    return min(years), uncertain


def _clean_title(raw: str) -> str:
    """Strip HTML italic tags and surrounding quote characters from a title."""
    title = re.sub(r'</?i>', '', raw)
    title = title.strip('"')
    return title.strip()


def parse_work_field(raw: str) -> dict:
    """Parse a subjectWork value into title and year components.

    Expected patterns:
        '<i>Title</i>(year)'  →  title='Title', year=...
        '"Title"(year)'       →  title='Title', year=...   (short stories/poems)
        'Series name'         →  title='Series name', year=None
    """
    # Find the trailing (year) block, if present
    m = re.search(r'\(([^)]+)\)$', raw)
    if m:
        year_str = m.group(1)
        raw_title = raw[: m.start()]
    else:
        year_str = ''
        raw_title = raw

    title = _clean_title(raw_title)
    year, year_uncertain = _extract_work_year(year_str)

    return {'title': title, 'year': year, 'year_uncertain': year_uncertain}


# ── Processing ─────────────────────────────────────────────────────────────────

def process_authors(
    csv_path: Path,
) -> tuple[list[AuthorRow], dict[str, int], dict[int, str]]:
    """Read mlaib_authors.csv; return rows, raw→id lookup, and id→last_name map."""
    rows: list[AuthorRow] = []
    raw_to_id: dict[str, int] = {}
    errors: list[str] = []

    with csv_path.open(newline='', encoding='utf-8') as fh:
        for i, row in enumerate(csv.DictReader(fh), start=1):
            raw = row['subjectAuthor']
            try:
                parsed = parse_author_field(raw)
                count = int(row['count'])
            except Exception as exc:
                errors.append(f'  row {i}: {raw!r} → {exc}')
                continue

            rows.append(
                AuthorRow(
                    author_id=i,
                    last_name=parsed['last_name'],
                    first_name=parsed['first_name'],
                    birth=parsed['birth'],
                    death=parsed['death'],
                    dates_uncertain=parsed['dates_uncertain'],
                    mlaib_record_count=count,
                )
            )
            raw_to_id[raw] = i

    if errors:
        print(f'  WARNING: {len(errors)} author parse error(s):')
        for e in errors:
            print(e, file=sys.stderr)

    id_to_last: dict[int, str] = {r.author_id: r.last_name for r in rows}
    return rows, raw_to_id, id_to_last


def process_works(
    csv_path: Path,
    raw_to_id: dict[str, int],
    id_to_last: dict[int, str],
) -> list[WorkRow]:
    """Read mlaib_works.csv; return normalized work rows."""
    rows: list[WorkRow] = []
    errors: list[str] = []

    with csv_path.open(newline='', encoding='utf-8') as fh:
        for i, row in enumerate(csv.DictReader(fh), start=1):
            raw_author = row['subjectAuthor']
            raw_work = row['subjectWork']
            try:
                parsed = parse_work_field(raw_work)
                count = int(row['count'])
            except Exception as exc:
                errors.append(f'  row {i}: {raw_work!r} → {exc}')
                continue

            author_id = raw_to_id.get(raw_author)
            if author_id is not None:
                author_last_name = id_to_last[author_id]
            else:
                # Author absent from authors CSV — parse name directly
                try:
                    author_last_name = parse_author_field(raw_author)['last_name']
                except Exception:
                    author_last_name = raw_author.split(',')[0].strip()

            rows.append(
                WorkRow(
                    work_id=i,
                    title=parsed['title'],
                    year=parsed['year'],
                    year_uncertain=parsed['year_uncertain'],
                    author_last_name=author_last_name,
                    author_id=author_id,
                    mlaib_record_count=count,
                )
            )

    if errors:
        print(f'  WARNING: {len(errors)} work parse error(s):')
        for e in errors:
            print(e, file=sys.stderr)

    return rows


# ── I/O ────────────────────────────────────────────────────────────────────────

_AUTHOR_FIELDS = [
    'author_id', 'last_name', 'first_name', 'birth', 'death',
    'dates_uncertain', 'mlaib_record_count',
]

_WORK_FIELDS = [
    'work_id', 'title', 'year', 'year_uncertain',
    'author_last_name', 'author_id', 'mlaib_record_count',
]


def _opt(value: Optional[int]) -> str:
    """Render an optional integer as an empty string when absent."""
    return '' if value is None else str(value)


def write_authors(rows: list[AuthorRow], out_path: Path) -> None:
    with out_path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh)
        writer.writerow(_AUTHOR_FIELDS)
        for r in rows:
            writer.writerow([
                r.author_id, r.last_name, r.first_name,
                _opt(r.birth), _opt(r.death),
                r.dates_uncertain, r.mlaib_record_count,
            ])


def write_works(rows: list[WorkRow], out_path: Path) -> None:
    with out_path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh)
        writer.writerow(_WORK_FIELDS)
        for r in rows:
            writer.writerow([
                r.work_id, r.title,
                _opt(r.year), r.year_uncertain,
                r.author_last_name, _opt(r.author_id),
                r.mlaib_record_count,
            ])


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_argument_parser() -> argparse.ArgumentParser:
    default_data = Path(__file__).resolve().parent.parent / 'data'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--authors',
        type=Path,
        default=default_data / 'mlaib_authors.csv',
        help='Input authors CSV (default: data/mlaib_authors.csv)',
    )
    parser.add_argument(
        '--works',
        type=Path,
        default=default_data / 'mlaib_works.csv',
        help='Input works CSV (default: data/mlaib_works.csv)',
    )
    parser.add_argument(
        '--out-dir',
        type=Path,
        default=default_data,
        help='Output directory for authors.csv and works.csv (default: data/)',
    )
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f'Reading {args.authors} …')
    author_rows, raw_to_id, id_to_last = process_authors(args.authors)
    print(f'  {len(author_rows):,} authors parsed.')

    print(f'Reading {args.works} …')
    work_rows = process_works(args.works, raw_to_id, id_to_last)
    print(f'  {len(work_rows):,} works parsed.')

    authors_out = out_dir / 'authors.csv'
    works_out = out_dir / 'works.csv'

    write_authors(author_rows, authors_out)
    print(f'Wrote {authors_out}')

    write_works(work_rows, works_out)
    print(f'Wrote {works_out}')

    # Warn about any works whose author wasn't found in the authors table
    unlinked = [w for w in work_rows if w.author_id is None]
    if unlinked:
        print(f'\n  NOTE: {len(unlinked)} work(s) could not be linked to an author row:')
        for w in unlinked:
            print(f'    work_id={w.work_id}  author_last_name={w.author_last_name!r}  title={w.title!r}')


if __name__ == '__main__':
    main()
