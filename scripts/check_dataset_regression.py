#!/usr/bin/env python3
"""Safety gate for the unattended monthly data refresh.

The refresh scripts fetch from live government sites that flake (travel.state.gov
layout drift, ISA PDF SSL resets, e-Stat timeouts). `fetch_visa_bulletin.py` and
`fetch_isa_periods.py` rebuild their whole file and blind-write it, so a *partial*
fetch produces a **shorter** file. Left unchecked, the workflow would commit that
regression and every install would pull it via the in-app updater.

This gate compares each dataset's working-tree record count against the version
committed at HEAD and treats any shrink (or a file that no longer parses as a JSON
list) as a regression. In `--revert` mode (used by the workflow) it restores the
offending file from HEAD so a good fetch of the *other* datasets can still commit;
without `--revert` it just reports and exits non-zero (a loud local/CI dry run).

Count is deliberately the only signal: it is schema-agnostic and catches the
catastrophic failure mode (data vanished). Value-level restatements are handled by
the app's provenance-ranked merge, not here.

Usage:
    python3 scripts/check_dataset_regression.py --revert FILE [FILE ...]
    python3 scripts/check_dataset_regression.py FILE [FILE ...]      # report-only
    python3 scripts/check_dataset_regression.py --self-test
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def record_count(text):
    """Number of records in a dataset JSON, or None if it is not a JSON list."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return len(data) if isinstance(data, list) else None


def find_regressions(new_counts, old_counts):
    """Files whose new count is missing (unparseable) or below the old count.

    `new_counts` / `old_counts` map path -> count (or None for unparseable/new-file).
    A file absent from `old_counts` (or old=None) is never a regression: brand-new
    datasets and first commits are allowed to appear.
    """
    regressed = []
    for path, new in new_counts.items():
        old = old_counts.get(path)
        if old is None:
            continue  # no baseline to regress against
        if new is None or new < old:
            regressed.append((path, old, new))
    return regressed


def git_show_head(path):
    """The file's contents at HEAD, or None if it does not exist there."""
    result = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def main():
    parser = argparse.ArgumentParser(description="Guard against dataset regressions.")
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--revert", action="store_true",
                        help="Restore regressed files from HEAD instead of just failing.")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.files:
        parser.error("provide dataset file paths (or --self-test)")

    new_counts, old_counts = {}, {}
    for path in args.files:
        key = str(path)
        new_counts[key] = record_count(path.read_text()) if path.exists() else None
        head = git_show_head(key)
        old_counts[key] = record_count(head) if head is not None else None

    regressed = find_regressions(new_counts, old_counts)
    if not regressed:
        print("No dataset regressions.")
        return 0

    for path, old, new in regressed:
        shown = "unparseable" if new is None else new
        print(f"REGRESSION {path}: {old} -> {shown}", file=sys.stderr)
        if args.revert:
            subprocess.run(["git", "checkout", "HEAD", "--", path], check=True)
            print(f"  reverted {path} to HEAD", file=sys.stderr)

    # In revert mode the offending files are restored, so the run can still commit
    # the healthy datasets: exit 0. Otherwise fail loudly.
    return 0 if args.revert else 1


def self_test():
    failures = []

    def expect(cond, msg):
        if not cond:
            failures.append(msg)

    expect(record_count('[{"a":1},{"a":2}]') == 2, "count list")
    expect(record_count('{"a":1}') is None, "object is not a list")
    expect(record_count("not json") is None, "garbage is None")

    # shrink, unparseable, and new-file baseline handling
    reg = find_regressions(
        new_counts={"vb": 90, "moj": 100, "isa": None, "new": 5},
        old_counts={"vb": 100, "moj": 100, "isa": 16, "new": None})
    got = {r[0] for r in reg}
    expect(got == {"vb", "isa"}, f"regressions: {got}")
    expect(("moj", 100, 100) not in reg, "equal count is not a regression")

    # growth is fine
    expect(find_regressions({"x": 101}, {"x": 100}) == [], "growth allowed")

    if failures:
        print("SELF-TEST FAILED:")
        for line in failures:
            print("  -", line)
        return 1
    print("SELF-TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
