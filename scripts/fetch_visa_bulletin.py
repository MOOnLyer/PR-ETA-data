#!/usr/bin/env python3
"""Fetches and parses the real U.S. Department of State Visa Bulletin.

Downloads the monthly bulletin HTML pages, parses the Final Action Dates and Dates for
Filing tables (both employment- and family-sponsored), and emits the canonical JSON that
PRETACore's VisaBulletinParser ingests — with provenance "official".

Source: https://travel.state.gov/.../visa-bulletin/{fiscalYear}/visa-bulletin-for-{month}-{year}.html
The bulletin tables are static HTML, so JavaScript rendering is not required.
If Cloudflare blocks that canonical host, the fetcher tries the same State Department
content on an official sibling host, then optionally ScraperAPI when SCRAPERAPI_KEY is set.

Usage:
    # Fetch a range and write the canonical dataset:
    python3 scripts/fetch_visa_bulletin.py --start 2016-01 --end 2025-12 \
        --out PRETA/Resources/SeedData/us_visa_bulletin.json

    # Parse a local page (offline / testing):
    python3 scripts/fetch_visa_bulletin.py --parse-file page.html --month 2024-06

    # Run the built-in parser self-test (no network):
    python3 scripts/fetch_visa_bulletin.py --self-test

Only the Python standard library is used.
"""

import argparse
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
PRIMARY_HOST = "travel.state.gov"
OFFICIAL_FALLBACK_HOST = "childabduction.state.gov"
OFFICIAL_HOSTS = {PRIMARY_HOST, OFFICIAL_FALLBACK_HOST}
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/"
SCRAPERAPI_TIMEOUT = 75
SCRAPERAPI_MAX_COST = "10"
SCRAPERAPI_DEFAULT_MAX_REQUESTS = 4
MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july",
               "august", "september", "october", "november", "december"]
DownloadResult = namedtuple("DownloadResult", "body status failure")

# Row-label prefix (normalized: lowercased, spaces/hyphens removed) -> USCategory raw value.
CATEGORY_MAP = {
    "1st": "EB1", "2nd": "EB2", "3rd": "EB3", "4th": "EB4",
    "f1": "F1", "f2a": "F2A", "f2b": "F2B", "f3": "F3", "f4": "F4",
}
# Row labels we intentionally skip (sub-categories the app's model doesn't track):
# the EB-3 "Other Workers" and EB-4 "Certain Religious Workers" sub-lines, and the
# post-2022 EB-5 set-aside carve-outs (Rural / High Unemployment / Infrastructure).
SKIP_ROWS = {"otherworkers", "certainreligiousworkers", "5thsetaside"}
EXPECTED_CATEGORIES = {
    "F1", "F2A", "F2B", "F3", "F4",
    "EB1", "EB2", "EB3", "EB4", "EB5",
}


def resolve_category(label, section):
    """Map a normalized row label to a USCategory raw value, or None to skip.

    Resolution is scoped to the table's `section` ("family" or "employment") because the
    ordinal labels are reused: pre-2015 family tables label their rows "1st/2A/2B/3rd/4th"
    — the same ordinals employment uses — so "1st" means F1 in a family table but EB1 in
    an employment table. Modern family tables already use "F1/F2A/…", handled here too.

    EB-5's label has changed repeatedly ("5th", "5th Targeted Employment Areas/Regional
    Centers", "5th Non-Regional Center"/"5th Regional Center", "5th Unreserved"). The
    set-aside carve-outs are removed by SKIP_ROWS first, so any remaining "5th…" row is
    the general EB-5 line, whatever its era's wording; per-key dedup keeps the first.
    """
    if any(label.startswith(skip) for skip in SKIP_ROWS):
        return None

    if section == "employment":
        if label.startswith("5th"):
            return "EB5"
        for prefix, category in (("1st", "EB1"), ("2nd", "EB2"),
                                 ("3rd", "EB3"), ("4th", "EB4")):
            if label.startswith(prefix):
                return category
        return None

    # family — check the more specific 2A/2B before the bare ordinals.
    for prefix, category in (("f2a", "F2A"), ("2a", "F2A"),
                             ("f2b", "F2B"), ("2b", "F2B"),
                             ("f1", "F1"), ("1st", "F1"),
                             ("f3", "F3"), ("3rd", "F3"),
                             ("f4", "F4"), ("4th", "F4")):
        if label.startswith(prefix):
            return category
    return None

