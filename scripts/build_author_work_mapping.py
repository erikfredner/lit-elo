#!/usr/bin/env python3
"""Build author-work data from data/mlaib_data/ XML records.

Processes individual MLAIB XML records using multiprocessing. Unlike the
consolidated mlaib_xml approach (semicolon-delimited subjects), these records
use structured XML that unambiguously distinguishes:
  <co:subjectWorks>/<co:work>  — works AUTHORED by the subject
  <co:feature>/<co:theme>      — topics of study (historical events, concepts)

Only American literature records (<co:specificLiteratures> containing
"American literature") are included.

Outputs:
  data/author_work_mapping.csv  — one row per unique (author, work) pair,
                                  with genre and record-count information
  data/author_presence.csv      — all authors seen in American lit records,
                                  with total record counts
  data/authors.csv              — import-ready author list for import_csv_data
  data/works.csv                — import-ready work list for import_csv_data
"""

from __future__ import annotations

import csv
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from multiprocessing import Pool
from pathlib import Path

# Reuse parsing helpers from normalize_mlaib.py
sys.path.insert(0, str(Path(__file__).parent))
from normalize_mlaib import parse_author_field, parse_work_field  # noqa: E402

CO_NS = "https://www.mla.org/Schema/CommonModules/co"


def _co(tag: str) -> str:
    return f"{{{CO_NS}}}{tag}"


def _normalize_title(title: str) -> str:
    """Normalize a work title for deduplication (not for display)."""
    t = re.sub(r"<[^>]+>", "", title).lower().strip()
    t = re.sub(r"^(the|a|an)\s+", "", t)
    t = re.sub(r"[^\w\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _author_agg_key(parsed: dict) -> tuple[str, str, str]:
    """Stable author key: (last_name_lower, birth_year_str, first_name_prefix)."""
    return (
        parsed["last_name"].lower(),
        str(parsed["birth"] or ""),
        (parsed["first_name"] or "")[:3].lower(),
    )


def _is_american_lit(basic_class_el) -> bool:
    """Return True if this <co:basicClassification> targets American literature."""
    spec_lits = basic_class_el.find(_co("specificLiteratures"))
    if spec_lits is None:
        return False
    for lit_el in spec_lits.iter(_co("literature")):
        if lit_el.text and "american literature" in lit_el.text.lower():
            return True
    return False


def _genres_from_work_group(wg_el) -> list[str]:
    """Extract unique genre strings from a <co:workGroup> element."""
    seen: set[str] = set()
    result: list[str] = []
    genres_el = wg_el.find(_co("genres"))
    if genres_el is None:
        return result
    for g_el in genres_el.iter(_co("genre")):
        text = g_el.text and g_el.text.strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def process_chunk(paths: list[str]) -> list[tuple[str, str | None, str]]:
    """Parse a chunk of XML file paths.

    Returns a list of (author_raw, work_raw_or_None, genres_str) tuples.
    work_raw is None when an author appears in an American lit record
    without any associated subject works.
    """
    results: list[tuple[str, str | None, str]] = []
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            print(f"WARN: parse error in {path}", file=sys.stderr, flush=True)
            continue

        descriptors = root.find(f".//{_co('descriptors')}")
        if descriptors is None:
            continue
        nat_lits = descriptors.find(_co("nationalLiteratures"))
        if nat_lits is None:
            continue

        for bc in nat_lits.findall(_co("basicClassification")):
            if not _is_american_lit(bc):
                continue

            # Only the DIRECT child <co:subjectAuthor> of <co:basicClassification>.
            # (Avoids picking up authors named in <co:literaryTechniques> blocks.)
            author_el = bc.find(_co("subjectAuthor"))
            if author_el is None or not (author_el.text and author_el.text.strip()):
                continue
            author_raw = author_el.text.strip()

            found_works = False
            for wg in bc.findall(_co("workGroup")):
                genres_str = ";".join(_genres_from_work_group(wg))
                sw = wg.find(_co("subjectWorks"))
                if sw is None:
                    continue
                for work_el in sw.findall(_co("work")):
                    if work_el.text and work_el.text.strip():
                        results.append((author_raw, work_el.text.strip(), genres_str))
                        found_works = True

            if not found_works:
                # Author present but no subject works listed in this record.
                results.append((author_raw, None, ""))

    return results


def _write_import_csvs(
    data_dir: Path,
    pres_sorted: list[dict],
    rows_sorted: list[dict],
) -> None:
    """Write authors.csv and works.csv from pre-sorted author/work entry lists."""
    authors_path = data_dir / "authors.csv"
    author_csv_fields = [
        "author_id", "first_name", "last_name", "birth", "death",
        "mlaib_record_count", "viaf_id",
    ]
    author_id_map: dict[tuple, int] = {}
    with authors_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=author_csv_fields)
        writer.writeheader()
        for i, entry in enumerate(pres_sorted, start=1):
            a_key = (
                entry["last_name"].lower(),
                str(entry["birth_year"]),
                (entry["first_name"] or "")[:3].lower(),
            )
            author_id_map[a_key] = i
            writer.writerow({
                "author_id": i,
                "first_name": entry["first_name"],
                "last_name": entry["last_name"],
                "birth": entry["birth_year"],
                "death": entry["death_year"],
                "mlaib_record_count": entry["record_count"],
                "viaf_id": "",
            })
    print(f"Wrote {authors_path} ({len(pres_sorted):,} authors)", flush=True)

    works_path = data_dir / "works.csv"
    work_csv_fields = [
        "work_id", "title", "author_id", "year", "mlaib_record_count", "genres",
    ]
    with works_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=work_csv_fields)
        writer.writeheader()
        work_id = 1
        for entry in rows_sorted:
            a_key = (
                entry["last_name"].lower(),
                str(entry["birth_year"]),
                (entry["first_name"] or "")[:3].lower(),
            )
            aid = author_id_map.get(a_key)
            if aid is None:
                continue
            # genres may be a Counter (from XML scan) or a plain string (from CSV).
            genres_val = entry["genres"]
            if isinstance(genres_val, Counter):
                top_genres = ";".join(g for g, _ in genres_val.most_common(5))
            else:
                top_genres = genres_val
            writer.writerow({
                "work_id": work_id,
                "title": entry["work_title"],
                "author_id": aid,
                "year": entry["work_year"],
                "mlaib_record_count": entry["record_count"],
                "genres": top_genres,
            })
            work_id += 1
    print(f"Wrote {works_path} ({work_id - 1:,} works)", flush=True)


