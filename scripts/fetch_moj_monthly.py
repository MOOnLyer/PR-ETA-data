#!/usr/bin/env python3
"""Fetches the MOJ/ISA monthly Excel table 「地方出入国在留管理局管内別 在留資格の取得等の
受理及び処理人員」 and emits canonical japan_monthly_stats records for the 永住 procedure.

Why this exists: the e-Stat *API* database (see fetch_estat.py) has no monthly data for
2018-02 → 2020-10, but the monthly Excel releases on the MOJ statistics index
(https://www.moj.go.jp/isa/policies/statistics/toukei_ichiran_nyukan.html) cover every
month from 2007-01 onward — including that gap. This script resolves the index's month →
e-Stat datalist links, finds the table's statInfId, downloads the .xlsx, and parses it
with the standard library (zipfile + ElementTree; the files are real xlsx).

Workbook layout (stable across years): a bureau header row (札幌/仙台/東京/…, with
成田・羽田・横浜・中部・関西・神戸・那覇 as (うち) sub-columns of their parent bureau),
then one block per procedure; within the 永住 block the rows 旧受 (pending carried over),
新受 (newly received) and 既済 (decided) are taken. Sub-columns are subtracted from their
parents exactly as in fetch_estat.py, so both sources yield identical office series.

Usage:
    # Fill the e-Stat gap months into an existing dataset:
    python3 scripts/fetch_moj_monthly.py --months gap \
        --merge-into PRETA/Resources/SeedData/japan_monthly_stats.json
    # Or fetch specific months to their own file:
    python3 scripts/fetch_moj_monthly.py --months 2019-05,2019-06 --out gap.json
    python3 scripts/fetch_moj_monthly.py --self-test

Only the Python standard library is used.
"""

import argparse
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

INDEX_URL = "https://www.moj.go.jp/isa/policies/statistics/toukei_ichiran_nyukan.html"
TABLE_TITLE = "在留資格の取得等の受理及び処理人員"
# The examination (在留資格審査) monthly section is the second 月報 heading on the page.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

# e-Stat gap months (see fetch_estat.py): monthly API data missing 2018-02 → 2020-10.
GAP_MONTHS = ([f"2018-{m:02d}" for m in range(2, 13)]
              + [f"2019-{m:02d}" for m in range(1, 13)]
              + [f"2020-{m:02d}" for m in range(1, 11)])

RECHECK_MONTHS = 3   # in --months incremental, re-fetch this many trailing months

# Office name -> app office id; parents map to the (うち) sub-columns subtracted from them.
MAIN_OFFICES = {
    "札幌": "sapporo", "仙台": "sendai", "東京": "tokyo", "名古屋": "nagoya",
    "大阪": "osaka", "広島": "hiroshima", "高松": "takamatsu", "福岡": "fukuoka",
}
BRANCH_OFFICES = {"横浜": "yokohama", "神戸": "kobe", "那覇": "naha"}
PARENT_OF = {  # sub-column name -> parent bureau name
    "成田": "東京", "羽田": "東京", "横浜": "東京",
    "中部": "名古屋",
    "関西": "大阪", "神戸": "大阪",
    "那覇": "福岡",
}


def fetch(url, binary=False):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    return data if binary else data.decode("utf-8", errors="replace")


