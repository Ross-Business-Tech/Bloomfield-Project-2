"""
trepp.py - loads Bloomfield's Trepp screen (every loan with DSCR under 1.0x) into the loans table.

In plain English: a person drops the export (CSV or Excel) into data/inbox/trepp/. This collector
  1. finds the header row and matches columns using config/trepp_columns.yaml,
  2. warns about columns it could not find, and about any loan whose DSCR is 1.0 or higher
     (the screen may have changed; the row is still kept),
  3. saves the file unchanged in data/raw/ (fingerprinted, so the same file is never loaded twice),
  4. writes one row per loan to `loans` with source "trepp", watchlist_flag and servicer_commentary.

LICENSED DATA: this file never makes a network call. Trepp data stays in our local database. It is
never committed (see .gitignore) and the Reader never sends it to an AI service.

Run from the pipeline/ folder:
    python -m collectors.trepp                       # load everything waiting in data/inbox/trepp/
    python -m collectors.trepp --as-of 2026-10-31    # if the file has no date column

(The column-matching logic was written by Neel and ported to the shared database and helpers.)
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from collectors.common import config, db
from collectors.common.files import store_file
from collectors.common.inbox import _unique_dest
from collectors.common.log import RunTracker, get_logger, setup_logging
from collectors.common.parse import (clean_str, load_yaml, to_bool, to_date, to_float, to_percent)

COLLECTOR = "trepp.py"
MAPPING_PATH = config.CONFIG_DIR / "trepp_columns.yaml"
SUPPORTED = {".csv", ".xlsx", ".xlsm"}
log = get_logger("trepp")

# Columns written to the loans table (loan_id is only used to build loan_key).
LOAN_COLUMNS = ["loan_key", "source", "deal_name", "property_name", "property_address", "property_city",
                "property_state", "property_zip", "property_type", "current_balance", "original_balance",
                "maturity_date", "dscr", "occupancy", "payment_status", "servicer_name", "watchlist_flag",
                "servicer_commentary", "report_period", "file_id", "collected_at"]


# ---------------------------------------------------------------------------
# Reading the file and matching columns
# ---------------------------------------------------------------------------
def _norm(header: Any) -> str:
    """Compare headers ignoring capitals, spaces and punctuation."""
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


def read_table(path: Path, sheet: Any = 0) -> pd.DataFrame:
    """Read a CSV or Excel file with every cell as text and no header assumed."""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return pd.read_excel(path, sheet_name=sheet, header=None, dtype=str)
    if suffix == ".xls":
        raise ValueError("old .xls files are not supported; open it in Excel and save as .xlsx or .csv")
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        lines = list(csv.reader(fh))
    width = max((len(r) for r in lines), default=0)         # title rows may be shorter than the table
    return pd.DataFrame([r + [""] * (width - len(r)) for r in lines], dtype=str)


def resolve_columns(headers: list[Any], mapping: dict[str, list[str]]) -> dict[str, int]:
    """{our column: position in the file}, using each column's list of possible header names."""
    normalized = [_norm(h) for h in headers]
    found: dict[str, int] = {}
    for target, candidates in mapping.items():
        for cand in candidates:
            if _norm(cand) in normalized:
                found[target] = normalized.index(_norm(cand))
                break
    return found


def find_header_row(raw: pd.DataFrame, mapping: dict[str, list[str]], scan: int = 25) -> int:
    """The first row (of the first 25) that contains at least 3 known column names."""
    for i in range(min(scan, len(raw))):
        if len(resolve_columns(list(raw.iloc[i].values), mapping)) >= 3:
            return i
    raise ValueError("could not find a header row that matches config/trepp_columns.yaml")


def make_loan_key(deal: str, loan_id: str) -> str:
    """'trepp:<deal>:<loan id>' with extra spaces squeezed out."""
    squeeze = lambda s: re.sub(r"\s+", " ", str(s)).strip()  # noqa: E731
    return f"trepp:{squeeze(deal)}:{squeeze(loan_id)}"


def map_rows(raw: pd.DataFrame, cfg: dict, *, file_id: str, as_of, run=None) -> list[dict]:
    """Turn the raw sheet into loans rows (see the module notes for the checks)."""
    mapping: dict[str, list[str]] = cfg["columns"]
    header_idx = find_header_row(raw, mapping)
    positions = resolve_columns(list(raw.iloc[header_idx].values), mapping)

    missing = sorted(set(mapping) - set(positions))
    if missing:
        log.warning("expected columns not found in the file (left blank): %s", ", ".join(missing))
    for req in cfg.get("required", []):
        if req not in positions:
            raise ValueError(f"required column '{req}' not found; add its header name to trepp_columns.yaml")

    percent, booleans = set(cfg.get("percent_columns", [])), set(cfg.get("bool_columns", []))
    dates, numbers = set(cfg.get("date_columns", [])), set(cfg.get("number_columns", []))
    now = db.utc_now()

    rows: dict[str, dict] = {}
    for line_no, values in enumerate(raw.iloc[header_idx + 1:].itertuples(index=False), start=header_idx + 2):
        values = list(values)
        if all(clean_str(v) is None for v in values):
            continue                                                  # blank line
        rec: dict[str, Any] = {}
        for target, pos in positions.items():
            v = values[pos] if pos < len(values) else None
            if target in percent:
                rec[target] = to_percent(v)
            elif target in booleans:
                rec[target] = to_bool(v)
            elif target in dates:
                rec[target] = to_date(v)
            elif target in numbers:
                rec[target] = to_float(v)
            else:
                rec[target] = clean_str(v)
        if not all(rec.get(r) for r in cfg.get("required", [])):
            msg = f"row {line_no}: missing deal name or loan ID; skipped"
            run.error(msg) if run else log.error(msg)
            continue
        period = rec.get("report_period") or as_of
        if period is None:
            raise ValueError("no report date in the file; pass --as-of YYYY-MM-DD")
        row = {c: rec.get(c) for c in LOAN_COLUMNS}
        row.update(loan_key=make_loan_key(rec["deal_name"], rec["loan_id"]), source="trepp",
                   report_period=period, file_id=file_id, collected_at=now)
        rows[row["loan_key"]] = row                                   # same loan twice: last one wins
    return list(rows.values())


