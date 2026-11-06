"""Load FFIEC Call Report bulk data (quarterly) into `bank_stats`: each bank's
commercial real estate (CRE) nonaccrual loans and the quarter-over-quarter trend.

    python -m collectors.ffiec --zip "FFIEC CDR Call Bulk All Schedules 06302026.zip"
    python -m collectors.ffiec --dir ~/Downloads/ffiec            # every zip in a folder
    python -m collectors.ffiec --url https://.../bulk.zip --report-date 2026-06-30

The FFIEC bulk page (cdr.ffiec.gov -> Download Bulk Data) is a web form rather
than a plain link, so the usual route is to download the "Call Reports --
Single Period", tab-delimited zip by hand and pass it with --zip (see README).
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, bindparam, text

from collectors.common import (
    CONFIG_DIR,
    FileResult,
    HttpClient,
    RunStats,
    clean_str,
    download_file,
    get_engine,
    init_db,
    load_yaml,
    register_local_file,
    run_isolated,
    setup_logging,
    to_float,
    to_iso_date,
    upsert,
)

SOURCE = "FFIEC"
CONFIG_PATH = CONFIG_DIR / "ffiec.yaml"
log = logging.getLogger("collectors.ffiec")
csv.field_size_limit(10_000_000)


def report_date_from_name(name: str) -> str | None:
    """'... 06302026.zip' -> '2026-06-30'."""
    m = re.search(r"(\d{2})(\d{2})(\d{4})(?!\d)", name)
    if not m:
        return None
    try:
        return datetime(int(m[3]), int(m[1]), int(m[2])).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _read_tsv(raw: bytes, id_col: str) -> tuple[list[str], list[list[str]]]:
    """Header + data rows of one bulk file; drops the description row if present
    (detected by a non-numeric value in the ID column)."""
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig", errors="replace")), delimiter="\t"))
    if not rows:
        return [], []
    header = [h.strip().strip('"') for h in rows[0]]
    data = rows[1:]
    pos = header.index(id_col) if id_col in header else 0
    first = data[0][pos] if data and len(data[0]) > pos else ""
    if data and not re.fullmatch(r"\d+", first.strip().strip('"')):
        data = data[1:]  # second line holds human-readable descriptions
    return header, data


def parse_bulk_zip(data: bytes, cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return {rssd_id: {MDRM code or por field: value}} for the codes we need."""
    needed = {c for item in [*cfg["nonaccrual_items"].values(), *cfg["loan_total_items"].values()] for c in item}
    id_col = cfg["id_column"]
    banks: dict[str, dict[str, Any]] = defaultdict(dict)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".txt"):
                continue
            header, rows = _read_tsv(zf.read(name), id_col)
            if id_col not in header:
                continue
            id_pos = header.index(id_col)
            if re.search(cfg["por_file_pattern"], name, re.IGNORECASE):
                wanted = {}
                for field, cands in cfg["por_columns"].items():
                    pos = next((header.index(c) for c in cands if c in header), None)
                    if pos is not None:
                        wanted[field] = pos
            else:
                wanted = {h: i for i, h in enumerate(header) if h in needed}
            if not wanted:
                continue
            for r in rows:
                if len(r) <= id_pos or not r[id_pos].strip():
                    continue
                bank = banks[r[id_pos].strip().strip('"')]
                for key, pos in wanted.items():
                    if pos < len(r) and r[pos].strip() != "":
                        bank[key] = r[pos].strip().strip('"')
    return dict(banks)


def _sum_items(values: dict[str, Any], items: dict[str, list[str]]) -> float | None:
    total, seen = 0.0, False
    for codes in items.values():
        for code in codes:
            num = to_float(values.get(code))
            if num is not None:
                total += num
                seen = True
                break
    return total if seen else None


def bank_rows(banks: dict[str, dict[str, Any]], cfg: dict[str, Any], report_date: str, file_id: str) -> list[dict[str, Any]]:
    """Compute CRE totals per bank for one quarter (trend filled in later)."""
    rows = []
    for rssd, vals in banks.items():
        nonaccrual = _sum_items(vals, cfg["nonaccrual_items"])
        total = _sum_items(vals, cfg["loan_total_items"])
        if nonaccrual is None and total is None:
            continue
        rows.append({
            "rssd_id": rssd,
            "report_date": report_date,
            "bank_name": clean_str(vals.get("bank_name")),
            "city": clean_str(vals.get("city")),
            "state": clean_str(vals.get("state")),
            "cre_loans_total": total,
            "cre_nonaccrual": nonaccrual,
            "cre_nonaccrual_ratio": (nonaccrual / total) if (nonaccrual is not None and total) else None,
            "file_id": file_id,
        })
    return rows


