"""
edgar.py - downloads CMBS loan-level data from SEC EDGAR and writes it to the loans table.

In plain English: every month, each commercial-mortgage bond trust (CMBS) must file an "ABS-EE" form
with the SEC. Attached is a data file (exhibit EX-102, in XML) listing every loan in the trust with
its balance, maturity date, DSCR, occupancy, payment status and property. This collector
  1. reads the SEC's quarterly index of filings and picks the CMBS trusts (by name),
  2. downloads each filing's EX-102 file (and saves it unchanged in data/raw/ for evidence),
  3. writes one row per loan to `loans` (source "sec_edgar", loan_key "edgar:<trust>:<asset number>").
     The loans table keeps only each loan's MOST RECENT report; every downloaded XML stays in raw_files.

SEC FAIR ACCESS: every request carries our User-Agent with the contact email from .env, and sec.gov is
limited to 4 requests per second (the SEC's cap is 10). All of that is handled by collectors/common/fetch.py.

THE XML TAG NAMES ARE UNVERIFIED (see config/edgar.yaml). After each file the collector prints a
"tag audit" listing any field it found no value for, so a wrong tag name is easy to spot and fix.

Run from the pipeline/ folder:
    python -m collectors.edgar                                   # last 45 days, at most 20 trusts
    python -m collectors.edgar --since 2026-10-01 --max-trusts 5
    python -m collectors.edgar --cik 1234567                     # one specific trust

Manual fallback: save an EX-102 XML by hand into data/inbox/sec_edgar/ with a sidecar
<file>.json like {"notes": "deal=BANK 2026-5YR1"}; the next run parses it.

(Adapted from Neel's edgar.py: the filing selection and XML parsing are his.)
"""
from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator

from collectors.common import config, db, fetch
from collectors.common.log import RunTracker, get_logger, setup_logging
from collectors.common.parse import clean_str, load_yaml, to_date, to_float

COLLECTOR = "edgar.py"
CONFIG_PATH = config.CONFIG_DIR / "edgar.yaml"
log = get_logger("edgar")

_IDX_LINE = re.compile(
    r"^(?P<form>\S+(?: \S+)*?)\s{2,}(?P<name>.+?)\s{2,}(?P<cik>\d+)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<file>\S+)\s*$")


@dataclass
class Filing:
    form: str
    company: str
    cik: str
    filed: str            # YYYY-MM-DD
    accession: str

    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")


# ---------------------------------------------------------------------------
# Finding the filings
# ---------------------------------------------------------------------------
def quarters(since: date, until: date) -> Iterator[tuple[int, int]]:
    """(year, quarter) pairs covering since..until."""
    y, q = since.year, (since.month - 1) // 3 + 1
    while (y, q) <= (until.year, (until.month - 1) // 3 + 1):
        yield y, q
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)


def parse_form_index(text: str) -> list[Filing]:
    """Parse EDGAR's fixed-width form.idx into Filing records."""
    lines = text.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if set(line.strip()) == {"-"}), 0)
    out = []
    for line in lines[start:]:
        m = _IDX_LINE.match(line)
        if m:
            out.append(Filing(m["form"], m["name"].strip(), m["cik"], m["date"], Path(m["file"]).stem))
    return out


def is_cmbs_name(company: str, patterns: list[str]) -> bool:
    return any(re.search(p, company, re.IGNORECASE) for p in patterns)


def limit_trusts(filings: list[Filing], max_trusts: int | None) -> list[Filing]:
    """Keep only `max_trusts` trusts (the ones that filed most recently), with all of their filings."""
    if not max_trusts:
        return filings
    latest: dict[str, str] = {}
    for f in filings:
        latest[f.cik] = max(latest.get(f.cik, ""), f.filed)
    chosen = set(sorted(latest, key=lambda c: latest[c], reverse=True)[:int(max_trusts)])
    return [f for f in filings if f.cik in chosen]


