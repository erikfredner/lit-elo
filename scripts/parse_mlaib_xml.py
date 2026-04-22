"""Parse MLAIB XML files and emit data/authors.csv + data/works.csv.

Reads every XML record in data/mlaib_xml/, identifies American-literature
authors and their cited works via a subject-string state machine, disambiguates
nationality via co-occurrence profiling, and writes two CSVs ready for
`python manage.py import_csv_data`.

Usage:
    python scripts/parse_mlaib_xml.py
    python scripts/parse_mlaib_xml.py --max-year 1950  # optional publication cutoff
"""

from __future__ import annotations

import argparse
import csv
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent.parent / "data"
XML_DIR = DATA_DIR / "mlaib_xml"
XML_FILES = sorted(XML_DIR.glob("*.xml"))
AUTHORS_CSV = DATA_DIR / "authors.csv"
WORKS_CSV = DATA_DIR / "works.csv"

# ── Regex patterns ─────────────────────────────────────────────────────────────

PERIOD_RE = re.compile(r"^\d{4}-\d{4}$")

# Matches "Last, First (YYYY-YYYY)", "Last, First (1951- )", "Last, Jr. (ca.1700-1760)",
# "Last, First (fl. 1820)", "Last, First (b. 1800)", etc.
DATED_AUTHOR_RE = re.compile(
    r"^[A-Z\u00C0-\u024F].+,\s+.+\("
    r"(?:\d{3,4}[/\d]*\??-(?:ca\.\s*)?[\d\s\?]*"
    r"|(?:ca\.|fl\.|b\.|d\.)\s*\d{3,4}(?:[/\-]\d{2,4})?)"
    r"\s*\)$"
)

# Matches "Last, First" with no parenthetical dates
UNDATED_AUTHOR_RE = re.compile(r"^[A-Z][A-Za-z'\-]+,\s+[A-Z][A-Za-z\.\s']+$")

# Matches "Some Title (1851)"
WORK_SIMPLE_RE = re.compile(r"^(.+)\s+\((\d{4})\)$")

# Matches "Some Title (1893, rev. 1896)" or "Title (1893/1896)" — use first year
WORK_COMPLEX_RE = re.compile(r"^(.+?)\s+\((\d{4})[^)]+\)$")


# ── Data containers ────────────────────────────────────────────────────────────

@dataclass
class AuthorData:
    display_name: str          # "Last, First" or "Last"
    first_name: str
    last_name: str
    birth: int | None
    death: int | None
    count: int = 0             # number of MLAIB records in which this author appears


@dataclass
class WorkData:
    title: str
    author_display_name: str   # key into author_records
    year: int | None
    count: int = 0             # number of MLAIB records in which this work appears


# ── Helpers ────────────────────────────────────────────────────────────────────

def parse_author_token(token: str) -> tuple[str, int | None, int | None]:
    """Extract (display_name, birth_year, death_year) from an author token."""
    m = re.match(r"^(.+?)\s*\((.+)\)\s*$", token)
    if not m:
        return token.strip(), None, None

    name = m.group(1).strip()
    date_str = m.group(2).strip()

    m2 = re.match(r"(\d{3,4})[/\d]*\??-(?:ca\.\s*)?(\d{4})?\s*$", date_str)
    if m2:
        birth = int(m2.group(1))
        death = int(m2.group(2)) if m2.group(2) else None
        return name, birth, death

    m3 = re.match(r"ca\.\s*(\d{3,4})-(\d{3,4})$", date_str)
    if m3:
        return name, int(m3.group(1)), int(m3.group(2))

    m4 = re.match(r"(?:ca\.|fl\.|b\.|d\.)\s*(\d{3,4})", date_str)
    if m4:
        return name, int(m4.group(1)), None

    return name, None, None


def split_display_name(display_name: str) -> tuple[str, str]:
    """Split MLAIB 'Last, First[, Suffix]' into (first_name, last_name).

    MLAIB occasionally appends honorific suffixes after a second comma
    (e.g. "James, Henry, Jr.").  We take only the token immediately after
    the first comma as the first name and discard trailing suffixes so that
    the final display name is "Henry James" rather than "Henry, Jr. James".
    Returns ("", name) for single-token names with no comma.
    """
    if "," not in display_name:
        return "", display_name.strip()
    parts = [p.strip() for p in display_name.split(",")]
    last = parts[0]
    first = parts[1] if len(parts) > 1 else ""
    # parts[2:] are suffixes (Jr., Sr., III …) — intentionally dropped
    return first, last


def classify_token(tok: str, nat_lit_types: frozenset) -> str:
    """Return token type: 'nat_lit', 'period', 'author_dated', 'author_undated',
    'work', or 'other'."""
    if tok in nat_lit_types:
        return "nat_lit"
    if PERIOD_RE.match(tok):
        return "period"
    if DATED_AUTHOR_RE.match(tok):
        return "author_dated"
    m_simple = WORK_SIMPLE_RE.match(tok)
    if m_simple and not DATED_AUTHOR_RE.match(tok):
        return "work"
    m_complex = WORK_COMPLEX_RE.match(tok)
    if m_complex and not DATED_AUTHOR_RE.match(tok):
        return "work"
    if UNDATED_AUTHOR_RE.match(tok):
        return "author_undated"
    return "other"