def zenkaku_to_int(text):
    return text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def month_links(index_html):
    """month 'yyyy-MM' -> e-Stat datalist URL, from the examination 月報 section."""
    strip = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()
    # Headings may wrap the text in spans (<h4><span id="a12">月報</span></h4>).
    headings = [m.start() for m in re.finditer(r"<h\d[^>]*>.{0,120}?月報.{0,40}?</h\d>",
                                               index_html, flags=re.S)]
    if not headings:
        raise RuntimeError("could not find 月報 headings on the index page")
    section = index_html[headings[-1]:]

    links = {}
    year = None
    for token in re.split(r'(<a[^>]+href="[^"]+"[^>]*>.*?</a>)', section, flags=re.S | re.I):
        anchor = re.match(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', token, flags=re.S | re.I)
        if anchor:
            # Some hrefs are wrapped across source lines — strip ALL whitespace (URLs
            # cannot contain it), or the trailing newline 404s at e-Stat.
            href = re.sub(r"\s+", "", anchor.group(1).replace("&amp;", "&"))
            text = zenkaku_to_int(strip(anchor.group(2)))
            month = re.fullmatch(r"(\d{1,2})月", text)
            if month and year and "e-stat" in href.lower():
                links[f"{year:04d}-{int(month.group(1)):02d}"] = href
        else:
            years = re.findall(r"(\d{4})年", zenkaku_to_int(token))
            if years:
                year = int(years[-1])
    return links


def month_links_verified(retries=2):
    """Fetch the index and parse the month links, retrying if the result looks truncated
    (the MOJ server occasionally serves a page variant missing older year rows)."""
    best = {}
    for _ in range(retries + 1):
        links = month_links(fetch(INDEX_URL))
        if len(links) > len(best):
            best = links
        keys = sorted(best)
        if keys:
            first, last = keys[0], keys[-1]
            span = ((int(last[:4]) - int(first[:4])) * 12
                    + int(last[5:7]) - int(first[5:7]) + 1)
            if first <= "2007-01" and len(best) == span:
                return best  # contiguous from 2007 — complete
        time.sleep(2)
    if best:
        keys = sorted(best)
        print(f"warning: index parse may be incomplete "
              f"({len(best)} months, {keys[0]}…{keys[-1]})", file=sys.stderr)
    return best


def download_table_xlsx(sid):
    """Download a table's Excel from e-Stat. Releases up to ~2020 serve it as
    fileKind=0; later releases moved to fileKind=4 (fileKind=0 404s there)."""
    import urllib.error
    base = f"https://www.e-stat.go.jp/stat-search/file-download?statInfId={sid}"
    for kind in (0, 4):
        try:
            return fetch(f"{base}&fileKind={kind}", binary=True)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    raise RuntimeError(f"statInfId {sid}: no Excel at fileKind 0 or 4")


def stat_inf_id(datalist_html):
    """The statInfId of the flow table on a monthly datalist page."""
    for m in re.finditer(r"statInfId=(\d+)", datalist_html):
        context = datalist_html[max(0, m.start() - 2500):m.start()]
        if TABLE_TITLE in re.sub(r"<[^>]+>", "", context):
            return m.group(1)
    raise RuntimeError(f"no statInfId with title {TABLE_TITLE} on datalist page")


OLE2_MAGIC = b"\xd0\xcf\x11\xe0"


def column_letter(index):
    """0-based column index -> spreadsheet letters (0 = A, 26 = AA)."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def load_xls_rows(data):
    """Legacy BIFF .xls (releases before ~2012-07) -> same shape as load_xlsx_rows.
    Requires xlrd (pure Python); only the old files need it."""
    try:
        import xlrd  # noqa: PLC0415 — optional, deferred
    except ImportError as error:
        raise RuntimeError(
            "legacy .xls workbook — install xlrd (python3 -m pip install xlrd)"
        ) from error
    book = xlrd.open_workbook(file_contents=data)
    sheet = book.sheet_by_index(0)
    rows = {}
    for r in range(sheet.nrows):
        cells = {}
        for c in range(sheet.ncols):
            value = sheet.cell_value(r, c)
            if value in ("", None):
                continue
            if isinstance(value, float) and value == int(value):
                value = int(value)  # xlsx path yields integer-looking strings
            cells[column_letter(c)] = str(value)
        if cells:
            rows[r + 1] = cells
    return rows


def load_xlsx_rows(data):
    """Workbook bytes -> {row_number: {column_letter: value}} for sheet 1.
    Dispatches on the container: zip = real xlsx (stdlib), OLE2 = legacy xls (xlrd)."""
    if data[:4] == OLE2_MAGIC:
        return load_xls_rows(data)
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    archive = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
        for si in ET.fromstring(archive.read("xl/sharedStrings.xml")).iter(ns + "si"):
            shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
    rows = {}
    sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    for row in sheet.iter(ns + "row"):
        cells = {}
        for cell in row.iter(ns + "c"):
            value = cell.find(ns + "v")
            if value is None:
                continue
            text = shared[int(value.text)] if cell.get("t") == "s" else value.text
            column = re.match(r"([A-Z]+)", cell.get("r")).group(1)
            cells[column] = text
        rows[int(row.get("r"))] = cells
    return rows


def normalize_label(text):
    """Collapse the workbook's decorative spacing/furigana: '受　　　理' -> '受理'."""
    return re.sub(r"[\s　]| |ハネダ", "", text or "")


def extract_records(rows, month):
    """Parse a loaded workbook into canonical records for `month` ('yyyy-MM')."""
    # 1. Bureau header row: the one naming 札幌 and 東京. The 2022+ layout splits the
    # header over two rows (main bureaus, then 支局/空港 branches below), so merge the
    # row below too — header-row names win where both rows fill the same column (the
    # older layouts put a bare 空港 fragment there). Airport names may arrive merged
    # ("成田空港"); strip the suffix so they match the nesting map.
    header_row = None
    for r in sorted(rows):
        names = {normalize_label(v) for v in rows[r].values()}
        if "札幌" in names and "東京" in names:
            header_row = r
            break
    if header_row is None:
        raise RuntimeError(f"{month}: bureau header row not found")

    def clean(value):
        name = normalize_label(value)
        return name[:-2] if name.endswith("空港") and len(name) > 2 else name

    column_names = {col: clean(v) for col, v in rows.get(header_row + 1, {}).items()}
    column_names.update({col: clean(v) for col, v in rows[header_row].items()})

    # 2. 永住 block: section label in column A, then 旧受 / 新受 / 既済 rows.
    section_row = None
    for r in sorted(rows):
        if r <= header_row:
            continue
        if normalize_label(rows[r].get("A")) == "永住":
            section_row = r
            break
    if section_row is None:
        raise RuntimeError(f"{month}: 永住 section not found")

    role_rows = {}
    for r in range(section_row, min(section_row + 10, max(rows) + 1)):
        # Role labels sit in column B/C in the 2007-2021 layouts and in column A from
        # 2022 (where data starts at B) — match whichever of A/B/C carries the label.
        candidates = {normalize_label(rows.get(r, {}).get(col)) for col in ("A", "B", "C")}
        if "旧受" in candidates:
            role_rows["pending"] = r
        elif "新受" in candidates:
            role_rows["received"] = r
        elif "既済" in candidates:
            role_rows["decided"] = r
    missing = {"pending", "received", "decided"} - set(role_rows)
    if missing:
        raise RuntimeError(f"{month}: rows missing in 永住 block: {missing}")

    # 許可 / 不許可 breakdown of 既済, in the rows immediately below it (looking only
    # there keeps the scan from bleeding into the next procedure's block). Optional —
    # absent in some early layouts, in which case granted/denied are simply omitted.
    for r in range(role_rows["decided"] + 1, role_rows["decided"] + 4):
        candidates = {normalize_label(rows.get(r, {}).get(col)) for col in ("A", "B", "C")}
        if "許可" in candidates and "granted" not in role_rows:
            role_rows["granted"] = r
        elif "不許可" in candidates and "denied" not in role_rows:
            role_rows["denied"] = r

    def cell(row, column):
        raw = rows.get(row, {}).get(column)
        if raw is None:
            return None
        text = str(raw).replace(",", "").strip()
        try:
            return int(float(text))
        except ValueError:
            return None

    # 3. Raw counts per bureau name, then subtract (うち) sub-columns from parents.
    counts = {}  # bureau display name -> {role: value}
    for column, name in column_names.items():
        if name in MAIN_OFFICES or name in BRANCH_OFFICES or name in PARENT_OF:
            counts[name] = {role: cell(r, column) for role, r in role_rows.items()}

    records = []
    for name, office in list(MAIN_OFFICES.items()) + list(BRANCH_OFFICES.items()):
        if name not in counts:
            raise RuntimeError(f"{month}: bureau column {name} not found")
        values = {}
        for role in ("received", "decided", "pending", "granted", "denied"):
            base = counts[name].get(role)
            if base is None:
                values[role] = None
                continue
            if name in MAIN_OFFICES:
                for child, parent in PARENT_OF.items():
                    if parent == name and child in counts:
                        base -= counts[child].get(role) or 0
            values[role] = max(base, 0)
        record = {
            "month": month,
            "office": office,
            "received": values["received"],
            "decided": values["decided"],
            "pending": values["pending"],
            "provenance": "official",
        }
        # Optional 許可/不許可 breakdown — omitted when the workbook lacks the rows.
        for role in ("granted", "denied"):
            if values[role] is not None:
                record[role] = values[role]
        records.append(record)
    return records


def verify_month(rows, month):
    """Cross-check the workbook's era-dated caption (（令和元年６月）) against `month`."""
    year, mon = int(month[:4]), int(month[5:7])
    for r in sorted(rows)[:6]:
        for value in rows[r].values():
            text = zenkaku_to_int(str(value))
            m = re.search(r"（(令和|平成)(元|\d+)年(\d+)月）", text)
            if m:
                era, n, em = m.group(1), m.group(2), int(m.group(3))
                base = 2018 if era == "令和" else 1988
                ey = base + (1 if n == "元" else int(n))
                if (ey, em) != (year, mon):
                    raise RuntimeError(f"workbook says {ey}-{em:02d}, expected {month}")
                return True
    return False  # caption not found; not fatal


# ------------------------------------------------------------------ self-test

def self_test():
    rows = {
        3: {"A": "５　地方出入国在留管理局管内"},
        4: {"R": "（令和元年６月）"},
        5: {"A": "申請の種類", "D": "総数", "H": "（うち）", "J": "（うち）"},
        6: {"E": "札幌", "F": "仙台", "G": "東京", "H": "成田", "I": "羽田ハネダ",
            "J": "横浜", "K": "名古屋", "L": "中部", "M": "大阪", "N": "関西",
            "O": "神戸", "P": "広島", "Q": "高　松", "R": "福岡", "S": "那覇"},
        20: {"A": "期　間　更　新"},
        21: {"B": "受　　理", "G": "99999"},
        59: {"A": "永　　住"},
        60: {"B": "受　　　理", "G": "25792"},
        61: {"C": "旧　受", "E": "128", "G": "20913", "H": "0", "I": "0", "J": "3079",
             "K": "5517", "L": "0", "M": "2074", "N": "0", "O": "410", "P": "376",
             "Q": "156", "R": "781", "S": "107", "F": "298"},
        62: {"C": "新　受", "E": "40", "G": "4879", "H": "0", "I": "0", "J": "787",
             "K": "1509", "L": "0", "M": "1099", "N": "0", "O": "255", "P": "191",
             "Q": "39", "R": "244", "S": "44", "F": "64"},
        63: {"B": "既　済", "E": "30", "G": "2882", "H": "0", "I": "0", "J": "584",
             "K": "964", "L": "0", "M": "420", "N": "0", "O": "66", "P": "123",
             "Q": "35", "R": "173", "S": "8", "F": "47"},
        64: {"C": "許　可", "G": "1388", "H": "0", "I": "0", "J": "300"},
        65: {"C": "不　許　可", "G": "1200", "H": "0", "I": "0", "J": "150"},
    }
    failures = []

    def expect(cond, msg):
        if not cond:
            failures.append(msg)

    records = extract_records(rows, "2019-06")
    by = {r["office"]: r for r in records}
    expect(len(records) == 11, f"expected 11 offices, got {len(records)}")
    # Tokyo residual: 新受 4879 − 成田 0 − 羽田 0 − 横浜 787 = 4092.
    expect(by["tokyo"]["received"] == 4092, f"tokyo received: {by['tokyo']}")
    expect(by["tokyo"]["pending"] == 20913 - 3079, "tokyo pending residual")
    expect(by["tokyo"]["decided"] == 2882 - 584, "tokyo decided residual")
    expect(by["yokohama"]["received"] == 787, "yokohama standalone")
    expect(by["osaka"]["received"] == 1099 - 255, "osaka minus kobe/kansai")
    expect(by["kobe"]["received"] == 255, "kobe standalone")
    expect(by["fukuoka"]["received"] == 244 - 44, "fukuoka minus naha")
    expect(by["takamatsu"]["received"] == 39, "takamatsu (spaced header)")
    # 許可/不許可 residuals: Tokyo 1388 − Yokohama 300 = 1088; 1200 − 150 = 1050.
    expect(by["tokyo"]["granted"] == 1088, f"tokyo granted: {by['tokyo']}")
    expect(by["tokyo"]["denied"] == 1050, "tokyo denied residual")
    expect(by["yokohama"]["granted"] == 300, "yokohama granted standalone")
    # Offices without 許可 cells in the fixture omit the keys rather than storing 0.
    expect("granted" not in by["osaka"], "absent granted omitted")
    # The 期間更新 block above must not have been picked up.
    expect(by["tokyo"]["received"] != 99999, "wrong section")
    expect(verify_month(rows, "2019-06"), "era caption check")
    try:
        verify_month(rows, "2020-01")
        failures.append("era mismatch not caught")
    except RuntimeError:
        pass

    # 2022+ layout: role labels move to column A, bureau columns shift left (B = 総数),
    # and the header splits over two rows — mains, then branches with 空港 suffixes.
    rows_2022 = {
        2: {"Q": "（令和４年１月）"},
        4: {"C": "札幌", "D": "仙台", "E": "東京", "I": "名古屋", "K": "大阪",
            "N": "広島", "O": "高松", "P": "福岡"},
        5: {"F": "成田\n空港", "G": "羽田\n空港", "H": "横浜", "J": "中部\n空港",
            "L": "関西\n空港", "M": "神戸", "Q": "那覇"},
        51: {"A": "永住"},
        52: {"A": "受理", "E": "17027"},
        53: {"A": "旧受", "C": "177", "D": "282", "E": "14813", "F": "0", "G": "0",
             "H": "2094", "I": "3319", "J": "0", "K": "3063", "L": "0", "M": "268",
             "N": "247", "O": "106", "P": "707", "Q": "89"},
        54: {"A": "新受", "C": "31", "D": "43", "E": "2214", "F": "0", "G": "0",
             "H": "324", "I": "859", "J": "0", "K": "477", "L": "0", "M": "72",
             "N": "101", "O": "29", "P": "134", "Q": "26"},
        55: {"A": "既済", "C": "26", "D": "48", "E": "2940", "F": "0", "G": "0",
             "H": "518", "I": "969", "J": "0", "K": "752", "L": "0", "M": "134",
             "N": "135", "O": "30", "P": "165", "Q": "29"},
        56: {"A": "許可", "E": "1868", "F": "0", "G": "0", "H": "310"},
        57: {"A": "不許可", "E": "880", "F": "0", "G": "0", "H": "160"},
    }
    new_records = extract_records(rows_2022, "2022-01")
    new_by = {r["office"]: r for r in new_records}
    expect(len(new_records) == 11, f"2022 layout: {len(new_records)} offices")
    # Tokyo residual: 2214 − 成田 0 − 羽田 0 − 横浜 324 = 1890.
    expect(new_by["tokyo"]["received"] == 1890, f"2022 tokyo received: {new_by['tokyo']}")
    expect(new_by["tokyo"]["pending"] == 14813 - 2094, "2022 tokyo pending residual")
    expect(new_by["osaka"]["decided"] == 752 - 134, "2022 osaka minus kobe/kansai")
    expect(new_by["tokyo"]["granted"] == 1868 - 310, "2022 tokyo granted residual")
    expect(new_by["tokyo"]["denied"] == 880 - 160, "2022 tokyo denied residual")
    expect(verify_month(rows_2022, "2022-01"), "2022 era caption")

    cands = ["2026-01", "2026-02", "2026-03", "2026-04"]
    expect(months_to_fetch(cands, set()) == cands, "incremental: empty fetches all")
    expect(months_to_fetch(cands, set(cands)) == ["2026-02", "2026-03", "2026-04"],
           "incremental: all present rechecks trailing 3")
    expect(months_to_fetch(cands, {"2026-01", "2026-02"}) ==
           ["2026-02", "2026-03", "2026-04"], "incremental: new months + recheck")

    if failures:
        print("SELF-TEST FAILED:")
        for line in failures:
            print("  -", line)
        return 1
    print(f"SELF-TEST PASSED ({len(records)} office-records from fixture)")
    return 0


# ------------------------------------------------------------------------ main

def months_to_fetch(candidate_months, existing_months, recheck=RECHECK_MONTHS):
    """Which candidate 'yyyy-MM' strings to (re)download: everything not already present,
    plus the trailing `recheck` months (MOJ occasionally restates a recent month).
    `candidate_months` must be sorted ascending. Mirrors the ISA scraper's incremental
    strategy: new months are always fetched, old ones skipped, a small tail rechecked."""
    cutoff = len(candidate_months) - recheck
    return [m for i, m in enumerate(candidate_months)
            if m not in existing_months or i >= cutoff]


def main():
    parser = argparse.ArgumentParser(description="Fetch MOJ monthly 在留資格審査 Excel tables.")
    parser.add_argument("--months", default="gap",
                        help="'incremental' (index months missing from --merge-into, plus "
                             "a trailing recheck), 'all' (every month on the index), 'gap' "
                             "(e-Stat hole 2018-02→2020-10), or comma-separated yyyy-MM")
    parser.add_argument("--out", type=Path, help="Write records to this JSON file")
    parser.add_argument("--merge-into", type=Path,
                        help="Existing japan_monthly_stats.json to merge into (in place)")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.out and not args.merge_into:
        parser.error("provide --out or --merge-into (or --self-test)")

    links = month_links_verified()
    if args.months == "gap":
        wanted = GAP_MONTHS
    elif args.months == "all":
        wanted = sorted(links)
    elif args.months == "incremental":
        if not args.merge_into or not args.merge_into.exists():
            parser.error("--months incremental requires an existing --merge-into file")
        existing_months = {r["month"] for r in json.loads(args.merge_into.read_text())}
        wanted = months_to_fetch(sorted(links), existing_months)
    else:
        wanted = args.months.split(",")
    missing = [m for m in wanted if m not in links]
    if missing:
        print(f"warning: no index link for {missing}", file=sys.stderr)

    records = []
    for month in wanted:
        url = links.get(month)
        if not url:
            continue
        try:
            datalist = fetch(url)
            sid = stat_inf_id(datalist)
            xlsx = download_table_xlsx(sid)
            rows = load_xlsx_rows(xlsx)
            verify_month(rows, month)
            month_records = extract_records(rows, month)
            records.extend(month_records)
            print(f"  {month}: {len(month_records)} offices (statInfId {sid})")
        except Exception as error:  # noqa: BLE001 — report and continue
            print(f"  ! {month}: {error}", file=sys.stderr)
        time.sleep(args.delay)

    if args.merge_into:
        existing = json.loads(args.merge_into.read_text())
        combined = {(r["month"], r["office"]): r for r in existing}
        added = 0
        for record in records:
            key = (record["month"], record["office"])
            if key not in combined:
                added += 1
            combined[key] = record
        merged = sorted(combined.values(), key=lambda r: (r["month"], r["office"]))
        args.merge_into.write_text(json.dumps(merged, indent=0))
        months = sorted({r["month"] for r in merged})
        print(f"Merged {len(records)} records ({added} new) → {args.merge_into} "
              f"[{months[0]} → {months[-1]}, {len(merged)} records]")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(records, indent=0))
        print(f"Wrote {len(records)} records to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
