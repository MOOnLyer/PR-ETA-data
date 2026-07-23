#!/usr/bin/env python3
"""Fail (exit 1) when the newest Visa Bulletin month in the dataset is older than the
publication schedule implies it should be.

Why: the fetcher must treat a missing month as "not published yet", so when
travel.state.gov started bot-blocking (Cloudflare 403s, July 2026) the daily runs
stayed green while silently never picking up the new bulletin. This gate encodes the
*expectation*: State publishes month M+1's bulletin mid-M, so by the 20th of M the
dataset must contain M+1 (and it must always contain M). A red run notifies the repo
owner, who can recover manually (save the page in a browser and run
fetch_visa_bulletin.py --parse-file) or investigate the block.

Usage:
    python3 scripts/check_bulletin_freshness.py us_visa_bulletin.json
    python3 scripts/check_bulletin_freshness.py --self-test
"""
import json
import sys
from datetime import datetime, timezone


def expected_month(today):
    """The newest bulletinMonth ('YYYY-MM') the dataset must contain on `today`."""
    year, month = today.year, today.month
    if today.day >= 20:
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return f"{year:04d}-{month:02d}"


def self_test():
    from datetime import date
    cases = [
        (date(2026, 7, 19), "2026-07"),   # before the 20th: current month suffices
        (date(2026, 7, 20), "2026-08"),   # from the 20th: next month expected
        (date(2026, 12, 25), "2027-01"),  # year rollover
    ]
    for today, want in cases:
        got = expected_month(today)
        if got != want:
            print(f"SELF-TEST FAILED: {today} -> {got}, want {want}")
            return 1
    print("SELF-TEST PASSED")
    return 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    path = sys.argv[1]
    rows = json.load(open(path))
    newest = max(r["bulletinMonth"] for r in rows)
    expected = expected_month(datetime.now(timezone.utc).date())
    if newest >= expected:
        print(f"Bulletin freshness OK: newest {newest} (expected ≥ {expected})")
        return 0
    print(f"STALE BULLETIN: newest is {newest} but {expected} should be published by "
          "now. The fetch is likely being blocked — check the '! HTTP ...' lines in "
          "the fetch step. Manual recovery: save the bulletin page in a browser, then "
          "fetch_visa_bulletin.py --parse-file page.html --month YYYY-MM --out ... and "
          "merge.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
