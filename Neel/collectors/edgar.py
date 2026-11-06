"""Download CMBS loan-level performance data (Form ABS-EE, exhibit EX-102) from
SEC EDGAR and write it to the shared `loans` table with source = "EDGAR".

    python -m collectors.edgar --since 2026-10-01 [--until 2026-10-31] [--cik 1234567] [--max-filings 20]

SEC fair access: every request carries the USER_AGENT from .env (name + email)
and sec.gov requests are limited to 8 per second (SEC's cap is 10).
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import Engine, text

from collectors.common import (
    CONFIG_DIR,
    HttpClient,
    RunStats,
    clean_str,
    download_file,
    get_engine,
    init_db,
    load_yaml,
    run_isolated,
    setup_logging,
    to_float,
    to_iso_date,
    upsert,
    utc_now,
)
from collectors.trepp import LOAN_COLUMNS, make_loan_id

SOURCE = "EDGAR"
CONFIG_PATH = CONFIG_DIR / "edgar.yaml"
log = logging.getLogger("collectors.edgar")

_IDX_LINE = re.compile(
    r"^(?P<form>\S+(?: \S+)*?)\s{2,}(?P<name>.+?)\s{2,}(?P<cik>\d+)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<file>\S+)\s*$"
)


@dataclass
class Filing:
    form: str
    company: str
    cik: str
    filed: str
    accession: str

    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")


def quarters(since: date, until: date) -> Iterator[tuple[int, int]]:
    """(year, quarter) pairs covering since..until."""
    y, q = since.year, (since.month - 1) // 3 + 1
    while (y, q) <= (until.year, (until.month - 1) // 3 + 1):
        yield y, q
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)


def parse_form_index(text_: str) -> list[Filing]:
    """Parse EDGAR's fixed-width form.idx into Filing records."""
    lines = text_.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if set(line.strip()) == {"-"}), 0)
    out = []
    for line in lines[start:]:
        m = _IDX_LINE.match(line)
        if m:
            accession = Path(m["file"]).stem
            out.append(Filing(m["form"], m["name"].strip(), m["cik"], m["date"], accession))
    return out


def is_cmbs_name(company: str, patterns: list[str]) -> bool:
    return any(re.search(p, company, re.IGNORECASE) for p in patterns)


def select_filings(
    filings: list[Filing], cfg: dict[str, Any], since: date, until: date, ciks: set[str] | None
) -> list[Filing]:
    """Keep ABS-EE filings in the date range from CMBS trusts (or the given CIKs)."""
    keep = []
    for f in filings:
        if f.form not in cfg["form_types"] or not (since.isoformat() <= f.filed <= until.isoformat()):
            continue
        if ciks:
            if f.cik.lstrip("0") in ciks:
                keep.append(f)
        elif is_cmbs_name(f.company, cfg["cmbs_name_patterns"]):
            keep.append(f)
    return keep


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(elem: ET.Element | None, names: list[str], skip_tag: str | None = None) -> str | None:
    """First non-empty text among ``names`` (in preference order) below ``elem``,
    not looking inside ``skip_tag`` sub-elements."""
    if elem is None:
        return None
    found: dict[str, str] = {}
    stack = list(elem)
    while stack:
        child = stack.pop()
        name = _local(child.tag)
        if skip_tag and name == skip_tag:
            continue
        if name in names and name not in found and child.text and child.text.strip():
            found[name] = child.text.strip()
        stack.extend(child)
    return next((found[n] for n in names if n in found), None)


def _decimal(value: str | None) -> float | None:
    num = to_float(value)
    if num is None:
        return None
    return num / 100 if abs(num) > 1.5 else num  # ABS-EE uses decimals; tolerate 4.5 meaning 4.5%


def parse_asset_xml(data: bytes, cfg: dict[str, Any], *, deal_name: str, file_id: str) -> list[dict[str, Any]]:
    """Parse one EX-102 XML into `loans` rows (one per loan, first property only)."""
    record_tag, prop_tag, fields = cfg["record_tag"], cfg["property_tag"], cfg["fields"]
    loaded_at = utc_now()
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for _event, elem in ET.iterparse(io.BytesIO(data), events=("end",)):
        if _local(elem.tag) != record_tag:
            continue
        prop = next((c for c in elem.iter() if _local(c.tag) == prop_tag), None)

        def get(target: str) -> str | None:
            """Loan-level tag first, then the loan's first <property>."""
            names = fields.get(target, [])
            return _find_text(elem, names, skip_tag=prop_tag) or _find_text(prop, names)

        loan_number = clean_str(get("loan_number"))
        period = to_iso_date(get("reporting_period"))
        if not loan_number or not period:
            elem.clear()
            continue
        transfer, back = to_iso_date(get("special_transfer_date")), to_iso_date(get("special_return_date"))
        special = None if transfer is None else int(back is None or back < transfer)
        rec = {
            "loan_id": make_loan_id(SOURCE, deal_name, loan_number),
            "reporting_period": period,
            "source": SOURCE,
            "file_id": file_id,
            "deal_name": deal_name,
            "loan_number": loan_number,
            "property_name": clean_str(get("property_name")),
            "property_address": clean_str(get("property_address")),
            "city": clean_str(get("city")),
            "state": clean_str(get("state")),
            "zip": clean_str(get("zip")),
            "property_type": clean_str(get("property_type")),
            "borrower_name": None,  # not disclosed in ABS-EE
            "lender_name": clean_str(get("lender_name")),
            "servicer_name": clean_str(get("servicer_name")),
            "original_balance": to_float(get("original_balance")),
            "current_balance": to_float(get("current_balance")),
            "interest_rate": _decimal(get("interest_rate")),
            "origination_date": to_iso_date(get("origination_date")),
            "maturity_date": to_iso_date(get("maturity_date")),
            "dscr": to_float(get("dscr")),
            "occupancy": _decimal(get("occupancy")),
            "delinquency_status": clean_str(get("delinquency_status")),
            "special_servicing": special,
            "watchlist": None,  # not in ABS-EE; comes from Trepp
            "watchlist_comment": None,
            "loaded_at": loaded_at,
        }
        rows[(rec["loan_id"], period)] = {c: rec[c] for c in LOAN_COLUMNS}
        elem.clear()
    return list(rows.values())


