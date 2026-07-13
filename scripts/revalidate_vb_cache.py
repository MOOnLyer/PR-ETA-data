#!/usr/bin/env python3
"""Delete cached Visa Bulletin HTML pages so the next fetch re-downloads them.

`fetch_visa_bulletin.py --cache-dir` treats a cached month as immutable: once a page is
cached it is never re-requested, so a State *correction* to an already-cached bulletin
would never be picked up. This script drops cache entries to force a re-fetch —
`--months N` clears the trailing N months (where State's corrections realistically land),
`--all` clears the whole cache. Run it before the fetch step; the fetch then re-downloads
exactly the dropped months.

Usage:
    python3 scripts/revalidate_vb_cache.py --cache-dir .cache/vb --months 24
    python3 scripts/revalidate_vb_cache.py --cache-dir .cache/vb --all
    python3 scripts/revalidate_vb_cache.py --self-test
"""

import argparse
import sys
from datetime import date
from pathlib import Path


def trailing_months(n, today=None):
    """The `n` most-recent 'yyyy-MM' strings, newest first, ending at `today`."""
    today = today or date.today()
    year, month = today.year, today.month
    result = []
    for _ in range(n):
        result.append(f"{year:04d}-{month:02d}")
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return result


def self_test():
    got = trailing_months(3, date(2026, 1, 15))
    expected = ["2026-01", "2025-12", "2025-11"]
    if got != expected:
        print(f"SELF-TEST FAILED: {got}")
        return 1
    print("SELF-TEST PASSED")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Bust cached Visa Bulletin pages.")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--months", type=int, help="Clear the trailing N months")
    parser.add_argument("--all", action="store_true", help="Clear the entire cache")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.cache_dir or (args.months is None and not args.all):
        parser.error("provide --cache-dir and (--months N or --all)")
    if not args.cache_dir.exists():
        print(f"{args.cache_dir}: nothing cached yet")
        return 0

    if args.all:
        pages = list(args.cache_dir.glob("*.html"))
        for page in pages:
            page.unlink()
        print(f"Cleared entire cache ({len(pages)} pages)")
        return 0

    removed = 0
    for month in trailing_months(args.months):
        page = args.cache_dir / f"{month}.html"
        if page.exists():
            page.unlink()
            removed += 1
            print(f"  will re-fetch {month}")
    print(f"Dropped {removed} of the trailing {args.months} cached months")
    return 0


if __name__ == "__main__":
    sys.exit(main())
