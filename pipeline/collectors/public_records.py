"""
public_records.py - general public records behind the creative signals (Part A).

Every one of these exists for ANY property and ANY kind of loan (CMBS, Fannie / Freddie, bank, bridge,
construction), and every one is public:

    source folder (inbox)        what it is                                 table                -> signal
    tax_collector                county treasurer / tax collector bills      property_tax_status  -> T unpaid property tax
                                                                                                     (a tax lien / sale -> late)
    assessor                     county assessor roll, value per tax year    assessed_values      -> W value cut (fallback)
                                 (a roll summary with no address = the       county_assessment_trend
                                  county's total, for comparison)
    code_enforcement             city code / fire / health violations        compliance_records   -> U compliance trouble
    listing                      space listed for lease (units or SF)        space_availability   -> Q occupancy slide (fallback)

A person (or county.py, from a county file's `tax:` / `assessor:` / `code:` sources) saves the county's or
city's CSV / JSON export in:

    data/inbox/<source>/<ST>/<county>/...        e.g. data/inbox/tax_collector/FL/pinellas/delinquent_2026.csv

and this step turns the rows into table rows. Structured rows need no AI reading. Column names vary by
county, so common spellings are matched (case, spaces and punctuation ignored).

Run:  python -m collectors.public_records      (normally run by collectors.run_all, after "permits")
"""
from __future__ import annotations

import argparse
import json
import re

from collectors.common import config, db
from collectors.common.log import RunTracker, get_logger, setup_logging
from collectors.common.parse import to_date
from collectors.permits import _key, read_rows

log = get_logger("public_records")
COLLECTOR = "public_records.py"

COMMON = {
    "address": ["address", "property_address", "site_address", "situs_address", "situs", "location",
                "OriginalAddress1", "street_address", "prop_address"],
    "city": ["city", "situs_city", "property_city", "OriginalCity"],
    "state": ["state", "situs_state", "property_state", "OriginalState"],
    "county": ["county", "county_name"],
    "parcel": ["parcel", "parcel_id", "parcel_number", "apn", "pin", "folio", "account", "account_number"],
}
ALIASES = {
    "tax_collector": {**COMMON,
                      "tax_year": ["tax_year", "year", "roll_year", "bill_year"],
                      "amount_due": ["amount_due", "balance_due", "balance", "unpaid", "delinquent_amount",
                                     "total_due", "amount_owed"],
                      "due_date": ["due_date", "delinquent_date", "installment_due", "date_due"],
                      "status": ["status", "payment_status", "tax_status", "bill_status"],
                      "as_of_date": ["as_of_date", "as_of", "report_date", "extract_date"]},
    "assessor": {**COMMON,
                 "tax_year": ["tax_year", "year", "roll_year", "assessment_year"],
                 "value": ["value", "market_value", "just_value", "assessed_value", "total_value",
                           "appraised_value", "taxable_value", "full_cash_value"]},
    "code_enforcement": {**COMMON,
                         "record_date": ["record_date", "date", "violation_date", "case_date", "opened_date",
                                         "date_opened", "inspection_date", "cited_date"],
                         "kind": ["kind", "type", "case_type", "violation_type", "category"],
                         "status": ["status", "case_status", "violation_status"],
                         "description": ["description", "violation", "violation_description", "details", "comments"],
                         "fine_amount": ["fine_amount", "fine", "penalty", "amount"]},
    "listing": {**COMMON,
                "observed_at": ["observed_at", "date", "listing_date", "as_of", "scraped_at"],
                "available": ["available", "available_sf", "available_units", "space_available", "vacant"],
                "total": ["total", "building_sf", "total_sf", "total_units", "rba", "units", "size"],
                "unit_basis": ["unit_basis", "basis", "unit"],
                "source_url": ["source_url", "url", "link"]},
}
TABLE = {"tax_collector": "property_tax_status", "assessor": "assessed_values",
         "code_enforcement": "compliance_records", "listing": "space_availability"}