# Full-name column-header substrings (normalized) -> ChargeabilityArea. The worldwide
# needle is the tolerant "allcharg" because the source text varies ("All Chargeability
# Areas", typos like "All Chargability Area"); it still excludes the Diversity Visa table's
# "All DV Chargeability Areas" (which normalizes to "alldvcharg…", not "allcharg…").
AREA_MAP = {
    "china": "china",
    "india": "india",
    "mexico": "mexico",
    "philippines": "philippines",
}
# Some 2005-2006 bulletins abbreviate the chargeability columns to two letters. These are
# matched EXACTLY (not as substrings) so "ch" doesn't collide with "chargeability".
AREA_ABBREVIATIONS = {"ch": "china", "in": "india", "me": "mexico", "ph": "philippines"}


def resolve_area(cell):
    """Map a chargeability column header to an area, or None. Worldwide is checked first;
    then exact two-letter abbreviations; then full-name substrings."""
    norm = normalize(cell)
    if "allcharg" in norm:
        return "worldwide"
    if norm in AREA_ABBREVIATIONS:
        return AREA_ABBREVIATIONS[norm]
    for needle, area in AREA_MAP.items():
        if needle in norm:
            return area
    return None


def normalize(text):
    return re.sub(r"[\s\-]+", "", text).lower()


def strip_tags(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", htmllib.unescape(text)).strip()


def parse_cutoff_date(token):
    """`08JUL15` -> `2015-07-08`; `C` / `U` pass through. Returns None if unparseable."""
    token = token.strip().upper()
    if token in ("C", "U"):
        return token
    # Day is 1-2 digits: some historical cells carry source typos like "2OCT91".
    m = re.fullmatch(r"(\d{1,2})([A-Z]{3})(\d{2})", token)
    if not m:
        return None
    day, mon_abbr, yy = m.group(1), m.group(2).title(), int(m.group(3))
    try:
        month = MONTH_NAMES.index({
            "Jan": "january", "Feb": "february", "Mar": "march", "Apr": "april",
            "May": "may", "Jun": "june", "Jul": "july", "Aug": "august",
            "Sep": "september", "Oct": "october", "Nov": "november", "Dec": "december",
        }[mon_abbr]) + 1
    except KeyError:
        return None
    # Two-digit year: cutoffs range ~1988..present. Years <= 40 are 2000s, else 1900s.
    year = 2000 + yy if yy <= 40 else 1900 + yy
    return f"{year:04d}-{month:02d}-{int(day):02d}"


def fiscal_year_dir(year, month):
    """State Dept organizes bulletins by federal fiscal year (Oct starts the next FY)."""
    return year + 1 if month >= 10 else year


def bulletin_urls(year, month, host=PRIMARY_HOST):
    """Candidate URLs for one bulletin. The State Dept. slug is inconsistent: most months
    are 'visa-bulletin-for-{month}-{year}', but some (e.g. October 2012) drop the 'for-'."""
    fy = fiscal_year_dir(year, month)
    name = MONTH_NAMES[month - 1]
    base = (f"https://{host}/content/travel/en/legal/visa-law0/visa-bulletin/"
            f"{fy}")
    return [
        f"{base}/visa-bulletin-for-{name}-{year}.html",
        f"{base}/visa-bulletin-{name}-{year}.html",
    ]


def _iter_tables(raw):
    for table in re.findall(r"<table[^>]*>.*?</table>", raw, flags=re.S | re.I):
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, flags=re.S | re.I)
        parsed = [
            [strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row,
                                               flags=re.S | re.I)]
            for row in rows
        ]
        yield table, parsed