def warn_if_screen_changed(rows: list[dict], cfg: dict) -> int:
    """The Trepp screen is 'DSCR under 1.0x'. Count (and warn about) loans at or above that."""
    limit = float(cfg.get("expected_max_dscr", 1.0))
    high = [r for r in rows if r.get("dscr") is not None and r["dscr"] >= limit]
    if high:
        log.warning("%d of %d loans have DSCR of %.2f or higher (kept, but the screen may have changed): %s",
                    len(high), len(rows), limit, ", ".join(r["loan_key"] for r in high[:5]))
    return len(high)


# ---------------------------------------------------------------------------
# Loading one file / the whole inbox
# ---------------------------------------------------------------------------
def _sidecar_report_period(path: Path):
    """An optional <file>.json next to the export can say {"report_period": "2026-10-31"}."""
    import json
    for cand in (path.with_name(path.name + ".json"), path.with_suffix(".json")):
        if cand.exists() and cand != path:
            try:
                return to_date(json.loads(cand.read_text(encoding="utf-8")).get("report_period")), cand
            except (OSError, ValueError):
                pass
    return None, None


def load_trepp_file(con, path: Path, run, *, as_of=None, cfg: dict | None = None) -> str:
    """
    Load one export. Returns "new" or "duplicate". The file is copied to data/raw/ unchanged.
    If the same file was stored before but its loans never got written (a crash), they are written now.
    """
    cfg = cfg or load_yaml(MAPPING_PATH)
    side_period, _ = _sidecar_report_period(path)
    as_of = to_date(as_of) or side_period

    raw = read_table(path, cfg.get("sheet", 0))
    res = store_file(con, path.read_bytes(), source="trepp", doc_category="other",
                     collector_name=COLLECTOR, state=None, county=None, ext=path.suffix,
                     notes="Trepp screen export (licensed data: local only)")
    already = con.execute("SELECT 1 FROM loans WHERE file_id = ? LIMIT 1", [res.file_id]).fetchone()
    if res.status == "duplicate" and already:
        run.skipped()
        log.info("already loaded (same fingerprint as %s): %s", res.file_id, path.name)
        return "duplicate"

    # The date to use if the file has no date column: --as-of, a sidecar, or (last resort) the file's date.
    if as_of is None:
        as_of = datetime.fromtimestamp(path.stat().st_mtime).date()
        log.warning("%s has no report date column; using its file date %s (pass --as-of to override)",
                    path.name, as_of)
    rows = map_rows(raw, cfg, file_id=res.file_id, as_of=as_of, run=run)
    warn_if_screen_changed(rows, cfg)
    written = sum(1 for r in rows if db.upsert_loan_if_newer(con, r))
    log.info("%s: %d loans read, %d written", path.name, len(rows), written)
    if res.status == "new":
        run.new_file()
    else:
        run.skipped()
    return res.status


def load_inbox(con, run, inbox_dir: Path | None = None, as_of=None, cfg: dict | None = None) -> dict:
    """Load every export waiting in data/inbox/trepp/, then clear it from the inbox."""
    folder = Path(inbox_dir) if inbox_dir else config.inbox_dir() / "trepp"
    totals = {"files": 0, "errors": 0}
    if not folder.exists():
        log.info("no Trepp inbox folder yet at %s", folder)
        return totals
    for path in sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED | {".xls"}):
        totals["files"] += 1
        try:
            status = load_trepp_file(con, path, run, as_of=as_of, cfg=cfg)
        except Exception as e:                                       # one bad file must not stop the rest
            totals["errors"] += 1
            run.error(f"could not load {path.name}: {e}")
            continue
        _, sidecar = _sidecar_report_period(path)
        if status == "duplicate":                                    # keep a copy, out of the way
            dest = _unique_dest(folder / "_duplicates" / path.name)
            dest.parent.mkdir(exist_ok=True)
            shutil.move(str(path), dest)
        else:
            path.unlink()
        if sidecar and sidecar.exists():
            sidecar.unlink()
    return totals


def collect(con, as_of=None) -> RunTracker:
    """The entry point used by run_all.py."""
    with RunTracker(con, COLLECTOR) as run:
        load_inbox(con, run, as_of=as_of)
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the Trepp screen from data/inbox/trepp/ (stays local).")
    parser.add_argument("--as-of", help="report date if the file has no date column (YYYY-MM-DD)")
    args = parser.parse_args()
    setup_logging()
    con = db.connect()
    run = collect(con, args.as_of)
    print(f"Trepp: {run.status}, {run.files_new} new, {run.files_skipped} skipped, {len(run.errors)} errors")


if __name__ == "__main__":
    main()
