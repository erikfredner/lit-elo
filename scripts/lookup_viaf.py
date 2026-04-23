"""Look up VIAF IDs for authors in data/authors.csv.

Reads data/authors.csv, queries the VIAF AutoSuggest API for each author
using their name and birth/death years, and writes the VIAF ID back into
the CSV. A JSON cache at data/.viaf_cache.json makes the script resumable:
already-looked-up authors are skipped on subsequent runs.

Usage:
    python scripts/lookup_viaf.py                   # all authors >= min-count 20
    python scripts/lookup_viaf.py --limit 10        # test with first 10 lookups
    python scripts/lookup_viaf.py --min-count 5     # lower the threshold
    python scripts/lookup_viaf.py --min-count 0     # look up everyone
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent.parent / "data"
AUTHORS_CSV = DATA_DIR / "authors.csv"
CACHE_FILE = DATA_DIR / ".viaf_cache.json"

VIAF_AUTOSUGGEST = "https://viaf.org/viaf/AutoSuggest"
# Cloudflare on viaf.org blocks Python's default User-Agent; a browser UA is required.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
SEARCH_DELAY = 1.0
TIMEOUT = 30
MAX_RETRIES = 3


# ── Helpers (reused verbatim from the corpus VIAF script) ─────────────────────

def name_tokens(name: str) -> set[str]:
    """Return lowercase ASCII tokens for name similarity — skip digits and noise words."""
    nfd = unicodedata.normalize("NFD", name)
    ascii_only = nfd.encode("ascii", "ignore").decode()
    tokens = set(re.findall(r"\w+", ascii_only.lower()))
    return {t for t in tokens if len(t) > 1 and not t.isdigit() and t not in ("jr", "sr", "ii", "iii", "iv")}


def name_similarity(a: str, b: str) -> float:
    """Word-level Jaccard similarity between two name strings."""
    ta = name_tokens(a)
    tb = name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def extract_years(text: str) -> list[int]:
    """Extract plausible 4-digit year numbers from a string."""
    return [int(y) for y in re.findall(r"\b(1[5-9]\d\d|20\d\d)\b", text)]


def viaf_autosuggest(query: str) -> list[dict]:
    """Query VIAF AutoSuggest and return the result list, retrying on transient errors."""
    url = f"{VIAF_AUTOSUGGEST}?{urllib.parse.urlencode({'query': query})}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.load(resp)
                return data.get("result") or []
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"\n  Attempt {attempt} failed ({exc}); retrying in {wait}s …")
                time.sleep(wait)
            else:
                print(f"\n  Warning: giving up after {MAX_RETRIES} attempts ({exc})")
    return []


def score_candidate(candidate: dict, author: str, birth: str, death: str) -> float:
    """Score a VIAF AutoSuggest candidate.

    Returns -1 for non-personal names; otherwise a float where higher is better.
    Date matches contribute most (1 point each); name similarity fills the gap
    when dates are absent or ambiguous.
    """
    if candidate.get("nametype") != "personal":
        return -1.0

    term = candidate.get("term", "")
    years = extract_years(term)

    date_score = 0.0
    if birth and int(birth) in years:
        date_score += 1.0
    if death and int(death) in years:
        date_score += 1.0

    sim = name_similarity(author, term)
    return date_score * 2 + sim


def _viaf_query(name: str) -> str:
    """Strip trailing honorifics that cause VIAF AutoSuggest to return no results.

    VIAF AutoSuggest returns 0 results for queries ending in 'Jr.', 'Sr.', 'II',
    etc.  Removing them before querying lets the API match the underlying record;
    name_tokens() already strips these tokens during scoring so similarity is
    unaffected.
    """
    return re.sub(
        r"[\s,]+\b(jr\.?|sr\.?|ii|iii|iv|v)\b\.?\s*$", "", name, flags=re.IGNORECASE
    ).strip()


def find_viaf_id(author: str, birth: str, death: str) -> str:
    """Return the best-matching VIAF ID for an author, or '' if not found."""
    results = viaf_autosuggest(_viaf_query(author))

    personal = [r for r in results if r.get("nametype") == "personal"]
    if not personal:
        return ""

    scored = sorted(
        ((score_candidate(r, author, birth, death), r) for r in personal),
        key=lambda x: x[0],
        reverse=True,
    )
    best_score, best = scored[0]

    if name_similarity(author, best.get("term", "")) < 0.3:
        return ""

    return str(best.get("viafid", ""))


# ── Cache helpers ─────────────────────────────────────────────────────────────

def load_cache() -> dict[str, str]:
    """Load author_id → viaf_id cache from disk, or return empty dict."""
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_cache(cache: dict[str, str]) -> None:
    """Atomically write cache to disk."""
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    tmp.replace(CACHE_FILE)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--min-count",
        type=int,
        default=20,
        metavar="N",
        help="Only look up authors with mlaib_record_count >= N (default: 20).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N API calls (useful for testing).",
    )
    args = parser.parse_args()

    if not AUTHORS_CSV.exists():
        raise SystemExit(f"Not found: {AUTHORS_CSV}  (run parse_mlaib_xml.py first)")

    with AUTHORS_CSV.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Filter to authors above the citation threshold
    eligible = [r for r in rows if (int(r.get("mlaib_record_count") or 0)) >= args.min_count]
    print(f"{len(eligible)} authors with mlaib_record_count >= {args.min_count} (of {len(rows)} total)")

    def _row_name(row: dict) -> str:
        first = row.get("first_name", "").strip()
        last = row.get("last_name", "").strip()
        return f"{first} {last}".strip() if first or last else row.get("author_id", "?")

    cache = load_cache()
    already_done = sum(1 for r in eligible if cache.get(_row_name(r)))
    to_fetch = [r for r in eligible if not cache.get(_row_name(r))]
    if args.limit is not None:
        to_fetch = to_fetch[:args.limit]

    print(f"{already_done} already cached, {len(to_fetch)} to look up")
    if not to_fetch:
        print("Nothing to do — writing CSV and exiting.")
    else:
        total = len(to_fetch)
        matched = 0

        for i, row in enumerate(to_fetch, 1):
            name = _row_name(row)
            birth = row.get("birth", "")
            death = row.get("death", "")

            print(f"[{i:{len(str(total))}}/{total}] {name!r} ...", end=" ", flush=True)

            viaf_id = find_viaf_id(name, birth, death)
            cache[name] = viaf_id
            save_cache(cache)

            if viaf_id:
                matched += 1
                print(f"→ {viaf_id}")
            else:
                print("no match")

            time.sleep(SEARCH_DELAY)

        print(f"\nMatched {matched}/{total} authors.")

    # Merge cache back into authors.csv
    fieldnames = list(rows[0].keys()) if rows else []
    if "viaf_id" not in fieldnames:
        fieldnames.append("viaf_id")

    for row in rows:
        name = _row_name(row)
        if name in cache:
            row["viaf_id"] = cache[name]

    with AUTHORS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    filled = sum(1 for r in rows if r.get("viaf_id"))
    print(f"Wrote {len(rows)} rows to {AUTHORS_CSV} ({filled} with VIAF IDs)")


if __name__ == "__main__":
    main()
