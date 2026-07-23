#!/usr/bin/env python3
"""Merge a manually saved Visa Bulletin page into us_visa_bulletin.json.

The recovery path while travel.state.gov's bot protection blocks automated fetches
(Cloudflare, since ~July 2026): open the bulletin in a normal browser, save it
(⌘S — "Webpage, HTML Only"), then:

    python3 scripts/merge_bulletin_page.py page.html 2026-08 \
        us_visa_bulletin.json

Parses with the same parser as the scraper and upserts by
(bulletinMonth, category, area, table) — replacing that month's rows if re-run,
never touching other months.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fetch_visa_bulletin as vb  # noqa: E402


def merge(entries, dataset):
    keyed = {(r["bulletinMonth"], r["category"], r["area"], r["table"]): r
             for r in dataset}
    added = replaced = 0
    for r in entries:
        key = (r["bulletinMonth"], r["category"], r["area"], r["table"])
        if key in keyed:
            replaced += 1
        else:
            added += 1
        keyed[key] = r
    merged = sorted(keyed.values(),
                    key=lambda r: (r["bulletinMonth"], r["table"], r["category"],
                                   r["area"]))
    return merged, added, replaced


def self_test():
    dataset = [
        {"bulletinMonth": "2026-07", "category": "EB2", "area": "india",
         "table": "finalAction", "status": "2013-09-01", "provenance": "official"},
    ]
    new = [
        {"bulletinMonth": "2026-08", "category": "EB2", "area": "india",
         "table": "finalAction", "status": "2013-10-01", "provenance": "official"},
        {"bulletinMonth": "2026-07", "category": "EB2", "area": "india",
         "table": "finalAction", "status": "2013-09-15", "provenance": "official"},
    ]
    merged, added, replaced = merge(new, dataset)
    ok = (added == 1 and replaced == 1 and len(merged) == 2
          and merged[0]["status"] == "2013-09-15")
    print("SELF-TEST PASSED" if ok else f"SELF-TEST FAILED: {merged}")
    return 0 if ok else 1


def main():
    if "--self-test" in sys.argv:
        return self_test()
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    page, month, dataset_path = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    raw = Path(page).read_text(encoding="utf-8", errors="replace")
    entries = vb.parse_bulletin(raw, month)
    if not entries:
        print(f"ERROR: no bulletin tables parsed from {page} — is it the right save "
              "format? Use 'Webpage, HTML Only'.", file=sys.stderr)
        return 1
    months = {r["bulletinMonth"] for r in entries}
    if months != {month}:
        print(f"ERROR: parsed rows carry months {sorted(months)}, expected {month}.",
              file=sys.stderr)
        return 1
    dataset = json.load(open(dataset_path))
    before = len(dataset)
    merged, added, replaced = merge(entries, dataset)
    dataset_path.write_text(json.dumps(merged, indent=0))
    print(f"{page}: parsed {len(entries)} rows for {month} — merged into "
          f"{dataset_path} ({before} → {len(merged)} rows; {added} new, "
          f"{replaced} replaced)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
