#!/usr/bin/env python3
"""Validate author-work relationships in data/works.csv against data/author_work_mapping.csv.

Requires running build_author_work_mapping.py first to produce:
  data/author_work_mapping.csv
  data/author_presence.csv

For each work in data/works.csv, classifies the author-work relationship as:
  VALIDATED                — confirmed in author_work_mapping.csv
  AUTHOR_NOT_IN_MLAIB_DATA — author absent from all American lit records in mlaib_data
  AUTHOR_PRESENT_NO_WORKS  — author found in mlaib_data but no subject works recorded
  WORK_NOT_IN_MLAIB_DATA   — author known but this title not found in any record

Outputs:
  data/validated_works.csv          — confirmed works, with genre + mapping count
  data/unvalidated_works.csv        — works needing manual review, with reason
  data/unmatched_entities_summary.txt — human-readable summary
"""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path


def _normalize_title(title: str) -> str:
    """Normalize a title for matching (not for display)."""
    t = re.sub(r"<[^>]+>", "", title).lower().strip()
    t = re.sub(r"^(the|a|an)\s+", "", t)
    t = re.sub(r"[^\w\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _author_key(last_name: str, birth_year: str, first_name: str = "") -> tuple[str, str, str]:
    """Stable author key matching build_author_work_mapping.py's _author_agg_key."""
    return (
        last_name.lower(),
        str(birth_year or ""),
        first_name[:3].lower(),
    )


def load_mapping(mapping_path: Path) -> dict[tuple, dict[str, dict]]:
    """Load author_work_mapping.csv.

    Returns: {author_key: {title_norm: row_dict}}
    """
    work_lookup: dict[tuple, dict[str, dict]] = defaultdict(dict)
    with mapping_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            a_key = _author_key(row["last_name"], row["birth_year"], row["first_name"])
            title_norm = _normalize_title(row["work_title"])
            work_lookup[a_key][title_norm] = row
    return work_lookup


def load_presence(presence_path: Path) -> set[tuple]:
    """Load author_presence.csv. Returns set of author keys."""
    known: set[tuple] = set()
    with presence_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            known.add(_author_key(row["last_name"], row["birth_year"], row["first_name"]))
    return known


def load_authors(authors_path: Path) -> dict[str, dict]:
    """Load authors.csv. Returns {author_id: row_dict}."""
    authors: dict[str, dict] = {}
    with authors_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            authors[row["author_id"]] = row
    return authors


def classify_work(
    title: str,
    author_row: dict,
    work_lookup: dict[tuple, dict[str, dict]],
    presence_set: set[tuple],
) -> tuple[str, dict | None]:
    """Return (reason, mapping_row_or_None) for this work."""
    last_name = author_row.get("last_name", "")
    birth = author_row.get("birth", "")
    first_name = author_row.get("first_name", "")

    a_key = _author_key(last_name, birth, first_name)
    title_norm = _normalize_title(title)

    # Primary lookup: (last_name, birth_year, fn_prefix)
    if a_key in work_lookup:
        if title_norm in work_lookup[a_key]:
            return "VALIDATED", work_lookup[a_key][title_norm]
        return "WORK_NOT_IN_MLAIB_DATA", None

    if a_key in presence_set:
        return "AUTHOR_PRESENT_NO_WORKS", None

    # Fallback: try without birth year in case of mismatch between CSV sources
    if birth:
        a_key_no_birth = _author_key(last_name, "", first_name)
        if a_key_no_birth in work_lookup:
            if title_norm in work_lookup[a_key_no_birth]:
                return "VALIDATED", work_lookup[a_key_no_birth][title_norm]
            return "WORK_NOT_IN_MLAIB_DATA", None
        if a_key_no_birth in presence_set:
            return "AUTHOR_PRESENT_NO_WORKS", None

    return "AUTHOR_NOT_IN_MLAIB_DATA", None


def main() -> None:
    data_dir = Path(__file__).resolve().parent.parent / "data"
    mapping_path = data_dir / "author_work_mapping.csv"
    presence_path = data_dir / "author_presence.csv"
    authors_path = data_dir / "authors.csv"
    works_path = data_dir / "works.csv"

    for p in [mapping_path, presence_path, authors_path, works_path]:
        if not p.exists():
            sys.exit(f"ERROR: required file not found: {p}\n"
                     "       Run build_author_work_mapping.py first.")

    print("Loading mapping …", flush=True)
    work_lookup = load_mapping(mapping_path)
    n_mapping_entries = sum(len(v) for v in work_lookup.values())
    print(f"  {n_mapping_entries:,} work entries, {len(work_lookup):,} authors", flush=True)

    print("Loading author presence …", flush=True)
    presence_set = load_presence(presence_path)
    print(f"  {len(presence_set):,} authors", flush=True)

    print("Loading authors.csv …", flush=True)
    authors = load_authors(authors_path)
    print(f"  {len(authors):,} authors", flush=True)

    print("Classifying works …", flush=True)

    validated_fields = [
        "work_id", "title", "author_id", "year", "mlaib_record_count",
        "genres", "mapping_record_count",
    ]
    unvalidated_fields = [
        "work_id", "title", "author_id", "year", "mlaib_record_count",
        "author_name", "reason",
    ]

    validated_rows: list[dict] = []
    unvalidated_rows: list[dict] = []
    counts: dict[str, int] = {
        "VALIDATED": 0,
        "AUTHOR_NOT_IN_MLAIB_DATA": 0,
        "AUTHOR_PRESENT_NO_WORKS": 0,
        "WORK_NOT_IN_MLAIB_DATA": 0,
    }

    with works_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            work_id = row.get("work_id", "")
            title = row.get("title", "")
            author_id = row.get("author_id", "")
            year = row.get("year", "")
            mrc = row.get("mlaib_record_count", "")

            author_row = authors.get(str(author_id), {})
            first = author_row.get("first_name", "")
            last = author_row.get("last_name", "")
            author_name = f"{first} {last}".strip() or f"author_id={author_id}"

            reason, mapping_row = classify_work(title, author_row, work_lookup, presence_set)
            counts[reason] += 1

            if reason == "VALIDATED":
                validated_rows.append(
                    {
                        "work_id": work_id,
                        "title": title,
                        "author_id": author_id,
                        "year": year,
                        "mlaib_record_count": mrc,
                        "genres": mapping_row.get("genres", "") if mapping_row else "",
                        "mapping_record_count": (
                            mapping_row.get("record_count", "") if mapping_row else ""
                        ),
                    }
                )
            else:
                unvalidated_rows.append(
                    {
                        "work_id": work_id,
                        "title": title,
                        "author_id": author_id,
                        "year": year,
                        "mlaib_record_count": mrc,
                        "author_name": author_name,
                        "reason": reason,
                    }
                )

    # ── Write validated_works.csv ──────────────────────────────────────────────
    validated_path = data_dir / "validated_works.csv"
    with validated_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=validated_fields)
        writer.writeheader()
        writer.writerows(validated_rows)
    print(f"Wrote {validated_path} ({len(validated_rows):,} rows)", flush=True)

    # ── Write unvalidated_works.csv ────────────────────────────────────────────
    unvalidated_path = data_dir / "unvalidated_works.csv"
    # Sort: WORK_NOT_IN_MLAIB_DATA first (most actionable), then others
    reason_order = {
        "WORK_NOT_IN_MLAIB_DATA": 0,
        "AUTHOR_PRESENT_NO_WORKS": 1,
        "AUTHOR_NOT_IN_MLAIB_DATA": 2,
    }
    unvalidated_rows.sort(key=lambda r: (reason_order.get(r["reason"], 9), r["author_name"]))
    with unvalidated_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=unvalidated_fields)
        writer.writeheader()
        writer.writerows(unvalidated_rows)
    print(f"Wrote {unvalidated_path} ({len(unvalidated_rows):,} rows)", flush=True)

    # ── Identify authors not found in mlaib_data at all ───────────────────────
    all_known_keys = set(work_lookup.keys()) | presence_set
    unmatched_authors = []
    for author_id, author_row in sorted(authors.items(), key=lambda kv: kv[1].get("last_name", "")):
        last = author_row.get("last_name", "")
        birth = author_row.get("birth", "")
        first = author_row.get("first_name", "")
        a_key = _author_key(last, birth, first)
        a_key_no_birth = _author_key(last, "", first)
        if a_key not in all_known_keys and a_key_no_birth not in all_known_keys:
            unmatched_authors.append(
                f"  {first} {last} (birth={birth or 'unknown'})".strip()
            )

    # ── Write summary ──────────────────────────────────────────────────────────
    total_works = sum(counts.values())
    total_authors = len(authors)

    summary_lines = [
        "Author-Work Validation Summary",
        "=" * 50,
        f"Authors in authors.csv:          {total_authors:>6,}",
        f"Authors not in mlaib_data:       {len(unmatched_authors):>6,}",
        "",
        f"Works in works.csv:              {total_works:>6,}",
        f"  VALIDATED:                     {counts['VALIDATED']:>6,}"
        f"  ({100 * counts['VALIDATED'] / max(total_works, 1):.1f}%)",
        f"  WORK_NOT_IN_MLAIB_DATA:        {counts['WORK_NOT_IN_MLAIB_DATA']:>6,}",
        f"  AUTHOR_PRESENT_NO_WORKS:       {counts['AUTHOR_PRESENT_NO_WORKS']:>6,}",
        f"  AUTHOR_NOT_IN_MLAIB_DATA:      {counts['AUTHOR_NOT_IN_MLAIB_DATA']:>6,}",
        "",
        "Notes:",
        "  WORK_NOT_IN_MLAIB_DATA   — author is confirmed but this work title was not",
        "                             found. May be in mlaib_xml (newer) but not mlaib_data.",
        "  AUTHOR_PRESENT_NO_WORKS  — author appears in mlaib_data but only in records",
        "                             without specific subject works listed.",
        "  AUTHOR_NOT_IN_MLAIB_DATA — author has no American lit records in mlaib_data.",
        "                             Could be mlaib_xml-only or a pipeline error.",
        "",
        f"Authors not found in mlaib_data ({len(unmatched_authors):,}):",
    ]
    MAX_LISTED = 200
    summary_lines.extend(unmatched_authors[:MAX_LISTED])
    if len(unmatched_authors) > MAX_LISTED:
        summary_lines.append(
            f"  … and {len(unmatched_authors) - MAX_LISTED} more "
            "(filter unvalidated_works.csv by AUTHOR_NOT_IN_MLAIB_DATA)"
        )

    summary_path = data_dir / "unmatched_entities_summary.txt"
    with summary_path.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(summary_lines) + "\n")
    print(f"Wrote {summary_path}", flush=True)

    print(f"\n{'=' * 50}", flush=True)
    print(f"VALIDATED:                {counts['VALIDATED']:,}", flush=True)
    print(f"WORK_NOT_IN_MLAIB_DATA:   {counts['WORK_NOT_IN_MLAIB_DATA']:,}", flush=True)
    print(f"AUTHOR_PRESENT_NO_WORKS:  {counts['AUTHOR_PRESENT_NO_WORKS']:,}", flush=True)
    print(f"AUTHOR_NOT_IN_MLAIB_DATA: {counts['AUTHOR_NOT_IN_MLAIB_DATA']:,}", flush=True)


if __name__ == "__main__":
    main()