def _table_type(raw, table_html):
    """Classify a preference table as Final Action (Table A) or Dates for Filing (Table B)
    from the descriptive text preceding it. Pre-Oct-2015 bulletins predate the "final
    action" wording (they say "cut-off date") and have no Dates-for-Filing tables, so an
    unlabeled preference table defaults to Final Action."""
    index = raw.find(table_html)
    preceding = strip_tags(raw[max(0, index - 900):index]).lower()
    # Take the classification nearest the table.
    filing = preceding.rfind("for filing")
    final = preceding.rfind("final action")
    return "datesForFiling" if filing > final else "finalAction"


def _detect_section(rows):
    """A preference table's section, from any row labelling it family- or
    employment-sponsored. Handles the labels' history: modern tables carry "Family-
    Sponsored"/"Employment- based" in the header cell; 2005-2008 use "Family"/
    "Employment-based"; pre-2005 put the label in a sub-row above the category rows."""
    for row in rows:
        if not row:
            continue
        first = normalize(row[0])
        if first == "family" or first.startswith("familysponsored"):
            return "family"
        if first.startswith("employment"):
            return "employment"
    return None


def _area_header(rows):
    """Locate the row that names the chargeability columns and map column index -> area.
    The columns vary by era (China/India only appear once oversubscribed), so this is
    driven entirely by the header text, not a fixed layout."""
    for index, row in enumerate(rows):
        if any("allcharg" in normalize(cell) for cell in row):
            col_area = {}
            for col_index, cell in enumerate(row):
                if col_index == 0:
                    continue  # column 0 is the category-label column
                area = resolve_area(cell)
                if area is not None:
                    col_area[col_index] = area
            if col_area:
                return index, col_area
    return None, None


def parse_bulletin(raw, month_key):
    """Returns canonical entry dicts for one bulletin month, across the bulletin's
    format history (2001 -> present)."""
    entries = []
    seen = set()
    for table_html, rows in _iter_tables(raw):
        if not rows:
            continue
        header_index, col_area = _area_header(rows)
        if col_area is None:
            continue  # not a preference table (e.g. the Diversity Visa table)
        section = _detect_section(rows)
        if section is None:
            continue
        table = _table_type(raw, table_html)

        for row in rows[header_index:]:
            if not row:
                continue
            category = resolve_category(normalize(row[0]), section)
            if category is None:
                continue
            for col_index, area in col_area.items():
                if col_index >= len(row):
                    continue
                status = parse_cutoff_date(row[col_index])
                if status is None:
                    continue
                key = (table, category, area)
                if key in seen:
                    continue
                seen.add(key)
                entries.append({
                    "bulletinMonth": month_key,
                    "table": table,
                    "category": category,
                    "area": area,
                    "status": status,
                    "provenance": "official",
                })
    return entries


def _validation_failure(raw, month_key):
    """Why a downloaded page is unsafe to label/cache as `month_key`, or None.

    A nonempty parse alone is insufficient: parse_bulletin stamps the caller-supplied
    month onto every row, so a redirect to an older bulletin would otherwise silently
    relabel valid-but-wrong data. Requiring near-complete worldwide category coverage
    also rejects truncated HTTP 200 responses while tolerating March 2003's one malformed
    source row.
    """
    year, month = (int(part) for part in month_key.split("-"))
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.S | re.I)
    if title_match is None:
        return "missing HTML title"
    title = re.sub(r"\s+", " ", strip_tags(title_match.group(1))).strip().lower()
    title_tokens = set(re.findall(r"[a-z0-9]+", title))
    expected_tokens = {"visa", "bulletin", MONTH_NAMES[month - 1], str(year)}
    if not expected_tokens.issubset(title_tokens):
        return f"page title {title!r} does not identify {month_key}"

    entries = parse_bulletin(raw, month_key)
    required_tables = ["finalAction"]
    if (year, month) >= (2015, 10):
        required_tables.append("datesForFiling")
    for table in required_tables:
        categories = {
            entry["category"] for entry in entries
            if entry["table"] == table and entry["area"] == "worldwide"
        }
        if len(categories & EXPECTED_CATEGORIES) < 9:
            missing = ", ".join(sorted(EXPECTED_CATEGORIES - categories))
            return f"incomplete {table} worldwide categories (missing: {missing})"
    return None


