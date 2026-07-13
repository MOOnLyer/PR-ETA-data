#!/usr/bin/env python3
"""Extracts the ISA's published 永住 (permanent residence) processing periods and emits
canonical japan_processing_periods JSON (national figures, provenance official).

Source: 在留審査処理期間 publications at
https://www.moj.go.jp/isa/applications/resources/nyuukokukanri07_00140.html

Publication history:
- Quarterly PDFs cover Apr 2017 → Sep 2024 and monthly PDFs begin Oct 2024, BUT the
  永住者 row (with the note that 永住許可申請 is included in the 変更 column) only
  appears from the 令和7年2月 (February 2025) monthly PDF onward. Earlier files list
  other statuses only, so this script extracts the monthly PDFs from 2025-02.
- Each 永住者 row carries two figures for 変更等: days to 処分（告知） (disposition
  notified — the wait an applicant actually experiences) and days to 審査終了. The first
  is used. National only; no per-bureau breakdown.
- Records are emitted at the source's native monthly granularity (one record per
  publication month).

Note the metric: it averages *granted* applications, to notification. That differs from
the queue model's "all decisions" drain — the difference is exactly what the κ
calibration absorbs (docs/METHODOLOGY.md §2.5).

Requires pdfplumber (the one non-stdlib dependency in this repo's pipelines):
    python3 -m pip install pdfplumber

Usage:
    # Monotonic upsert into the bundle (a partial fetch never drops months):
    python3 scripts/fetch_isa_periods.py \
        --merge-into PRETA/Resources/SeedData/japan_processing_periods.json
    # Or a fresh file:
    python3 scripts/fetch_isa_periods.py --out PRETA/Resources/SeedData/japan_processing_periods.json
    python3 scripts/fetch_isa_periods.py --self-test
"""

import argparse
import io
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ARCHIVE_URL = "https://www.moj.go.jp/isa/applications/resources/nyuukokukanri07_00140.html"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
FIRST_MONTH_WITH_PR = (2025, 2)   # 永住者 row first appears in the 令和7年2月 PDF


def fetch(url, binary=False):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    return data if binary else data.decode("utf-8", errors="replace")


def zenkaku_to_int(text):
    return text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def monthly_pdf_links(archive_html):
    """[(year, month, absolute pdf url)] for the monthly (令和N年M月) publications."""
    strip = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()
    results = []
    for m in re.finditer(r'<a[^>]+href="([^"]+\.pdf)"[^>]*>(.*?)</a>',
                         archive_html, flags=re.S | re.I):
        href, label = m.group(1), zenkaku_to_int(strip(m.group(2)))
        era = re.match(r"(令和|平成)(元|\d+)年(\d{1,2})月（", label)
        if not era:
            continue  # quarterly (第N四半期…) or unrelated
        base = 2018 if era.group(1) == "令和" else 1988
        year = base + (1 if era.group(2) == "元" else int(era.group(2)))
        month = int(era.group(3))
        url = href if href.startswith("http") else "https://www.moj.go.jp" + href
        results.append((year, month, url))
    return sorted(set(results))


def extract_pr_days(pdf_bytes):
    """The 永住者 row's first 変更 figure (days to 処分（告知）), or None if absent."""
    import pdfplumber  # deferred so --self-test runs without the dependency
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return pr_days_from_text(text)


def pr_days_from_text(text):
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("永住者") and "配偶者" not in stripped:
            numbers = re.findall(r"\d+(?:\.\d+)?", stripped)
            if numbers:
                return float(numbers[0])
    return None


def monthly_records(monthly_values):
    """{(year, month): days} -> canonical monthly records, sorted."""
    return [
        {
            "month": f"{year:04d}-{month:02d}",
            "averageDays": round(days, 1),
            "provenance": "official",
        }
        for (year, month), days in sorted(monthly_values.items())
    ]


# ------------------------------------------------------------------ self-test

def self_test():
    failures = []

    def expect(cond, msg):
        if not cond:
            failures.append(msg)

    sample = """在留審査処理期間（日数）
令和8年4月許可分
教授 25.8 31.3 16.8 25.9 16.4
永住者の配偶者等 118.0 39.9 27.4 52.7 41.7
永住者 317.9 305.5
定住者 130.6 43.8 30.6 48.6 36.9"""
    expect(pr_days_from_text(sample) == 317.9, "永住者 row extraction")
    expect(pr_days_from_text("教授 25.8\n永住者の配偶者等 118.0") is None,
           "spouse-of-PR must not match")

    archive = ('<a href="/isa/content/001439009.pdf">令和７年４月（PDF：99KB）</a>'
               '<a href="/isa/content/930003325.pdf">第１四半期（平成２９年４月１日～６月３０日）（PDF）</a>'
               '<a href="/isa/content/001466245.pdf">令和８年５月（PDF：109KB）</a>')
    links = monthly_pdf_links(archive)
    expect(links == [
        (2025, 4, "https://www.moj.go.jp/isa/content/001439009.pdf"),
        (2026, 5, "https://www.moj.go.jp/isa/content/001466245.pdf"),
    ], f"archive link parse: {links}")

    records = monthly_records({(2025, 5): 300.04, (2025, 4): 313.8})
    expect(records == [
        {"month": "2025-04", "averageDays": 313.8, "provenance": "official"},
        {"month": "2025-05", "averageDays": 300.0, "provenance": "official"},
    ], f"monthly records: {records}")

    if failures:
        print("SELF-TEST FAILED:")
        for line in failures:
            print("  -", line)
        return 1
    print("SELF-TEST PASSED")
    return 0


# ------------------------------------------------------------------------ main

def main():
    parser = argparse.ArgumentParser(description="Extract ISA 永住 processing periods.")
    parser.add_argument("--out", type=Path, help="Write records to this JSON file")
    parser.add_argument("--merge-into", type=Path,
                        help="Existing japan_processing_periods.json to upsert into "
                             "(monotonic — a partial fetch never drops months)")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.out and not args.merge_into:
        parser.error("provide --out or --merge-into (or --self-test)")

    links = monthly_pdf_links(fetch(ARCHIVE_URL))
    monthly = {}
    for year, month, url in links:
        if (year, month) < FIRST_MONTH_WITH_PR:
            continue
        try:
            days = extract_pr_days(fetch(url, binary=True))
            if days is None:
                print(f"  ! {year}-{month:02d}: no 永住者 row", file=sys.stderr)
            else:
                monthly[(year, month)] = days
                print(f"  {year}-{month:02d}: {days} days")
        except Exception as error:  # noqa: BLE001 — report and continue
            print(f"  ! {year}-{month:02d}: {error}", file=sys.stderr)
        time.sleep(args.delay)

    records = monthly_records(monthly)

    if args.merge_into:
        existing = (json.loads(args.merge_into.read_text())
                    if args.merge_into.exists() else [])
        combined = {(r["month"], r.get("office")): r for r in existing}
        added = sum(1 for r in records
                    if (r["month"], r.get("office")) not in combined)
        for record in records:
            combined[(record["month"], record.get("office"))] = record
        merged = sorted(combined.values(),
                        key=lambda r: (r["month"], r.get("office") or ""))
        args.merge_into.parent.mkdir(parents=True, exist_ok=True)
        args.merge_into.write_text(json.dumps(merged, indent=0))
        print(f"Merged {len(records)} fetched ({added} new) → {args.merge_into} "
              f"[{len(merged)} records]")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(records, indent=0))
    print(f"Wrote {len(records)} monthly records to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