DATES = {"due_date", "as_of_date", "record_date", "observed_at"}
NUMBERS = {"amount_due", "value", "fine_amount", "available", "total", "tax_year"}


def column_map(headers, aliases: dict) -> dict:
    by_key = {_key(h): h for h in headers}
    out = {}
    for field, names in aliases.items():
        for n in [field] + names:
            if _key(n) in by_key:
                out[field] = by_key[_key(n)]
                break
    return out


def _num(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[,$\s]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return None


def load_file(con, f: dict) -> dict:
    """Turn one public-records file into table rows. Returns counts."""
    from scorer.equation import create_equation_tables

    create_equation_tables(con)
    source = f["source"]
    path = config.from_stored_path(f["file_path"])
    rows = read_rows(path.read_bytes(), path.suffix)
    counts = {"rows": 0, "county_totals": 0, "skipped": 0}
    if not rows:
        return counts
    cols = column_map(rows[0].keys(), ALIASES[source])
    table = TABLE[source]
    for row in rows:
        rec = {}
        for field, col in cols.items():
            v = row.get(col)
            v = v.strip() if isinstance(v, str) else v
            if field in DATES:
                v = to_date(v) if v not in (None, "") else None
            elif field in NUMBERS:
                v = _num(v)
                if field == "tax_year" and v is not None:
                    v = int(v)
            rec[field] = v if v != "" else None
        rec["state"] = (rec.get("state") or f.get("state") or "").upper() or None
        rec["city"] = rec.get("city") or f.get("city")
        if source in ("tax_collector", "assessor"):
            rec["county"] = rec.get("county") or f.get("county")
        if source == "assessor" and not rec.get("address") and not rec.get("parcel"):
            # A roll summary (county total per year): used to compare each property with its county.
            if rec.get("tax_year") is not None and rec.get("value"):
                con.execute("INSERT INTO county_assessment_trend (state, county, tax_year, total_value, source) "
                            "VALUES (?, ?, ?, ?, ?)", [rec["state"], rec.get("county"), rec["tax_year"], rec["value"],
                                                       f"{source}:{f['file_id']}"])
                counts["county_totals"] += 1
            else:
                counts["skipped"] += 1
            continue
        if not rec.get("address"):
            counts["skipped"] += 1
            continue
        rec["file_id"] = f["file_id"]
        if source != "listing":
            rec["source"] = source
        keep = {k: v for k, v in rec.items() if k in _columns(con, table)}
        names = list(keep)
        con.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                    [keep[n] for n in names])
        counts["rows"] += 1
    return counts


def _columns(con, table: str) -> set[str]:
    return {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                                      [table]).fetchall()}


def collect(con) -> RunTracker:
    """Load every structured public-records file not loaded yet."""
    with RunTracker(con, COLLECTOR) as run:
        marks = ", ".join("?" for _ in TABLE)
        cur = con.execute(f"SELECT * FROM raw_files WHERE source IN ({marks}) "
                          "AND lower(coalesce(file_type, '')) IN ('csv', 'json') ORDER BY collected_at", list(TABLE))
        names = [d[0] for d in cur.description]
        for row in cur.fetchall():
            f = dict(zip(names, row))
            key = f"loaded:{f['file_id']}"
            if db.get_state(con, COLLECTOR, key):
                run.skipped()
                continue
            try:
                counts = load_file(con, f)
                db.set_state(con, COLLECTOR, key, json.dumps(counts))
                run.new_file()
                log.info("%s: %s", f["file_id"], counts)
            except Exception as e:                       # one bad file must not stop the rest
                run.error(f"{f['file_id']}: {e}")
    return run


def main(argv=None):
    argparse.ArgumentParser(description="Load county tax, assessor, code-violation and listing files.").parse_args(argv)
    setup_logging()
    con = db.connect()
    run = collect(con)
    print(f"public_records.py: {run.summary() if hasattr(run, 'summary') else 'done'}")


if __name__ == "__main__":
    main()
