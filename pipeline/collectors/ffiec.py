"""
ffiec.py - loads FFIEC Call Report bulk files into bank_stats: each bank's commercial real estate
(CRE) loans, past-due and nonaccrual amounts, and whether its nonaccruals keep rising.

In plain English: every bank files a quarterly "call report" with the regulators. The FFIEC
publishes them as one zip per quarter (tab-delimited text files). This collector reads each zip and,
for every bank, adds up the three CRE groups (construction, multifamily, nonfarm nonresidential):
    cre_total             = construction + multifamily + nonfarm nonresidential balances
    cre_nonaccrual_ratio  = CRE loans on nonaccrual / cre_total      (a fraction, 0.03 = 3%)
    nonaccrual_change_qoq = this quarter's ratio minus last quarter's ratio
    rising_two_quarters   = the ratio rose this quarter AND last quarter (both quarters in a row)
It also keeps each bank's name, city and state (Part C matches lender names to rssd_id).

WHERE THE FILES COME FROM: the FFIEC download page is a web form, not a plain link, so a person
downloads "Call Reports -- Single Period" (tab delimited) for each quarter and drops the zips in
data/inbox/ffiec/. See the README for the steps. (Automated download is not attempted.)

THE ITEM CODES ARE UNVERIFIED (see config/ffiec.yaml). On every load this collector prints a
"code audit": the description the FFIEC file itself gives for each code. Check it the first time.

Run from the pipeline/ folder:
    python -m collectors.ffiec                          # load every zip in data/inbox/ffiec/
    python -m collectors.ffiec --zip some.zip           # load a specific zip
    python -m collectors.ffiec --audit some.zip         # only print the code audit, change nothing

(Adapted from Neel's ffiec.py: zip parsing and quarter-file handling are his.)
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import shutil
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path

from collectors.common import config, db
from collectors.common.files import store_file
from collectors.common.inbox import _unique_dest
from collectors.common.log import RunTracker, get_logger, setup_logging
from collectors.common.parse import clean_str, load_yaml, to_date, to_float

COLLECTOR = "ffiec.py"
CONFIG_PATH = config.CONFIG_DIR / "ffiec.yaml"
log = get_logger("ffiec")
csv.field_size_limit(10_000_000)

BALANCE_MEASURES = ["cre_construction", "cre_multifamily", "cre_nonfarm_nonres"]
FLOW_MEASURES = {"past_due_30_89": "cre_past_due_30_89", "past_due_90_plus": "cre_past_due_90_plus",
                 "nonaccrual": "cre_nonaccrual"}
DOLLARS_PER_THOUSAND = 1000


# ---------------------------------------------------------------------------
# Reading the zip
# ---------------------------------------------------------------------------
def report_date_from_name(name: str) -> date | None:
    """'FFIEC CDR Call Bulk All Schedules 06302026.zip' -> 2026-06-30."""
    m = re.search(r"(\d{2})(\d{2})(\d{4})(?!\d)", name)
    if not m:
        return None
    try:
        return date(int(m[3]), int(m[1]), int(m[2]))
    except ValueError:
        return None


def candidate_codes(stem: str, cfg: dict) -> list[str]:
    """'F180' -> ['RCONF180', 'RCFDF180'] (RCON first)."""
    return [p + stem for p in cfg["prefixes"]]


def _read_tsv(raw: bytes, id_col: str) -> tuple[list[str], list[str], list[list[str]]]:
    """
    One bulk file -> (header, description row, data rows). The second line of a real file holds
    human-readable descriptions of each column; it is detected by a non-numeric value in the ID column.
    """
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig", errors="replace")), delimiter="\t"))
    if not rows:
        return [], [], []
    header = [h.strip().strip('"') for h in rows[0]]
    data, descriptions = rows[1:], []
    pos = header.index(id_col) if id_col in header else 0
    if data and len(data[0]) > pos and not re.fullmatch(r"\d+", data[0][pos].strip().strip('"')):
        descriptions, data = [d.strip().strip('"') for d in data[0]], data[1:]
    return header, descriptions, data


def parse_bulk_zip(data: bytes, cfg: dict) -> tuple[dict[str, dict], dict[str, str]]:
    """
    Returns ({rssd_id: {code or name/city/state: value}}, {code: description from the file}).
    Only the item codes listed in the config are read.
    """
    needed = {code for stem in cfg["items"] for code in candidate_codes(str(stem), cfg)}
    id_col = cfg["id_column"]
    banks: dict[str, dict] = defaultdict(dict)
    descriptions: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".txt"):
                continue
            header, desc_row, rows = _read_tsv(zf.read(name), id_col)
            if id_col not in header:
                continue
            id_pos = header.index(id_col)
            if re.search(cfg["por_file_pattern"], name, re.IGNORECASE):         # bank names file
                wanted = {}
                for field, cands in cfg["por_columns"].items():
                    pos = next((header.index(c) for c in cands if c in header), None)
                    if pos is not None:
                        wanted[field] = pos
            else:                                                                 # a schedule file
                wanted = {h: i for i, h in enumerate(header) if h in needed}
                for code, i in wanted.items():
                    if i < len(desc_row) and desc_row[i]:
                        descriptions[code] = desc_row[i]
            if not wanted:
                continue
            for r in rows:
                if len(r) <= id_pos or not r[id_pos].strip():
                    continue
                bank = banks[r[id_pos].strip().strip('"')]
                for key, pos in wanted.items():
                    if pos < len(r) and r[pos].strip() != "":
                        bank[key] = r[pos].strip().strip('"')
    return dict(banks), descriptions


def audit_codes(descriptions: dict[str, str], cfg: dict) -> list[str]:
    """
    Check each configured code against the file's own descriptions. Returns a list of warnings
    (and logs a table). This is how a person verifies the UNVERIFIED codes in config/ffiec.yaml.
    """
    warnings, lines = [], []
    for stem, rule in cfg["items"].items():
        stem = str(stem)
        found = next((c for c in candidate_codes(stem, cfg) if c in descriptions), None)
        if found is None:
            if descriptions:      # only meaningful if the files had descriptions at all
                warnings.append(f"{stem} ({rule['measure']}): none of {', '.join(candidate_codes(stem, cfg))} "
                                "found in the files; fix the code in config/ffiec.yaml")
            continue
        text = descriptions[found]
        lines.append(f"  {found:<10} {rule['measure']:<20} {text[:110]}")
        missing = [w for w in rule.get("expect", []) if w.lower() not in text.lower()]
        if missing:
            warnings.append(f"{found} ({rule['measure']}): its description does not mention "
                            f"{', '.join(repr(w) for w in missing)}; the code may be wrong. Description: {text[:150]}")
    if lines:
        log.info("code audit (what the FFIEC file says each configured code means):\n%s", "\n".join(lines))
    for w in warnings:
        log.warning("CODE AUDIT: %s", w)
    return warnings


# ---------------------------------------------------------------------------
# The calculations (kept as plain functions so they are easy to test)
# ---------------------------------------------------------------------------
def _first_number(values: dict, stem: str, cfg: dict) -> float | None:
    """The number for an item: RCON version first, then RCFD."""
    for code in candidate_codes(stem, cfg):
        num = to_float(values.get(code))
        if num is not None:
            return num
    return None


def bank_row(rssd: str, vals: dict, cfg: dict, report_date: date) -> dict | None:
    """One bank's bank_stats row for one quarter (trend columns are filled in later). None if no CRE data."""
    sums: dict[str, float | None] = {}
    for stem, rule in cfg["items"].items():
        num = _first_number(vals, str(stem), cfg)
        if num is not None:
            sums[rule["measure"]] = (sums.get(rule["measure"]) or 0.0) + num
    if not sums:
        return None
    dollars = lambda v: None if v is None else v * DOLLARS_PER_THOUSAND  # noqa: E731
    balances = [sums.get(m) for m in BALANCE_MEASURES]
    present = [b for b in balances if b is not None]
    total = sum(present) if present else None
    nonaccrual = sums.get("nonaccrual")
    return {
        "rssd_id": rssd, "report_date": report_date,
        "bank_name": clean_str(vals.get("bank_name")), "city": clean_str(vals.get("city")),
        "state": clean_str(vals.get("state")),
        "cre_construction": dollars(sums.get("cre_construction")),
        "cre_multifamily": dollars(sums.get("cre_multifamily")),
        "cre_nonfarm_nonres": dollars(sums.get("cre_nonfarm_nonres")),
        "cre_total": dollars(total),
        "cre_past_due_30_89": dollars(sums.get("past_due_30_89")),
        "cre_past_due_90_plus": dollars(sums.get("past_due_90_plus")),
        "cre_nonaccrual": dollars(nonaccrual),
        "cre_nonaccrual_ratio": (nonaccrual / total) if (nonaccrual is not None and total) else None,
    }