def update_trends(engine: Engine, rssd_ids: list[str], tolerance: float = 0.0) -> None:
    """Recompute nonaccrual_qoq_change / nonaccrual_trend for these banks, across
    every quarter loaded, comparing each quarter to the bank's previous one."""
    if not rssd_ids:
        return
    sel = text("SELECT rssd_id, report_date, cre_nonaccrual FROM bank_stats WHERE rssd_id IN :ids "
               "ORDER BY rssd_id, report_date").bindparams(bindparam("ids", expanding=True))
    updates, prev = [], {}
    with engine.connect() as conn:
        for rssd, rdate, amt in conn.execute(sel, {"ids": list(rssd_ids)}):
            before = prev.get(rssd)
            change = trend = None
            if before is not None and amt is not None and before[1] is not None:
                change = amt - before[1]
                trend = "flat" if abs(change) <= tolerance else ("up" if change > 0 else "down")
            updates.append({"c": change, "t": trend, "r": rssd, "d": rdate})
            prev[rssd] = (rdate, amt)
    with engine.begin() as conn:
        conn.execute(text("UPDATE bank_stats SET nonaccrual_qoq_change = :c, nonaccrual_trend = :t "
                          "WHERE rssd_id = :r AND report_date = :d"), updates)


def _rows_loaded(engine: Engine, file_id: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM bank_stats WHERE file_id = :f LIMIT 1"), {"f": file_id}).first() is not None


def load_quarter(engine: Engine, reg: FileResult, report_date: str | None, name: str,
                 cfg: dict[str, Any], stats: RunStats, force: bool = False) -> None:
    """Parse one registered bulk zip and upsert its banks."""
    if not reg.is_new and not force and _rows_loaded(engine, reg.file_id):
        log.info("%s already loaded (file_id %s)", name, reg.file_id)
        return
    report_date = report_date or report_date_from_name(name)
    if not report_date:
        raise ValueError(f"can't tell the quarter from '{name}'; pass --report-date YYYY-MM-DD")
    rows = bank_rows(parse_bulk_zip(Path(reg.local_path).read_bytes(), cfg), cfg, report_date, reg.file_id)
    if not rows:
        raise ValueError(f"no CRE figures found in {name}; check codes in config/ffiec.yaml")
    stats.rows_written += upsert(engine, "bank_stats", rows, ["rssd_id", "report_date"])
    with engine.begin() as conn:
        conn.execute(text("UPDATE raw_files SET date = :d WHERE file_id = :f AND date IS NULL"),
                     {"d": report_date, "f": reg.file_id})
    update_trends(engine, [r["rssd_id"] for r in rows], float(cfg.get("flat_tolerance_thousands", 0)))
    log.info("%s: %d banks for %s", name, len(rows), report_date)


def run(engine: Engine, *, zips: list[Path] = (), urls: list[str] = (), report_date: str | None = None,
        client: HttpClient | None = None, force: bool = False, stats: RunStats | None = None) -> RunStats:
    """Load each zip / URL; one failing quarter does not stop the others."""
    cfg = load_yaml(CONFIG_PATH)
    stats = stats or RunStats("ffiec")
    # oldest first so each quarter's trend sees the one before it
    for path in sorted(zips, key=lambda p: report_date_from_name(p.name) or ""):
        def _local(path: Path = path) -> None:
            rd = report_date or report_date_from_name(path.name)
            reg = register_local_file(engine, path, source=SOURCE, date=rd, stats=stats)
            load_quarter(engine, reg, rd, path.name, cfg, stats, force)
        run_isolated(stats, str(path), _local)
    for url in urls:
        def _remote(url: str = url) -> None:
            rd = report_date or report_date_from_name(url)
            reg = download_file(client or HttpClient(), engine, url, source=SOURCE, date=rd, ext=".zip", stats=stats)
            load_quarter(engine, reg, rd, url, cfg, stats, force)
        run_isolated(stats, url, _remote)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load FFIEC call report CRE nonaccruals into bank_stats.")
    parser.add_argument("--zip", action="append", type=Path, default=[], help="bulk zip on this computer (repeatable)")
    parser.add_argument("--dir", type=Path, help="load every .zip in this folder")
    parser.add_argument("--url", action="append", default=[], help="direct link to a bulk zip (repeatable)")
    parser.add_argument("--report-date", help="quarter end YYYY-MM-DD if not in the file name")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--db-url")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    zips = list(args.zip) + (sorted(args.dir.glob("*.zip")) if args.dir else [])
    if not zips and not args.url:
        parser.error("give --zip, --dir or --url")
    engine = get_engine(args.db_url)
    init_db(engine)
    stats = run(engine, zips=zips, urls=args.url, force=args.force,
                report_date=to_iso_date(args.report_date) if args.report_date else None)
    stats.log_summary()
    return 1 if stats.errors and not stats.rows_written else 0


if __name__ == "__main__":
    sys.exit(main())