def _rows_loaded(engine: Engine, file_id: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM loans WHERE file_id = :f LIMIT 1"), {"f": file_id}).first() is not None


def process_filing(client: HttpClient, engine: Engine, filing: Filing, cfg: dict[str, Any], stats: RunStats) -> None:
    """Find the EX-102 file in one filing, download it once, load its loans."""
    idx_url = cfg["filing_index_url"].format(cik=filing.cik, accession_nodash=filing.accession_nodash)
    items = client.get(idx_url).json().get("directory", {}).get("item", [])
    pattern = re.compile(cfg["asset_file_pattern"], re.IGNORECASE)
    assets = [i for i in items if pattern.search(i.get("name", ""))]
    if not assets:
        log.info("no EX-102 asset file in %s (%s)", filing.accession, filing.company)
        return
    for item in assets:
        size_mb = (to_float(item.get("size")) or 0) / 1e6
        if size_mb > cfg.get("max_file_mb", 200):
            stats.error(item["name"], f"skipped: {size_mb:.0f} MB is over max_file_mb")
            continue
        url = cfg["file_url"].format(cik=filing.cik, accession_nodash=filing.accession_nodash, name=item["name"])
        res = download_file(client, engine, url, source=SOURCE, date=filing.filed, ext=".xml", stats=stats)
        if not res.is_new and _rows_loaded(engine, res.file_id):
            continue
        rows = parse_asset_xml(Path(res.local_path).read_bytes(), cfg, deal_name=filing.company, file_id=res.file_id)
        if not rows:
            log.warning("no loan rows parsed from %s (not CMBS, or tag names differ; see config/edgar.yaml)", url)
        stats.rows_written += upsert(engine, "loans", rows, ["loan_id", "reporting_period"])


def run(
    client: HttpClient,
    engine: Engine,
    since: date,
    until: date,
    *,
    ciks: set[str] | None = None,
    max_filings: int | None = None,
    cfg: dict[str, Any] | None = None,
    stats: RunStats | None = None,
) -> RunStats:
    """Collect every CMBS ABS-EE filing filed between ``since`` and ``until``."""
    cfg = cfg or load_yaml(CONFIG_PATH)
    stats = stats or RunStats("edgar")
    selected: list[Filing] = []
    for year, q in quarters(since, until):
        url = cfg["index_url"].format(year=year, quarter=q)
        resp = run_isolated(stats, url, lambda url=url: client.get(url))
        if resp is not None:
            selected += select_filings(parse_form_index(resp.text), cfg, since, until, ciks)
    if max_filings:
        selected = selected[:max_filings]
    log.info("%d CMBS ABS-EE filings selected", len(selected))
    for filing in selected:
        run_isolated(stats, f"{filing.company} {filing.accession}",
                     lambda f=filing: process_filing(client, engine, f, cfg, stats))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load CMBS loan-level data from SEC EDGAR (Form ABS-EE).")
    parser.add_argument("--since", required=True, type=date.fromisoformat, help="first filing date, YYYY-MM-DD")
    parser.add_argument("--until", type=date.fromisoformat, default=date.today(), help="last filing date")
    parser.add_argument("--cik", action="append", help="only this trust CIK (repeatable); skips the name filter")
    parser.add_argument("--max-filings", type=int, help="stop after this many filings (useful for a first test)")
    parser.add_argument("--db-url")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    engine = get_engine(args.db_url)
    init_db(engine)
    ciks = {c.lstrip("0") for c in args.cik} if args.cik else None
    stats = run(HttpClient(), engine, args.since, args.until, ciks=ciks, max_filings=args.max_filings)
    stats.log_summary()
    return 1 if stats.errors and not (stats.rows_written or stats.files_skipped_duplicate) else 0


if __name__ == "__main__":
    sys.exit(main())