def _request(request, display_url, retries=3, timeout=30):
    """Run one HTTP route and retain its final status for fallback decisions."""
    failure = "unknown"
    status = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                if response.status != 200:
                    failure = f"HTTP {response.status}"
                    if response.status in (403, 404) or attempt == retries - 1:
                        break
                    time.sleep(2 * (attempt + 1))
                    continue
                body = response.read().decode("utf-8", errors="replace")
                return DownloadResult(body, 200, None)
        except urllib.error.HTTPError as error:
            status = error.code
            failure = f"HTTP {error.code}"
            error.close()
            # A Cloudflare block is deterministic for this egress identity; retrying it
            # only delays the useful route. A 404 is definitive for this candidate slug.
            if error.code in (403, 404) or attempt == retries - 1:
                break
            time.sleep(2 * (attempt + 1))
        except Exception as error:  # noqa: BLE001 - network best effort
            failure = type(error).__name__
            if attempt == retries - 1:
                break
            time.sleep(2 * (attempt + 1))
    print(f"  ! {failure} {display_url}", file=sys.stderr)
    return DownloadResult(None, status, failure)


def _download_result(url, retries=3):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return _request(request, url, retries=retries)


def _download(url, retries=3):
    """Backward-compatible body-only wrapper for one candidate URL."""
    return _download_result(url, retries=retries).body


