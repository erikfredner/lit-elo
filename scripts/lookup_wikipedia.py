"""Retrieve English Wikipedia URLs from VIAF justlinks for authors with a viaf_id.

Reads Author records from the Django DB, queries the VIAF justlinks.json endpoint
for each author that has a viaf_id, and writes the English Wikipedia URL back to
the DB.  A JSON cache at data/.wikipedia_cache.json (keyed by viaf_id) makes the
script fully resumable — authors already in the cache are skipped by default.

Usage:
    python scripts/lookup_wikipedia.py               # all authors with viaf_id
    python scripts/lookup_wikipedia.py --limit 5     # test with first 5 lookups
    python scripts/lookup_wikipedia.py --dry-run     # fetch but do not write to DB
    python scripts/lookup_wikipedia.py --force       # re-fetch even cached entries
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── Django setup ───────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

from core.models import Author  # noqa: E402

# ── Configuration ──────────────────────────────────────────────────────────────

DATA_DIR = REPO_ROOT / "data"
CACHE_FILE = DATA_DIR / ".wikipedia_cache.json"

VIAF_RECORD = "https://viaf.org/viaf/{viaf_id}"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SEARCH_DELAY = 1.0
TIMEOUT = 30
MAX_RETRIES = 3


# ── VIAF helpers ───────────────────────────────────────────────────────────────

def fetch_viaf_record(viaf_id: str) -> dict:
    """Fetch the VIAF cluster record for a VIAF ID via content negotiation.

    VIAF no longer supports /justlinks.json or /viaf.json path suffixes.
    Sending Accept: application/json on the base URL triggers a redirect that
    returns the full cluster record as JSON.
    """
    url = VIAF_RECORD.format(viaf_id=viaf_id)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"\n  Attempt {attempt} failed ({exc}); retrying in {wait}s …")
                time.sleep(wait)
            else:
                print(f"\n  Warning: giving up after {MAX_RETRIES} attempts ({exc})")
    return {}


def extract_en_wikipedia_url(data: dict) -> str:
    """Return the English Wikipedia URL from a VIAF cluster record, or ''.

    The record structure is:
      data['ns1:VIAFCluster']['ns1:xLinks']['ns1:xLink']
    Each xLink entry has 'type' == 'Wikipedia' and 'content' == the URL.
    ns1:xLink may be a single dict or a list of dicts.
    """
    try:
        xlink = data["ns1:VIAFCluster"]["ns1:xLinks"]["ns1:xLink"]
    except (KeyError, TypeError):
        return ""

    # Normalise to list
    if isinstance(xlink, dict):
        xlink = [xlink]

    en_urls = [
        entry["content"]
        for entry in xlink
        if isinstance(entry, dict)
        and entry.get("type") == "Wikipedia"
        and "en.wikipedia.org" in entry.get("content", "")
    ]

    if not en_urls:
        return ""
    # Prefer https; otherwise take first match
    https = [u for u in en_urls if u.startswith("https://")]
    return https[0] if https else en_urls[0]


# ── Cache helpers ─────────────────────────────────────────────────────────────

def load_cache() -> dict[str, str]:
    """Load viaf_id → wikipedia_url cache from disk, or return empty dict."""
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
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N API calls (useful for testing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print results but do not write to the DB.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch authors already present in the cache.",
    )
    args = parser.parse_args()

    authors = list(Author.objects.exclude(viaf_id="").order_by("name"))
    print(f"{len(authors)} authors with viaf_id in DB")

    cache = load_cache()

    if args.force:
        to_fetch = authors
    else:
        to_fetch = [a for a in authors if a.viaf_id not in cache]

    already_done = len(authors) - len(to_fetch)
    if args.limit is not None:
        to_fetch = to_fetch[: args.limit]

    print(f"{already_done} already cached, {len(to_fetch)} to look up")

    if not to_fetch:
        print("Nothing to fetch.")
    else:
        total = len(to_fetch)
        found = 0

        for i, author in enumerate(to_fetch, 1):
            print(f"[{i:{len(str(total))}}/{total}] {author.name!r} (VIAF {author.viaf_id}) …", end=" ", flush=True)

            data = fetch_viaf_record(author.viaf_id)
            url = extract_en_wikipedia_url(data)

            cache[author.viaf_id] = url
            save_cache(cache)

            if url:
                found += 1
                print(f"→ {url}")
            else:
                print("no Wikipedia link")

            time.sleep(SEARCH_DELAY)

        print(f"\nFound {found}/{total} Wikipedia URLs in this batch.")

    # Write all cached URLs back to the DB
    if args.dry_run:
        print("Dry run — skipping DB writes.")
        return

    updated = 0
    for author in authors:
        cached_url = cache.get(author.viaf_id, "")
        if cached_url != author.wikipedia_url:
            author.wikipedia_url = cached_url
            author.save(update_fields=["wikipedia_url"])
            updated += 1

    total_with_url = Author.objects.exclude(wikipedia_url="").count()
    print(f"Updated {updated} DB rows. Total authors with Wikipedia URL: {total_with_url}")


if __name__ == "__main__":
    main()
