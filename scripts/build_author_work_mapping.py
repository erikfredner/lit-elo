#!/usr/bin/env python3
"""Build a validated author-work mapping table from data/mlaib_data/ XML records.

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


def main() -> None:
    data_dir = Path(__file__).resolve().parent.parent / "data"
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


if __name__ == "__main__":
    main()