def compute_trends(history: list[tuple[date, float | None]], max_gap_days: int = 120) -> list[tuple[float | None, bool]]:
    """
    For one bank's quarters (oldest first, as (report_date, nonaccrual ratio)), return for each
    quarter (change vs the previous quarter, rising_two_quarters).
      change  = ratio - previous ratio, or None if there is no previous quarter, the previous quarter
                is more than max_gap_days earlier (a quarter is missing), or a ratio is missing.
      rising  = True when the change is positive AND the previous quarter's change was positive too.
    """
    out, prev, prev_change = [], None, None
    for when, ratio in history:
        change = None
        if prev is not None and ratio is not None and prev[1] is not None and (when - prev[0]).days <= max_gap_days:
            change = ratio - prev[1]
        rising = bool(change is not None and change > 0 and prev_change is not None and prev_change > 0)
        out.append((change, rising))
        prev, prev_change = (when, ratio), change
    return out


def update_trends(con, rssd_ids: list[str], max_gap_days: int = 120) -> None:
    """Recompute the trend columns for these banks across every stored quarter."""
    for rssd in rssd_ids:
        hist = con.execute("SELECT report_date, cre_nonaccrual_ratio FROM bank_stats WHERE rssd_id = ? "
                           "ORDER BY report_date", [rssd]).fetchall()
        for (when, _), (change, rising) in zip(hist, compute_trends(hist, max_gap_days)):
            con.execute("UPDATE bank_stats SET nonaccrual_change_qoq = ?, rising_two_quarters = ? "
                        "WHERE rssd_id = ? AND report_date = ?", [change, rising, rssd, when])


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_zip(con, path: Path, run, *, report_date=None, cfg: dict | None = None) -> str:
    """Load one quarter's zip. Returns "new" or "duplicate". The zip is saved unchanged in data/raw/."""
    cfg = cfg or load_yaml(CONFIG_PATH)
    path = Path(path)
    report_date = to_date(report_date) or report_date_from_name(path.name)
    if report_date is None:
        raise ValueError(f"can't tell the quarter from '{path.name}'; pass --report-date YYYY-MM-DD")
    data = path.read_bytes()
    banks, descriptions = parse_bulk_zip(data, cfg)             # parse first: a bad zip is never stored
    res = store_file(con, data, source="ffiec", doc_category="other", collector_name=COLLECTOR,
                     state=None, county=None, ext=".zip", notes=f"FFIEC call report bulk file, {report_date}")
    loaded = con.execute("SELECT 1 FROM bank_stats WHERE report_date = ? LIMIT 1", [report_date]).fetchone()
    if res.status == "duplicate" and loaded:
        run.skipped()
        log.info("already loaded (same fingerprint as %s): %s", res.file_id, path.name)
        return "duplicate"

    for w in audit_codes(descriptions, cfg):
        log.warning("%s: %s", path.name, w)
    rows = [r for rssd, vals in banks.items() if (r := bank_row(rssd, vals, cfg, report_date))]
    if not rows:
        raise ValueError(f"no CRE figures found in {path.name}; the codes in config/ffiec.yaml may be wrong")
    for r in rows:
        db.insert_row(con, "bank_stats", r, replace=True)
    update_trends(con, [r["rssd_id"] for r in rows], int(cfg.get("max_quarter_gap_days", 120)))
    log.info("%s: %d banks for %s", path.name, len(rows), report_date)
    run.new_file() if res.status == "new" else run.skipped()
    return res.status


