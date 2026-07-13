# PR ETA — public data mirror

The refreshed datasets behind the **PR ETA** app (permanent-residence timelines for the
U.S. and Japan). The app's source lives in a private repo; this repo is public so the
app's in-app updater can read fresh data from stable URLs:

| File | Contents |
|---|---|
| [`us_visa_bulletin.json`](us_visa_bulletin.json) | Every monthly U.S. Visa Bulletin cell, 2001-12→present (Final Action + Dates for Filing, all categories × chargeability areas) |
| [`japan_monthly_stats.json`](japan_monthly_stats.json) | Japan 永住 monthly flow by immigration bureau — received / decided / pending + 許可/不許可 approval split, 2007-01→present |
| [`japan_processing_periods.json`](japan_processing_periods.json) | ISA published average 永住 processing period (national, monthly, 2025-02→present) |

Raw URL pattern the app reads:
`https://raw.githubusercontent.com/MOOnLyer/PR-ETA-data/main/<file>.json`

## How it stays fresh

[`.github/workflows/refresh.yml`](.github/workflows/refresh.yml) runs on the 16th of each
month (and on demand) with **no credentials** — it scrapes the public government sources
and commits any changes with the built-in token:

- **U.S. Visa Bulletin** — `scripts/fetch_visa_bulletin.py` scrapes travel.state.gov
  (pages cached between runs).
- **Japan flow** — `scripts/fetch_moj_monthly.py` reads the MOJ monthly Excel releases
  (pre-2012 legacy `.xls` needs `xlrd`).
- **ISA processing periods** — `scripts/fetch_isa_periods.py` extracts the 永住者 row from
  the ISA's monthly PDFs (needs `pdfplumber`).

The government sites occasionally flake mid-run, and two scrapers rebuild-and-overwrite
their whole file, so before committing the workflow runs
[`scripts/check_dataset_regression.py`](scripts/check_dataset_regression.py): any dataset
that lost records (a partial fetch) is restored from HEAD, so a flaky run can never
publish a shrink. Every script has an offline `--self-test`.

The data is public government information; estimates derived from it in the app are
statistical projections, **not legal advice**.