def regenerate_from_csv(data_dir: Path) -> None:
    """Re-generate authors.csv and works.csv from existing mapping CSVs.

    Reads author_presence.csv and author_work_mapping.csv, re-parses each
    author_raw field with the current parse_author_field, and rewrites the
    import-ready CSVs.  Much faster than a full XML scan; use after fixing
    name-parsing logic without changing the underlying data.
    """
    presence_path = data_dir / "author_presence.csv"
    mapping_path = data_dir / "author_work_mapping.csv"
    for p in (presence_path, mapping_path):
        if not p.exists():
            sys.exit(f"ERROR: {p} not found — run without --regenerate-from-csv first")

    print("Re-parsing author_presence.csv …", flush=True)
    author_map: dict[tuple, dict] = {}
    with presence_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            author_raw = row["author_raw"]
            try:
                ap = parse_author_field(author_raw)
            except Exception:
                continue
            a_key = _author_agg_key(ap)
            author_map[a_key] = {
                "author_raw": author_raw,
                "last_name": ap["last_name"],
                "first_name": ap["first_name"],
                "birth_year": ap["birth"] or "",
                "death_year": ap["death"] or "",
                "record_count": int(row.get("record_count") or 0),
            }
    pres_sorted = sorted(
        author_map.values(), key=lambda e: (-e["record_count"], e["last_name"])
    )
    print(f"  {len(pres_sorted):,} authors", flush=True)

    print("Re-parsing author_work_mapping.csv …", flush=True)
    work_map: dict[tuple, dict] = {}
    with mapping_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            author_raw = row["author_raw"]
            try:
                ap = parse_author_field(author_raw)
            except Exception:
                continue
            a_key = _author_agg_key(ap)
            title_norm = _normalize_title(row["work_title"])
            w_key = (*a_key, title_norm)
            work_map[w_key] = {
                "last_name": ap["last_name"],
                "first_name": ap["first_name"],
                "birth_year": ap["birth"] or "",
                "work_title": row["work_title"],
                "work_year": row["work_year"],
                "genres": row["genres"],  # already a pre-computed string
                "record_count": int(row.get("record_count") or 0),
            }
    rows_sorted = sorted(
        work_map.values(), key=lambda e: (-e["record_count"], e["last_name"])
    )
    print(f"  {len(rows_sorted):,} author-work pairs", flush=True)

    _write_import_csvs(data_dir, pres_sorted, rows_sorted)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--regenerate-from-csv",
        action="store_true",
        help=(
            "Re-generate authors.csv and works.csv from existing "
            "author_presence.csv and author_work_mapping.csv without "
            "re-scanning the mlaib_data XML files."
        ),
    )
    args = parser.parse_args()

    data_dir = Path(__file__).resolve().parent.parent / "data"

    if args.regenerate_from_csv:
        regenerate_from_csv(data_dir)
        return

    src_dir = data_dir / "mlaib_data"
    if not src_dir.exists():
        sys.exit(f"ERROR: {src_dir} not found")

    print(f"Scanning {src_dir} …", flush=True)
    all_files = sorted(str(p) for p in src_dir.rglob("*.xml"))
    total = len(all_files)
    print(f"  {total:,} XML files found", flush=True)

    CHUNK_SIZE = 500
    chunks = [all_files[i : i + CHUNK_SIZE] for i in range(0, total, CHUNK_SIZE)]
    n_workers = os.cpu_count() or 4
    print(
        f"Processing with {n_workers} workers, {len(chunks):,} chunks of {CHUNK_SIZE} …",
        flush=True,
    )

    # Aggregation structures, keyed by stable (last_name, birth_year, fn_prefix) tuples.
    #
    # work_map: agg_key → entry dict (includes genres Counter and record_count)
    # The work sub-key is (author_agg_key, title_norm).
    work_map: dict[tuple, dict] = {}

    # author_map: author_agg_key → entry dict (for presence CSV)
    author_map: dict[tuple, dict] = {}

    processed = 0
    with Pool(n_workers) as pool:
        for chunk_results in pool.imap_unordered(process_chunk, chunks):
            for author_raw, work_raw, genres_str in chunk_results:
                try:
                    ap = parse_author_field(author_raw)
                except Exception:
                    continue

                a_key = _author_agg_key(ap)

                # Track author presence (whether or not they have works here)
                if a_key not in author_map:
                    author_map[a_key] = {
                        "author_raw": author_raw,
                        "last_name": ap["last_name"],
                        "first_name": ap["first_name"],
                        "birth_year": ap["birth"] or "",
                        "death_year": ap["death"] or "",
                        "record_count": 0,
                    }
                author_map[a_key]["record_count"] += 1

                if work_raw is None:
                    continue

                try:
                    wp = parse_work_field(work_raw)
                except Exception:
                    continue

                title_norm = _normalize_title(wp["title"])
                w_key = (*a_key, title_norm)

                if w_key not in work_map:
                    work_map[w_key] = {
                        "author_raw": author_raw,
                        "last_name": ap["last_name"],
                        "first_name": ap["first_name"],
                        "birth_year": ap["birth"] or "",
                        "death_year": ap["death"] or "",
                        "work_title": wp["title"],
                        "work_year": wp["year"] or "",
                        "genres": Counter(),
                        "record_count": 0,
                    }
                entry = work_map[w_key]
                entry["record_count"] += 1
                for g in genres_str.split(";"):
                    g = g.strip()
                    if g:
                        entry["genres"][g] += 1

            processed += 1
            if processed % 200 == 0:
                pct = 100.0 * processed / len(chunks)
                print(
                    f"  {processed:,}/{len(chunks):,} chunks ({pct:.1f}%) — "
                    f"{len(work_map):,} author-work pairs …",
                    flush=True,
                )

    print(
        f"\nDone: {len(work_map):,} unique author-work pairs, "
        f"{len(author_map):,} unique authors",
        flush=True,
    )

    # ── Write author_work_mapping.csv ──────────────────────────────────────────
    mapping_path = data_dir / "author_work_mapping.csv"
    fields = [
        "author_raw",
        "last_name",
        "first_name",
        "birth_year",
        "death_year",
        "work_title",
        "work_year",
        "genres",
        "record_count",
    ]
    rows_sorted = sorted(
        work_map.values(), key=lambda e: (-e["record_count"], e["last_name"])
    )
    with mapping_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for entry in rows_sorted:
            top_genres = ";".join(g for g, _ in entry["genres"].most_common(5))
            writer.writerow(
                {
                    "author_raw": entry["author_raw"],
                    "last_name": entry["last_name"],
                    "first_name": entry["first_name"],
                    "birth_year": entry["birth_year"],
                    "death_year": entry["death_year"],
                    "work_title": entry["work_title"],
                    "work_year": entry["work_year"],
                    "genres": top_genres,
                    "record_count": entry["record_count"],
                }
            )
    print(f"Wrote {mapping_path} ({len(work_map):,} rows)", flush=True)

    # ── Write author_presence.csv ──────────────────────────────────────────────
    presence_path = data_dir / "author_presence.csv"
    pres_fields = [
        "author_raw",
        "last_name",
        "first_name",
        "birth_year",
        "death_year",
        "record_count",
    ]
    pres_sorted = sorted(
        author_map.values(), key=lambda e: (-e["record_count"], e["last_name"])
    )
    with presence_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=pres_fields)
        writer.writeheader()
        for entry in pres_sorted:
            writer.writerow(
                {
                    "author_raw": entry["author_raw"],
                    "last_name": entry["last_name"],
                    "first_name": entry["first_name"],
                    "birth_year": entry["birth_year"],
                    "death_year": entry["death_year"],
                    "record_count": entry["record_count"],
                }
            )
    print(f"Wrote {presence_path} ({len(author_map):,} authors)", flush=True)

    _write_import_csvs(data_dir, pres_sorted, rows_sorted)


if __name__ == "__main__":
    main()