def extract_work_year(tok: str) -> tuple[str, int] | None:
    """Return (title, year) from a dated work token, or None."""
    m = WORK_SIMPLE_RE.match(tok)
    if m:
        return m.group(1).strip(), int(m.group(2))
    m = WORK_COMPLEX_RE.match(tok)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return None


# ── Pre-scans ─────────────────────────────────────────────────────────────────

def collect_nat_lit_types(xml_files: list[Path]) -> frozenset[str]:
    """Collect every token at position 0 of a subjects string.
    Position-0 tokens are always national literature markers."""
    types: set[str] = set()
    for path in xml_files:
        tree = ET.parse(path)
        for subj_el in tree.iterfind(".//subjects"):
            text = (subj_el.text or "").strip()
            if not text:
                continue
            first_tok = text.split(" ; ")[0].strip()
            if first_tok:
                types.add(first_tok)
    return frozenset(types)


def build_author_nat_profiles(
    xml_files: list[Path], nat_lit_types: frozenset
) -> tuple[dict, dict]:
    """For each author, count co-occurrences with non-American national literature
    tokens across all records.

    Returns:
        author_nat:   author_name → Counter(foreign_nat_lit → record_count)
        author_total: author_name → total records the author appeared in
    """
    author_nat: dict[str, Counter] = defaultdict(Counter)
    author_total: dict[str, int] = defaultdict(int)

    for path in xml_files:
        tree = ET.parse(path)
        for record in tree.iterfind(".//record"):
            subj_el = record.find("subjects")
            if subj_el is None:
                continue
            text = (subj_el.text or "").strip()
            if not text:
                continue

            tokens = [t.strip() for t in text.split(" ; ")]

            foreign_nat_lits = {
                t for t in tokens
                if t in nat_lit_types
                and t != "American literature"
                and not t.startswith("American literature ")
            }

            for tok in tokens:
                if DATED_AUTHOR_RE.match(tok) or UNDATED_AUTHOR_RE.match(tok):
                    name, _, _ = parse_author_token(tok)
                    author_total[name] += 1
                    for nat_lit in foreign_nat_lits:
                        author_nat[name][nat_lit] += 1

    return dict(author_nat), dict(author_total)


def is_american_author(name: str, author_nat: dict, author_total: dict) -> bool:
    """Return True if the author should be treated as American.

    An author is considered non-American if their single most common
    non-American national literature appears in >= 50% of all records
    where they appear as a subject.
    """
    total = author_total.get(name, 0)
    if total == 0:
        return True
    foreign = author_nat.get(name)
    if not foreign:
        return True
    top_count = foreign.most_common(1)[0][1]
    return top_count / total < 0.5


# ── Core parsing ───────────────────────────────────────────────────────────────

def process_xml_files(
    xml_files: list[Path],
    nat_lit_types: frozenset,
    author_nat: dict,
    author_total: dict,
    max_year: int | None,
) -> tuple[dict[str, AuthorData], dict[tuple[str, str], WorkData]]:
    """Parse all records and collect author and work data.

    Returns:
        author_records: display_name → AuthorData
        work_records:   (title, author_display_name) → WorkData
    """
    author_records: dict[str, AuthorData] = {}
    work_records: dict[tuple[str, str], WorkData] = {}

    for path in xml_files:
        print(f"Parsing {path.name}...")
        tree = ET.parse(path)
        for record in tree.iterfind(".//record"):
            subj_el = record.find("subjects")
            if subj_el is None:
                continue
            subj_text = (subj_el.text or "").strip()
            if not subj_text or "American literature" not in subj_text:
                continue

            tokens = [t.strip() for t in subj_text.split(" ; ")]

            _process_record(
                tokens,
                nat_lit_types,
                author_nat,
                author_total,
                author_records,
                work_records,
                max_year,
            )

    return author_records, work_records