def _download_via_scraperapi(url, api_key):
    """Fetch one validated State URL through ScraperAPI without putting its key in a URL."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS:
        raise ValueError(f"refusing to proxy non-State URL: {url}")

    proxy_url = SCRAPERAPI_ENDPOINT + "?" + urllib.parse.urlencode({"url": url})
    request = urllib.request.Request(proxy_url, headers={
        "x-sapi-api_key": api_key,
        "x-sapi-premium": "true",
        "x-sapi-max_cost": SCRAPERAPI_MAX_COST,
    })
    print(f"  ↪ ScraperAPI fallback for {url}", file=sys.stderr)
    return _request(
        request,
        f"ScraperAPI for {url}",
        retries=1,  # ScraperAPI performs its own retries for up to about 70 seconds.
        timeout=SCRAPERAPI_TIMEOUT,
    )


def _looks_like_bot_block(result):
    if result.body is None:
        return False
    lowered = result.body.lower()
    return (
        "attention required! | cloudflare" in lowered
        or "cf-chl-" in lowered
        or ("captcha" in lowered and not parse_bulletin(result.body, "0000-00"))
    )


class ScraperAPIFallback:
    """A shared, run-bounded paid fallback with a credential-failure circuit breaker."""

    def __init__(self, api_key, max_requests=SCRAPERAPI_DEFAULT_MAX_REQUESTS):
        self.api_key = api_key.strip()
        self.remaining = max(0, max_requests)
        self._limit_logged = False

    @classmethod
    def from_environment(cls):
        raw_limit = os.environ.get(
            "SCRAPERAPI_MAX_REQUESTS", str(SCRAPERAPI_DEFAULT_MAX_REQUESTS))
        try:
            max_requests = int(raw_limit)
        except ValueError:
            print(f"  ! invalid SCRAPERAPI_MAX_REQUESTS={raw_limit!r}; using "
                  f"{SCRAPERAPI_DEFAULT_MAX_REQUESTS}", file=sys.stderr)
            max_requests = SCRAPERAPI_DEFAULT_MAX_REQUESTS
        return cls(os.environ.get("SCRAPERAPI_KEY", ""), max_requests)

    def fetch(self, url):
        if not self.api_key:
            return None
        if self.remaining == 0:
            if not self._limit_logged:
                print("  ! ScraperAPI per-run request limit reached; paid fallback "
                      "disabled for remaining pages", file=sys.stderr)
                self._limit_logged = True
            return None

        self.remaining -= 1
        result = _download_via_scraperapi(url, self.api_key)
        if result.status in (401, 403):
            # ScraperAPI uses these for invalid/exhausted credentials or max-cost
            # refusal. Another slug cannot repair account configuration this run.
            self.api_key = ""
        return result


def _validated_body(result, source_url, month_key, cache_file):
    """Accept and cache only HTML that actually contains Visa Bulletin data."""
    if result.body is None:
        return None
    failure = _validation_failure(result.body, month_key)
    if failure is not None:
        print(f"  ! invalid Visa Bulletin HTTP 200 from {source_url}: {failure}",
              file=sys.stderr)
        return None
    if cache_file:
        cache_file.write_text(result.body, encoding="utf-8")
    return result.body


def fetch_page(year, month, cache_dir, retries=3, scraperapi=None):
    cache_file = cache_dir / f"{year:04d}-{month:02d}.html" if cache_dir else None
    month_key = f"{year:04d}-{month:02d}"
    if cache_file and cache_file.exists():
        cached = cache_file.read_text(encoding="utf-8", errors="replace")
        failure = _validation_failure(cached, month_key)
        if failure is None:
            return cached
        print(f"  ! ignoring invalid cached bulletin {cache_file}: {failure}",
              file=sys.stderr)

    scraperapi = scraperapi or ScraperAPIFallback.from_environment()
    primary_urls = bulletin_urls(year, month, PRIMARY_HOST)
    official_fallback_urls = bulletin_urls(year, month, OFFICIAL_FALLBACK_HOST)

    for primary_url, official_fallback_url in zip(primary_urls, official_fallback_urls):
        primary = _download_result(primary_url, retries=retries)
        bot_blocked = primary.status == 403 or _looks_like_bot_block(primary)
        body = _validated_body(primary, primary_url, month_key, cache_file)
        if body is not None:
            return body
        if primary.status == 404:
            continue  # The canonical host reached the origin: try the alternate slug.

        official_fallback = _download_result(
            official_fallback_url, retries=retries)
        bot_blocked = (
            bot_blocked
            or official_fallback.status == 403
            or _looks_like_bot_block(official_fallback)
        )
        body = _validated_body(
            official_fallback, official_fallback_url, month_key, cache_file)
        if body is not None:
            print(f"  ↪ official-host fallback for {primary_url}", file=sys.stderr)
            return body
        if official_fallback.status == 404:
            # Both hostnames serve the same State AEM content. This is an authoritative
            # "not published under this slug", so do not buy the same 404 from a proxy.
            continue

        if bot_blocked:
            proxied = scraperapi.fetch(primary_url)
        else:
            proxied = None
        if proxied is not None:
            body = _validated_body(proxied, primary_url, month_key, cache_file)
            if body is not None:
                return body
    return None


def month_range(start, end):
    (sy, sm), (ey, em) = start, end
    y, m = sy, sm
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def parse_month_arg(value):
    year, month = value.split("-")
    return int(year), int(month)


# ------------------------------------------------------------------ self-test

SELF_TEST_HTML = """
<p>are authorized for issuance only for applicants whose priority date is earlier than the
final action date listed below.</p>
<table>
<tr><td>Employment- based</td><td>All Chargeability Areas Except Those Listed</td>
<td>CHINA- mainland born</td><td>INDIA</td><td>MEXICO</td><td>PHILIPPINES</td></tr>
<tr><td>1st</td><td>C</td><td>01SEP22</td><td>01MAR21</td><td>C</td><td>C</td></tr>
<tr><td>2nd</td><td>15JAN23</td><td>01FEB20</td><td>15APR12</td><td>15JAN23</td><td>15JAN23</td></tr>
<tr><td>Other Workers</td><td>08OCT20</td><td>01JAN17</td><td>22AUG12</td><td>08OCT20</td><td>01MAY20</td></tr>
<tr><td>5th Non-Regional Center (C5 and T5)</td><td>C</td><td>15DEC15</td><td>01DEC20</td><td>C</td><td>C</td></tr>
<tr><td>5th Regional Center (I5 and R5)</td><td>C</td><td>08JAN16</td><td>08JAN20</td><td>C</td><td>C</td></tr>
</table>
<p>may be used (in lieu of the chart in paragraph 5.A.) this month for filing applications.</p>
<table>
<tr><td>Employment- based</td><td>All Chargeability Areas Except Those Listed</td>
<td>CHINA- mainland born</td><td>INDIA</td><td>MEXICO</td><td>PHILIPPINES</td></tr>
<tr><td>2nd</td><td>01AUG23</td><td>01JUN20</td><td>01JAN13</td><td>01AUG23</td><td>01AUG23</td></tr>
</table>
"""

# Pre-Oct-2015 format: "cut-off date" wording, no Dates-for-Filing table, and the older
# single EB-5 label. Must still classify as Final Action and resolve EB-5.
SELF_TEST_HTML_LEGACY = """
<p>Numbers are available only for applicants whose priority date is earlier than the
cut-off date listed below.</p>
<table>
<tr><td>Family- Sponsored</td><td>All Chargeability Areas Except Those Listed</td>
<td>CHINA- mainland born</td><td>INDIA</td><td>MEXICO</td><td>PHILIPPINES</td></tr>
<tr><td>1st</td><td>01JAN06</td><td>01JAN06</td><td>01JAN06</td><td>15JUL93</td><td>01MAR97</td></tr>
<tr><td>2A</td><td>01MAY10</td><td>01MAY10</td><td>01MAY10</td><td>15APR10</td><td>01MAY10</td></tr>
<tr><td>2B</td><td>01OCT03</td><td>01OCT03</td><td>01OCT03</td><td>15JAN93</td><td>01JUN99</td></tr>
<tr><td>3rd</td><td>08JUL01</td><td>08JUL01</td><td>08JUL01</td><td>01MAY93</td><td>08JUL92</td></tr>
<tr><td>4th</td><td>01JAN01</td><td>01JAN01</td><td>15NOV00</td><td>15SEP96</td><td>01FEB89</td></tr>
</table>
<p>Numbers are available only for applicants whose priority date is earlier than the
cut-off date listed below.</p>
<table>
<tr><td>Employment- Based</td><td>All Chargeability Areas Except Those Listed</td>
<td>CHINA - mainland born</td><td>INDIA</td><td>MEXICO</td><td>PHILIPPINES</td></tr>
<tr><td>1st</td><td>C</td><td>C</td><td>C</td><td>C</td><td>C</td></tr>
<tr><td>3rd</td><td>01OCT08</td><td>01OCT08</td><td>22SEP03</td><td>01OCT08</td><td>01OCT08</td></tr>
<tr><td>Other Workers</td><td>01OCT08</td><td>22JUL03</td><td>22SEP03</td><td>01OCT08</td><td>01OCT08</td></tr>
<tr><td>5th Targeted Employment Areas/ Regional Centers and Pilot Programs</td>
<td>C</td><td>C</td><td>C</td><td>C</td><td>C</td></tr>
</table>
"""

# Pre-2004 format: empty table header cell, the section label ("Family"/"Employment- Based")
# sits in its own sub-row, and China is not yet a separate column.
SELF_TEST_HTML_PRE2004 = """
<p>applicants whose priority date is earlier than the cut-off date listed below.</p>
<table>
<tr><td></td><td>All Chargeability Areas Except Those Listed</td><td>INDIA</td>
<td>MEXICO</td><td>PHILIPPINES</td></tr>
<tr><td>Family</td><td></td><td></td><td></td><td></td></tr>
<tr><td>1st</td><td>15JAN99</td><td>15JAN99</td><td>01APR94</td><td>01MAY90</td></tr>
<tr><td>2A</td><td>01MAR97</td><td>01MAR97</td><td>01FEB97</td><td>01MAR97</td></tr>
<tr><td>4th</td><td>01JAN91</td><td>22NOV89</td><td>01JAN91</td><td>01FEB80</td></tr>
</table>
<table>
<tr><td></td><td>All Chargeability Areas Except Those Listed</td><td>INDIA</td>
<td>MEXICO</td><td>PHILIPPINES</td></tr>
<tr><td>Employment- Based</td><td></td><td></td><td></td><td></td></tr>
<tr><td>1st</td><td>C</td><td>C</td><td>C</td><td>C</td></tr>
<tr><td>3rd</td><td>01OCT01</td><td>01MAY00</td><td>01OCT01</td><td>01OCT01</td></tr>
</table>
"""


def self_test():
    entries = parse_bulletin(SELF_TEST_HTML, "2024-06")
    by_key = {(e["table"], e["category"], e["area"]): e["status"] for e in entries}
    checks = {
        ("finalAction", "EB1", "worldwide"): "C",
        ("finalAction", "EB1", "china"): "2022-09-01",
        ("finalAction", "EB2", "india"): "2012-04-15",
        ("finalAction", "EB5", "china"): "2015-12-15",
        ("finalAction", "EB5", "worldwide"): "C",
        ("datesForFiling", "EB2", "india"): "2013-01-01",
    }
    failures = []

    def expect(condition, message):
        if not condition:
            failures.append(message)

    for key, expected in checks.items():
        actual = by_key.get(key)
        if actual != expected:
            failures.append(f"{key}: expected {expected}, got {actual}")
    # "Other Workers" must be skipped (not mapped to EB3).
    if any(e["category"] == "EB3" for e in entries):
        failures.append("Other Workers leaked into EB3")
    if parse_cutoff_date("22MAR05") != "2005-03-22":
        failures.append("date parse 22MAR05")
    if parse_cutoff_date("01DEC99") != "1999-12-01":
        failures.append("century rollover 01DEC99")
    if parse_cutoff_date("2OCT91") != "1991-10-02":
        failures.append("single-digit day typo 2OCT91")
    if fiscal_year_dir(2023, 10) != 2024 or fiscal_year_dir(2024, 6) != 2024:
        failures.append("fiscal year dir")

    # Chargeability columns: full names, two-letter abbreviations (2005-2006), and the
    # worldwide column must not collide with the "ch" in "Chargeability".
    expect(resolve_area("All Chargeability Areas Except Those Listed") == "worldwide",
           "worldwide column")
    expect(resolve_area("CHINA-mainland born") == "china", "full China column")
    expect(resolve_area("CH") == "china" and resolve_area("IN") == "india", "abbrev CH/IN")
    expect(resolve_area("ME") == "mexico" and resolve_area("PH") == "philippines", "abbrev ME/PH")
    expect(resolve_area("Employment- based") is None, "non-area header not matched")

    # Legacy (pre-2015) format: cut-off wording defaults to Final Action; EB-5 resolves.
    legacy = parse_bulletin(SELF_TEST_HTML_LEGACY, "2013-06")
    legacy_by_key = {(e["table"], e["category"], e["area"]): e["status"] for e in legacy}
    expect(legacy_by_key.get(("finalAction", "EB3", "india")) == "2003-09-22",
           "legacy EB3 india final action")
    expect(legacy_by_key.get(("finalAction", "EB1", "worldwide")) == "C",
           "legacy EB1 employment resolved")
    expect(legacy_by_key.get(("finalAction", "EB5", "worldwide")) == "C",
           "legacy EB5 (Targeted Employment Areas) resolved")
    expect(all(e["table"] == "finalAction" for e in legacy),
           "legacy tables default to Final Action")
    expect(not any(e["category"] == "EB3" and e["status"] == "2003-07-22" for e in legacy),
           "legacy Other Workers must not leak into EB3")
    # Section-scoped resolution: legacy family "1st/2A/2B/3rd/4th" must become F-categories,
    # and must not shadow the employment ordinals of the same name.
    expect(legacy_by_key.get(("finalAction", "F1", "india")) == "2006-01-01",
           "legacy family 1st -> F1")
    expect(legacy_by_key.get(("finalAction", "F2A", "mexico")) == "2010-04-15",
           "legacy family 2A -> F2A")
    expect(legacy_by_key.get(("finalAction", "F2B", "worldwide")) == "2003-10-01",
           "legacy family 2B -> F2B")
    expect(legacy_by_key.get(("finalAction", "F4", "philippines")) == "1989-02-01",
           "legacy family 4th -> F4")
    expect(legacy_by_key.get(("finalAction", "EB1", "china")) == "C",
           "employment 1st still EB1 despite family 1st present")

    # Pre-2004 format: empty header cell, section label in a sub-row, no China column.
    pre = parse_bulletin(SELF_TEST_HTML_PRE2004, "2002-06")
    pre_by_key = {(e["table"], e["category"], e["area"]): e["status"] for e in pre}
    expect(pre_by_key.get(("finalAction", "F1", "worldwide")) == "1999-01-15",
           "pre-2004 family 1st -> F1 (label in sub-row)")
    expect(pre_by_key.get(("finalAction", "EB3", "india")) == "2000-05-01",
           "pre-2004 employment 3rd -> EB3")
    expect(pre_by_key.get(("finalAction", "EB1", "worldwide")) == "C",
           "pre-2004 employment 1st -> EB1")
    expect(all(e["area"] != "china" for e in pre),
           "pre-2004 has no China column, so no china entries")
    expect(any(e["category"] == "F4" for e in pre), "pre-2004 family 4th -> F4")

    if failures:
        print("SELF-TEST FAILED:")
        for line in failures:
            print("  -", line)
        return 1
    print(f"SELF-TEST PASSED ({len(entries)} entries parsed from fixture)")
    return 0


# ------------------------------------------------------------------------ main

def main():
    parser = argparse.ArgumentParser(description="Fetch and parse the U.S. Visa Bulletin.")
    parser.add_argument("--start", type=parse_month_arg, help="YYYY-MM inclusive")
    parser.add_argument("--end", type=parse_month_arg, help="YYYY-MM inclusive")
    parser.add_argument("--out", type=Path, help="Output JSON path")
    parser.add_argument("--cache-dir", type=Path, help="Directory to cache raw HTML pages")
    parser.add_argument("--parse-file", type=Path, help="Parse a local HTML file instead of fetching")
    parser.add_argument("--month", help="YYYY-MM for --parse-file")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between fetches")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if args.parse_file:
        raw = args.parse_file.read_text(encoding="utf-8", errors="replace")
        month_key = args.month or "0000-00"
        entries = parse_bulletin(raw, month_key)
        text = json.dumps(entries, indent=0)
        if args.out:
            args.out.write_text(text)
        print(f"{len(entries)} entries from {args.parse_file}")
        return 0

    if not (args.start and args.end and args.out):
        parser.error("provide --start, --end and --out (or --parse-file / --self-test)")

    cache_dir = args.cache_dir
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    months = list(month_range(args.start, args.end))
    entries_by_month = {}
    scraperapi = ScraperAPIFallback.from_environment()
    fetched = 0
    # Fetch newest first so the bounded paid fallback is spent on the freshness-critical
    # months during a wider cache revalidation. Flatten chronologically before writing.
    for year, month in reversed(months):
        cached = cache_dir and (cache_dir / f"{year:04d}-{month:02d}.html").exists()
        raw = fetch_page(year, month, cache_dir, scraperapi=scraperapi)
        if not cached and args.delay:
            time.sleep(args.delay)
        if raw is None:
            continue
        month_key = f"{year:04d}-{month:02d}"
        entries = parse_bulletin(raw, month_key)
        if entries:
            fetched += 1
            entries_by_month[(year, month)] = entries
            print(f"  {year}-{month:02d}: {len(entries)} entries")
        else:
            print(f"  {year}-{month:02d}: no tables parsed", file=sys.stderr)

    all_entries = [
        entry
        for year, month in months
        for entry in entries_by_month.get((year, month), [])
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(all_entries, indent=0))
    print(f"\nWrote {len(all_entries)} entries from {fetched}/{len(months)} months "
          f"to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
