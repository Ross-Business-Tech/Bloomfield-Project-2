"""Load Bloomfield's Trepp export (CSV or XLSX) into the shared `loans` table.

    python -m collectors.trepp path/to/export.xlsx [--as-of 2026-10-31]

LICENSED DATA: this module never makes a network call. The file is registered
in raw_files with restricted = 1 so nobody sends it to an external API, and it
is not copied anywhere: its own local path is recorded.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, text

from collectors.common import (
    CONFIG_DIR,
    RunStats,
    clean_str,
    get_engine,
    init_db,
    load_yaml,
    register_local_file,
    run_isolated,
    setup_logging,
    to_flag,
    to_float,
    to_iso_date,
    upsert,
    utc_now,
)

SOURCE = "TREPP"
MAPPING_PATH = CONFIG_DIR / "trepp_mapping.yaml"
LOAN_COLUMNS = [
    "loan_id", "reporting_period", "source", "file_id", "deal_name", "loan_number", "property_name",
    "property_address", "city", "state", "zip", "property_type", "borrower_name", "lender_name",
    "servicer_name", "original_balance", "current_balance", "interest_rate", "origination_date",
    "maturity_date", "dscr", "occupancy", "delinquency_status", "special_servicing", "watchlist",
    "watchlist_comment", "loaded_at",
]
log = logging.getLogger("collectors.trepp")


def _norm(header: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


def make_loan_id(source: str, deal: str, loan_number: str) -> str:
    """Stable loan ID: '<SOURCE>:<deal>:<loan number>' with whitespace collapsed."""
    clean = lambda s: re.sub(r"\s+", " ", str(s)).strip()  # noqa: E731
    return f"{source}:{clean(deal)}:{clean(loan_number)}"


def read_table(path: Path, sheet: Any = 0) -> pd.DataFrame:
    """Read a CSV/XLSX with every cell as text and no header assumed."""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return pd.read_excel(path, sheet_name=sheet, header=None, dtype=str)
    # csv module tolerates title rows that are shorter/longer than the table
    import csv

    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        lines = list(csv.reader(fh))
    width = max((len(r) for r in lines), default=0)
    return pd.DataFrame([r + [""] * (width - len(r)) for r in lines], dtype=str)


def resolve_columns(headers: list[Any], mapping: dict[str, list[str]]) -> dict[str, int]:
    """Return {loans column: position in file} using the mapping's candidate names."""
    normalized = [_norm(h) for h in headers]
    found: dict[str, int] = {}
    for target, candidates in mapping.items():
        for cand in candidates:
            if _norm(cand) in normalized:
                found[target] = normalized.index(_norm(cand))
                break
    return found


def find_header_row(raw: pd.DataFrame, mapping: dict[str, list[str]], scan: int = 25) -> int:
    """Index of the first row that looks like the header (>= 3 known names)."""
    for i in range(min(scan, len(raw))):
        if len(resolve_columns(list(raw.iloc[i].values), mapping)) >= 3:
            return i
    raise ValueError("could not find a header row matching config/trepp_mapping.yaml")


def _percent(value: Any) -> float | None:
    num = to_float(value)
    if num is None:
        return None
    return num / 100 if (abs(num) > 1.0 or "%" in str(value)) else num