def select_filings(filings: list[Filing], cfg: dict, since: date, until: date,
                   ciks: set[str] | None, max_trusts: int | None = None) -> list[Filing]:
    """Keep ABS-EE filings in the date range from CMBS trusts (or the given CIKs), oldest first."""
    keep = []
    for f in filings:
        if f.form not in cfg["form_types"] or not (since.isoformat() <= f.filed <= until.isoformat()):
            continue
        if ciks:
            if f.cik.lstrip("0") in ciks:
                keep.append(f)
        elif is_cmbs_name(f.company, cfg["cmbs_name_patterns"]):
            keep.append(f)
    return sorted(limit_trusts(keep, max_trusts), key=lambda f: (f.filed, f.cik))


# ---------------------------------------------------------------------------
# Reading the XML
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(elem: ET.Element | None, names: list[str], skip_tag: str | None = None) -> str | None:
    """First non-empty text among `names` (in order of preference) below `elem`, not looking inside `skip_tag`."""
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


def _fraction(value: str | None, threshold: float) -> float | None:
    """0.95 stays 0.95; 95 becomes 0.95 (some files give percents)."""
    num = to_float(value)
    if num is None:
        return None
    return num / 100 if abs(num) > threshold else num


@dataclass
class ParseResult:
    rows: list[dict]
    found: Counter            # field -> how many loans had a value for it
    loans_seen: int
    sample_tags: list[str]    # tag names inside the first loan (to help fix wrong names in the config)


def parse_asset_xml(path: Path, cfg: dict, *, deal_name: str, file_id: str) -> ParseResult:
    """Parse one EX-102 XML file into loans rows (one per loan, first property only)."""
    record_tag, prop_tag, fields = cfg["record_tag"], cfg["property_tag"], cfg["fields"]
    pct_threshold = float(cfg.get("occupancy_percent_threshold", 1.5))
    now = db.utc_now()
    rows: dict[str, dict] = {}
    found: Counter = Counter()
    sample_tags: list[str] = []
    seen = 0
    for _event, elem in ET.iterparse(str(path), events=("end",)):
        if _local(elem.tag) != record_tag:
            continue
        seen += 1
        if not sample_tags:
            sample_tags = sorted({_local(e.tag) for e in elem.iter() if e is not elem})[:60]
        prop = next((c for c in elem.iter() if _local(c.tag) == prop_tag), None)

        def get(target: str) -> str | None:
            """Loan-level tag first, then the loan's first <property>."""
            names = fields.get(target, [])
            return _find_text(elem, names, skip_tag=prop_tag) or _find_text(prop, names)

        for target in fields:
            if get(target) is not None:
                found[target] += 1
        number, period = clean_str(get("loan_number")), to_date(get("report_period"))
        if not number or not period:
            elem.clear()
            continue
        key = f"edgar:{deal_name}:{number}"
        rows[key] = {
            "loan_key": key, "source": "sec_edgar", "deal_name": deal_name,
            "property_name": clean_str(get("property_name")),
            "property_address": clean_str(get("property_address")),
            "property_city": clean_str(get("property_city")),
            "property_state": clean_str(get("property_state")),
            "property_zip": clean_str(get("property_zip")),
            "property_type": clean_str(get("property_type")),
            "current_balance": to_float(get("current_balance")),
            "original_balance": to_float(get("original_balance")),
            "maturity_date": to_date(get("maturity_date")),
            "dscr": to_float(get("dscr")),
            "occupancy": _fraction(get("occupancy"), pct_threshold),
            "payment_status": clean_str(get("payment_status")),
            "servicer_name": clean_str(get("servicer_name")),
            "watchlist_flag": None,          # not in ABS-EE; only Trepp has it
            "servicer_commentary": None,     # not in ABS-EE; only Trepp has it
            "report_period": period, "file_id": file_id, "collected_at": now,
        }
        elem.clear()
    return ParseResult(list(rows.values()), found, seen, sample_tags)