def _process_record(
    tokens: list[str],
    nat_lit_types: frozenset,
    author_nat: dict,
    author_total: dict,
    author_records: dict[str, AuthorData],
    work_records: dict[tuple[str, str], WorkData],
    max_year: int | None,
) -> None:
    in_am_lit = False
    current_author_name: str | None = None  # display name ("Last, First")
    # Deduplicate authors and works within a single record
    seen_authors_this_rec: set[str] = set()
    seen_works_this_rec: set[tuple[str, str]] = set()

    for tok in tokens:
        # ── national literature boundary ───────────────────────────────────────
        if tok == "American literature" or tok.startswith("American literature "):
            in_am_lit = True
            current_author_name = None
            # Handle fused period: "American literature 1800-1899"
            if tok != "American literature":
                suffix = tok[len("American literature"):].strip()
                if PERIOD_RE.match(suffix):
                    pass  # period noted but not used for filtering
            continue

        kind = classify_token(tok, nat_lit_types)

        if kind == "nat_lit":
            # Entering a different national literature section
            in_am_lit = False
            current_author_name = None
            continue

        if not in_am_lit:
            continue

        # ── within American literature context ─────────────────────────────────
        if kind in ("author_dated", "author_undated"):
            name, birth, death = parse_author_token(tok)
            if not is_american_author(name, author_nat, author_total):
                current_author_name = None  # non-American; don't attach works
                continue
            current_author_name = name
            if name not in seen_authors_this_rec:
                seen_authors_this_rec.add(name)
                if name not in author_records:
                    first, last = split_display_name(name)
                    author_records[name] = AuthorData(
                        display_name=name,
                        first_name=first,
                        last_name=last,
                        birth=birth,
                        death=death,
                    )
                author_records[name].count += 1

        elif kind == "work" and current_author_name is not None:
            result = extract_work_year(tok)
            if result is None:
                continue
            title, year = result
            if max_year is not None and year > max_year:
                continue
            key = (title, current_author_name)
            if key not in seen_works_this_rec:
                seen_works_this_rec.add(key)
                if key not in work_records:
                    work_records[key] = WorkData(
                        title=title,
                        author_display_name=current_author_name,
                        year=year,
                    )
                work_records[key].count += 1

        # "period", "other" tokens: no-op, but don't reset current_author_name


# ── Output ─────────────────────────────────────────────────────────────────────

AUTHOR_FIELDNAMES = ["author_id", "first_name", "last_name", "birth", "death", "mlaib_record_count", "viaf_id"]
WORK_FIELDNAMES = ["work_id", "title", "author_id", "year", "mlaib_record_count"]


def write_csvs(
    author_records: dict[str, AuthorData],
    work_records: dict[tuple[str, str], WorkData],
) -> tuple[int, int]:
    """Write authors.csv and works.csv; return (author_count, work_count)."""
    # Sort authors by count descending, then name ascending
    sorted_authors = sorted(
        author_records.values(),
        key=lambda a: (-a.count, a.display_name),
    )
    # Assign sequential IDs
    author_id_map: dict[str, int] = {}
    with AUTHORS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=AUTHOR_FIELDNAMES)
        writer.writeheader()
        for i, a in enumerate(sorted_authors, start=1):
            author_id_map[a.display_name] = i
            writer.writerow({
                "author_id": i,
                "first_name": a.first_name,
                "last_name": a.last_name,
                "birth": a.birth if a.birth is not None else "",
                "death": a.death if a.death is not None else "",
                "mlaib_record_count": a.count,
                "viaf_id": "",
            })

    # Sort works by author_id, then count descending, then title
    sorted_works = sorted(
        work_records.values(),
        key=lambda w: (
            author_id_map.get(w.author_display_name, 999999),
            -w.count,
            w.title,
        ),
    )
    written_works = 0
    with WORKS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=WORK_FIELDNAMES)
        writer.writeheader()
        work_id = 1
        for w in sorted_works:
            aid = author_id_map.get(w.author_display_name)
            if aid is None:
                continue  # author was filtered out (shouldn't happen)
            writer.writerow({
                "work_id": work_id,
                "title": w.title,
                "author_id": aid,
                "year": w.year if w.year is not None else "",
                "mlaib_record_count": w.count,
            })
            work_id += 1
            written_works += 1

    return len(sorted_authors), written_works


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--max-year",
        type=int,
        default=None,
        metavar="YEAR",
        help="Exclude works published after this year (default: no cutoff).",
    )
    args = parser.parse_args()

    if not XML_FILES:
        raise SystemExit(f"No XML files found in {XML_DIR}")

    print(f"Found {len(XML_FILES)} XML file(s) in {XML_DIR}")

    print("Collecting national literature types...")
    nat_lit_types = collect_nat_lit_types(XML_FILES)
    print(f"  Found {len(nat_lit_types)} national literature type tokens.")

    print("Building author nationality profiles...")
    author_nat, author_total = build_author_nat_profiles(XML_FILES, nat_lit_types)
    print(f"  Profiled {len(author_total)} unique authors across all records.")

    print("Processing records for American literature authors and works...")
    author_records, work_records = process_xml_files(
        XML_FILES, nat_lit_types, author_nat, author_total, args.max_year
    )
    print(f"  Found {len(author_records)} American literature authors.")
    print(f"  Found {len(work_records)} unique works (title + author).")

    print("Writing CSVs...")
    n_authors, n_works = write_csvs(author_records, work_records)
    print(f"  Wrote {n_authors} authors to {AUTHORS_CSV}")
    print(f"  Wrote {n_works} works to {WORKS_CSV}")


if __name__ == "__main__":
    main()