def map_rows(
    df_raw: pd.DataFrame, config: dict[str, Any], *, file_id: str, as_of: str | None, stats: RunStats
) -> list[dict[str, Any]]:
    """Turn the raw sheet into `loans` rows using the YAML mapping."""
    mapping: dict[str, list[str]] = config["columns"]
    header_idx = find_header_row(df_raw, mapping)
    positions = resolve_columns(list(df_raw.iloc[header_idx].values), mapping)
    missing = sorted(set(mapping) - set(positions))
    if missing:
        log.warning("Trepp columns not found in file (left blank): %s", ", ".join(missing))
    for req in config.get("required", []):
        if req not in positions:
            raise ValueError(f"required column '{req}' not found; add its header name to trepp_mapping.yaml")

    percent_cols = set(config.get("percent_columns", []))
    flag_cols = set(config.get("flag_columns", []))
    date_cols = set(config.get("date_columns", []))
    number_cols = set(config.get("number_columns", []))
    loaded_at = utc_now()

    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for line_no, values in enumerate(df_raw.iloc[header_idx + 1 :].itertuples(index=False), start=header_idx + 2):
        values = list(values)
        if all(clean_str(v) is None for v in values):
            continue
        rec: dict[str, Any] = {}
        for target, pos in positions.items():
            v = values[pos] if pos < len(values) else None
            if target in percent_cols:
                rec[target] = _percent(v)
            elif target in flag_cols:
                rec[target] = to_flag(v)
            elif target in date_cols:
                rec[target] = to_iso_date(v)
            elif target in number_cols:
                rec[target] = to_float(v)
            else:
                rec[target] = clean_str(v)
        if not all(rec.get(r) for r in config.get("required", [])):
            stats.error(f"row {line_no}", "missing deal name or loan number; skipped")
            continue
        period = rec.get("reporting_period") or as_of
        if not period:
            raise ValueError("no reporting period column in file; pass --as-of YYYY-MM-DD")
        rec.update(
            loan_id=make_loan_id(SOURCE, rec["deal_name"], rec["loan_number"]),
            reporting_period=period,
            source=SOURCE,
            file_id=file_id,
            loaded_at=loaded_at,
        )
        rows[(rec["loan_id"], period)] = {c: rec.get(c) for c in LOAN_COLUMNS}  # last one wins
    return list(rows.values())


def _already_loaded(engine: Engine, file_id: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM loans WHERE file_id = :f LIMIT 1"), {"f": file_id}).first() is not None


def load_trepp(
    path: str | Path,
    engine: Engine,
    *,
    as_of: str | None = None,
    mapping_path: Path = MAPPING_PATH,
    force: bool = False,
    stats: RunStats | None = None,
) -> RunStats:
    """Register the Trepp file and upsert its loans. Re-running is a no-op."""
    stats = stats or RunStats("trepp")
    config = load_yaml(mapping_path)
    path = Path(path).expanduser().resolve()
    as_of = to_iso_date(as_of) if as_of else None

    reg = register_local_file(engine, path, source=SOURCE, date=as_of, restricted=True, stats=stats)
    if not reg.is_new and not force and _already_loaded(engine, reg.file_id):
        log.info("Trepp file already loaded (same SHA-256 as file_id %s); nothing to do", reg.file_id)
        return stats

    raw = read_table(path, config.get("sheet", 0))
    rows = map_rows(raw, config, file_id=reg.file_id, as_of=as_of, stats=stats)
    stats.rows_written += upsert(engine, "loans", rows, ["loan_id", "reporting_period"])
    if rows and not as_of:
        with engine.begin() as conn:  # record the data date on the raw_files row
            conn.execute(
                text("UPDATE raw_files SET date = :d WHERE file_id = :f AND date IS NULL"),
                {"d": max(r["reporting_period"] for r in rows), "f": reg.file_id},
            )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load a Trepp export into the loans table (stays local).")
    parser.add_argument("path", nargs="?", default=os.environ.get("TREPP_EXPORT_PATH"),
                        help="CSV/XLSX export (default: $TREPP_EXPORT_PATH)")
    parser.add_argument("--as-of", help="reporting period if the file has no date column (YYYY-MM-DD)")
    parser.add_argument("--mapping", type=Path, default=MAPPING_PATH)
    parser.add_argument("--force", action="store_true", help="reload rows even if this exact file was loaded")
    parser.add_argument("--db-url")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    if not args.path:
        parser.error("give the export path or set TREPP_EXPORT_PATH in Neel/.env")

    engine = get_engine(args.db_url)
    init_db(engine)
    stats = RunStats("trepp")
    run_isolated(stats, str(args.path), lambda: load_trepp(
        args.path, engine, as_of=args.as_of, mapping_path=args.mapping, force=args.force, stats=stats))
    stats.log_summary()
    return 1 if stats.errors and not stats.rows_written else 0


if __name__ == "__main__":
    sys.exit(main())