def audit_tags(result: ParseResult, cfg: dict) -> list[str]:
    """Warn about every field for which NO loan had a value (probably a wrong tag name in edgar.yaml)."""
    if result.loans_seen == 0:
        return []
    optional = set(cfg.get("optional_fields", []))
    missing = [f for f in cfg["fields"] if result.found[f] == 0 and f not in optional]
    warnings = []
    if missing:
        warnings.append(f"no value found for {', '.join(missing)} in any of {result.loans_seen} loans; the tag names in "
                        f"config/edgar.yaml may be wrong. Tags present in the first loan: {', '.join(result.sample_tags)}")
    for w in warnings:
        log.warning("TAG AUDIT: %s", w)
    return warnings


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def write_loans(con, result: ParseResult) -> int:
    """Write the rows, keeping only the most recent report per loan. Returns how many were written."""
    return sum(1 for r in result.rows if db.upsert_loan_if_newer(con, r))


def _rows_exist(con, file_id: str) -> bool:
    return con.execute("SELECT 1 FROM loans WHERE file_id = ? LIMIT 1", [file_id]).fetchone() is not None


def _get(url: str, session=None):
    host = url.split("/")[2]
    return fetch.polite_get(url, config.domain_settings(host), session=session)


def process_filing(con, run, filing: Filing, cfg: dict, session=None) -> None:
    """Find the EX-102 file in one filing, download it once, and load its loans."""
    idx_url = cfg["filing_index_url"].format(cik=filing.cik, accession_nodash=filing.accession_nodash)
    resp = _get(idx_url, session)
    if resp is None:
        raise RuntimeError(f"could not read the filing index {idx_url} (blocked, disallowed or not found)")
    items = resp.json().get("directory", {}).get("item", [])
    pattern = re.compile(cfg["asset_file_pattern"], re.IGNORECASE)
    assets = [i for i in items if pattern.search(i.get("name", ""))]
    if not assets:
        log.info("no EX-102 asset file in %s (%s)", filing.accession, filing.company)
        return
    for item in assets:
        size_mb = (to_float(item.get("size")) or 0) / 1e6
        if size_mb > cfg.get("max_file_mb", 200):
            run.error(f"{item['name']}: skipped, {size_mb:.0f} MB is over max_file_mb")
            continue
        url = cfg["file_url"].format(cik=filing.cik, accession_nodash=filing.accession_nodash, name=item["name"])
        known = con.execute("SELECT file_id FROM raw_files WHERE url = ?", [url]).fetchone()
        if known and _rows_exist(con, known[0]):                 # already downloaded and loaded: no new request
            run.skipped()
            continue
        res = fetch.fetch_and_store(
            con, url, config.domain_settings(url.split("/")[2]), source="sec_edgar", doc_category="other",
            collector_name=COLLECTOR, session=session, state=None, ext="xml",
            source_record_id=filing.accession, notes=f"deal={filing.company}; filed={filing.filed}")
        if res is None:
            raise RuntimeError(f"could not download {url}")
        if res.status == "duplicate" and _rows_exist(con, res.file_id):
            run.skipped()
            continue
        load_file(con, run, res.file_id, config.from_stored_path(res.file_path), filing.company, cfg)
        run.new_file() if res.status == "new" else run.skipped()


def load_file(con, run, file_id: str, path: Path, deal_name: str, cfg: dict) -> int:
    result = parse_asset_xml(path, cfg, deal_name=deal_name, file_id=file_id)
    if not result.rows:
        run.error(f"no loan rows parsed from {path.name} ({deal_name}): not CMBS, or the tag names in config/edgar.yaml differ")
        audit_tags(result, cfg)
        return 0
    audit_tags(result, cfg)
    written = write_loans(con, result)
    log.info("%s: %d loans read, %d written", deal_name, len(result.rows), written)
    return written