def load_inbox(con, run, inbox_dir: Path | None = None, cfg: dict | None = None) -> dict:
    """Load every zip in data/inbox/ffiec/ (oldest quarter first, so trends build up in order)."""
    folder = Path(inbox_dir) if inbox_dir else config.inbox_dir() / "ffiec"
    totals = {"files": 0, "errors": 0}
    if not folder.exists():
        log.info("no FFIEC inbox folder yet at %s", folder)
        return totals
    zips = sorted((p for p in folder.glob("*.zip") if p.is_file()),
                  key=lambda p: (report_date_from_name(p.name) or date.min, p.name))
    for path in zips:
        totals["files"] += 1
        try:
            status = load_zip(con, path, run, cfg=cfg)
        except Exception as e:                                   # one bad quarter must not stop the rest
            totals["errors"] += 1
            run.error(f"could not load {path.name}: {e}")
            continue
        if status == "duplicate":
            dest = _unique_dest(folder / "_duplicates" / path.name)
            dest.parent.mkdir(exist_ok=True)
            shutil.move(str(path), dest)
        else:
            path.unlink()
    return totals


def collect(con) -> RunTracker:
    """The entry point used by run_all.py."""
    with RunTracker(con, COLLECTOR) as run:
        load_inbox(con, run)
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description="Load FFIEC call report CRE data into bank_stats.")
    parser.add_argument("--zip", action="append", type=Path, default=[], help="load this zip (repeatable)")
    parser.add_argument("--report-date", help="quarter end YYYY-MM-DD if it is not in the file name")
    parser.add_argument("--audit", type=Path, help="print the code audit for this zip and stop")
    args = parser.parse_args()
    setup_logging()
    if args.audit:
        cfg = load_yaml(CONFIG_PATH)
        _, descriptions = parse_bulk_zip(args.audit.read_bytes(), cfg)
        problems = audit_codes(descriptions, cfg)
        print(f"{len(descriptions)} configured codes found; {len(problems)} warnings")
        return
    con = db.connect()
    if args.zip:
        with RunTracker(con, COLLECTOR) as run:
            for z in sorted(args.zip, key=lambda p: report_date_from_name(p.name) or date.min):
                try:
                    load_zip(con, z, run, report_date=args.report_date)
                except Exception as e:
                    run.error(f"could not load {z.name}: {e}")
    else:
        run = collect(con)
    print(f"FFIEC: {run.status}, {run.files_new} new, {run.files_skipped} skipped, {len(run.errors)} errors")


if __name__ == "__main__":
    main()