def load_unparsed_manual_files(con, run, cfg: dict) -> int:
    """
    Parse EX-102 XML files that were saved by hand (data/inbox/sec_edgar/) or whose loans were never
    written because a run crashed. The trust name comes from the notes: "deal=<name>".
    """
    rows = con.execute(
        "SELECT r.file_id, r.file_path, r.notes FROM raw_files r WHERE r.source = 'sec_edgar' AND r.file_type = 'xml' "
        "AND NOT EXISTS (SELECT 1 FROM loans l WHERE l.file_id = r.file_id)").fetchall()
    n = 0
    for file_id, file_path, notes in rows:
        m = re.search(r"deal=([^;]+)", notes or "")
        deal = m.group(1).strip() if m else "UNKNOWN TRUST (manual upload)"
        try:
            n += 1 if load_file(con, run, file_id, config.from_stored_path(file_path), deal, cfg) else 0
        except Exception as e:                                   # a bad file must not stop the rest
            run.error(f"could not parse {file_id}: {e}")
    return n


def run_edgar(con, run, since: date, until: date, *, ciks: set[str] | None = None, max_trusts: int | None = None,
              cfg: dict | None = None, session=None) -> int:
    """Collect every CMBS ABS-EE filing filed between `since` and `until`. Returns the number of filings."""
    cfg = cfg or load_yaml(CONFIG_PATH)
    max_trusts = max_trusts if max_trusts is not None else cfg.get("max_trusts")
    load_unparsed_manual_files(con, run, cfg)

    selected: list[Filing] = []
    for year, q in quarters(since, until):
        url = cfg["index_url"].format(year=year, quarter=q)
        resp = _get(url, session)
        if resp is None:
            run.error(f"could not read the SEC filing index {url}")
            continue
        selected += select_filings(parse_form_index(resp.text), cfg, since, until, ciks, None)
    selected = sorted(limit_trusts(selected, max_trusts), key=lambda f: (f.filed, f.cik))   # limit across all quarters
    log.info("%d CMBS ABS-EE filings selected from %d trusts", len(selected), len({f.cik for f in selected}))

    for filing in selected:
        try:
            process_filing(con, run, filing, cfg, session)
        except Exception as e:                                   # one failing filing must not stop the rest
            run.error(f"{filing.company} {filing.accession}: {e}")
    for domain, reason in fetch.get_fallbacks().items():
        if domain.endswith("sec.gov"):
            run.manual_inbox(f"sec_edgar ({domain})", reason)
    return len(selected)


def collect(con, since: date | None = None, until: date | None = None, ciks=None, max_trusts=None,
            session=None) -> RunTracker:
    """The entry point used by run_all.py."""
    cfg = load_yaml(CONFIG_PATH)
    until = until or date.today()
    since = since or until - timedelta(days=int(cfg.get("lookback_days", 45)))
    with RunTracker(con, COLLECTOR) as run:
        run_edgar(con, run, since, until, ciks=ciks, max_trusts=max_trusts, cfg=cfg, session=session)
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description="Load CMBS loan-level data from SEC EDGAR (Form ABS-EE).")
    parser.add_argument("--since", type=date.fromisoformat, help="first filing date YYYY-MM-DD (default: 45 days ago)")
    parser.add_argument("--until", type=date.fromisoformat, help="last filing date (default: today)")
    parser.add_argument("--cik", action="append", help="only this trust CIK (repeatable); skips the name filter")
    parser.add_argument("--max-trusts", type=int, help="read at most this many trusts (default from edgar.yaml: 20)")
    args = parser.parse_args()
    setup_logging()
    con = db.connect()
    ciks = {c.lstrip("0") for c in args.cik} if args.cik else None
    run = collect(con, args.since, args.until, ciks, args.max_trusts)
    print(f"EDGAR: {run.status}, {run.files_new} new, {run.files_skipped} skipped, {len(run.errors)} errors")


if __name__ == "__main__":
    main()
