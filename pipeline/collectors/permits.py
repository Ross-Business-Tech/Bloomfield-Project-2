"""
permits.py - building permits, inspections and certificates of occupancy (Part A).

NETR Online does not list building departments, so permit data comes from each CITY's own
building department (open-data portal, permit search export, or files a person drops in the inbox):

    data/inbox/permit/<ST>/<county>/building_permit/...          permits (may include completed dates)
    data/inbox/permit/<ST>/<county>/permit_inspection/...        inspection results
    data/inbox/permit/<ST>/<county>/certificate_of_occupancy/... certificates of occupancy

or a county file's `permits:` source with access: open_data_api (see config/counties/_template.yaml).

This step reads those permit files (CSV, or JSON rows saved by collectors/county.py) and fills:
    permit_inspections  (permit_number, inspection_date, ...)  -> I  site inactivity
    filing_fields       building_permit rows                   -> I  (which permit belongs to which property)
    filing_fields       certificate_of_occupancy rows          -> "opened but construction loan still outstanding",
                                                                  stale-opening check, applicability
Structured permit rows need no AI reading, so they skip the Reader.

Column names: the BLDS open permit-data standard by default (PermitNum, IssuedDate, CompletedDate,
InspectionDate, ...), with aliases for common variants. A county file can override them:
    sources: {permits: {columns: {permit_number: "Permit #", issued_date: "Issue Dt"}}}

Residential permits are skipped (Bloomfield buys commercial / multifamily / senior housing).

Run:  python -m collectors.permits            (normally run by collectors.run_all, after "county")
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
from datetime import date

from collectors import county as county_mod
from collectors.common import config, db
from collectors.common.log import RunTracker, get_logger, setup_logging
from collectors.common.parse import to_date

log = get_logger("permits")
COLLECTOR = "permits.py"

# BLDS names first, then common variants. Matching ignores case, spaces and punctuation.
ALIASES = {
    "permit_number": ["PermitNum", "permit_number", "permit_no", "permitnumber", "permit_id", "Permit", "RecordNumber"],
    "issued_date": ["IssuedDate", "issue_date", "date_issued", "issued", "IssueDate"],
    "applied_date": ["AppliedDate", "application_date", "applied"],
    "completed_date": ["CompletedDate", "FinaledDate", "final_date", "co_date", "CertificateOfOccupancyDate",
                       "co_issued_date", "completed"],
    "inspection_date": ["InspectionDate", "InspectedDate", "inspection_date", "insp_date", "ResultDate"],
    "inspection_type": ["InspectionType", "InspType", "inspection_type"],
    "result": ["Result", "InspectionResult", "inspection_result"],
    "status": ["StatusCurrent", "status", "permit_status", "CurrentStatus"],
    "address": ["OriginalAddress1", "address", "property_address", "site_address", "OriginalAddress", "location"],
    "city": ["OriginalCity", "city"],
    "state": ["OriginalState", "state"],
    "zip": ["OriginalZip", "zip", "zipcode"],
    "permit_class": ["PermitClass", "PermitClassMapped", "permit_class", "work_class", "class"],
    "permit_type": ["PermitType", "PermitTypeMapped", "permit_type", "type", "Description"],
    "valuation": ["EstProjectCost", "valuation", "job_value", "project_value", "total_valuation"],
}
RESIDENTIAL = re.compile(r"\b(residential|single[- ]family|duplex|townhouse|r-?3)\b", re.I)


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def column_map(headers, overrides: dict | None = None) -> dict:
    """Our field name -> the file's column name."""
    by_key = {_key(h): h for h in headers}
    out = {}
    for field, names in ALIASES.items():
        wanted = ([overrides[field]] if overrides and overrides.get(field) else []) + names
        for n in wanted:
            if _key(n) in by_key:
                out[field] = by_key[_key(n)]
                break
    return out


def read_rows(raw: bytes, ext: str) -> list[dict]:
    """CSV, a JSON list, or one JSON row (what county.py saves for each open-data record)."""
    text = raw.decode("utf-8-sig", errors="replace")
    if ext.lower().lstrip(".") == "json" or text.lstrip().startswith(("{", "[")):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("features") or data.get("data") or [data]
        return [r.get("attributes", r) if isinstance(r, dict) else {} for r in data]
    return list(csv.DictReader(io.StringIO(text)))


def _get(row, cols, field):
    col = cols.get(field)
    v = row.get(col) if col else None
    return v.strip() if isinstance(v, str) else v


def _date(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) and v > 10_000_000_000:          # ArcGIS epoch milliseconds
        return date.fromtimestamp(v / 1000)
    return to_date(v)


def county_overrides(state: str | None, county_name: str | None) -> dict:
    for cfg in county_mod.load_configs(state=state):
        if cfg.get("county") == county_name:
            return ((cfg.get("sources") or {}).get("permits") or {}).get("columns") or {}
    return {}


def load_file(con, f: dict) -> dict:
    """Turn one permit file into permit_inspections / filing_fields rows. Returns counts."""
    from scout_reader_schema import create_scout_reader_tables, upsert
    from scorer.equation import create_equation_tables

    create_scout_reader_tables(con)
    create_equation_tables(con)
    path = config.from_stored_path(f["file_path"])
    rows = read_rows(path.read_bytes(), path.suffix)
    counts = {"permits": 0, "inspections": 0, "certificates": 0, "skipped_residential": 0}
    if not rows:
        return counts
    cols = column_map(rows[0].keys(), county_overrides(f.get("state"), f.get("county")))
    category = f.get("doc_category") or "building_permit"
    for i, row in enumerate(rows):
        label = f"{_get(row, cols, 'permit_class') or ''} {_get(row, cols, 'permit_type') or ''}"
        if RESIDENTIAL.search(label):
            counts["skipped_residential"] += 1
            continue
        num = _get(row, cols, "permit_number")
        address = _get(row, cols, "address")
        st = (_get(row, cols, "state") or f.get("state") or "").upper() or None
        city = _get(row, cols, "city") or f.get("city")
        base = {"property_address": address, "property_city": city, "property_state": st,
                "permit_number": num, "status": "ok", "confidence": "high",
                "model_name": "structured (no AI needed)", "prompt_version": "permits.py",
                "extracted_at": db.utc_now(), "quotes": json.dumps({"row": json.dumps(row, default=str)[:500]})}
        insp_d = _date(_get(row, cols, "inspection_date"))
        if insp_d and num:
            con.execute("INSERT INTO permit_inspections (permit_number, state, city, inspection_date, inspection_type, "
                        "result, file_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [num, st, city, insp_d, _get(row, cols, "inspection_type"), _get(row, cols, "result"),
                         f["file_id"]])
            counts["inspections"] += 1
        issued = _date(_get(row, cols, "issued_date")) or _date(_get(row, cols, "applied_date"))
        if num and issued and category != "certificate_of_occupancy":
            kind = " · ".join(str(x) for x in (_get(row, cols, "permit_class"), _get(row, cols, "permit_type")) if x)
            status = " · ".join(str(x) for x in (_get(row, cols, "status"), kind) if x)   # keeps "New Building" etc.
            upsert(con, "filing_fields", {**base, "file_id": f"{f['file_id']}:permit:{num}",
                                          "filing_type": "building_permit", "filing_date": issued,
                                          "permit_status": status})
            counts["permits"] += 1
        done = _date(_get(row, cols, "completed_date"))
        if category == "certificate_of_occupancy":
            done = done or issued
        if done:
            upsert(con, "filing_fields", {**base, "file_id": f"{f['file_id']}:co:{num or i}",
                                          "filing_type": "certificate_of_occupancy", "filing_date": done})
            counts["certificates"] += 1
    return counts


def collect(con) -> RunTracker:
    """Load every permit file not loaded yet (from the inbox or county.py's permits sources)."""
    with RunTracker(con, COLLECTOR) as run:
        # Structured files only; scanned permits / certificates (PDF, images) go to the AI Reader instead.
        cur = con.execute("SELECT * FROM raw_files WHERE source = 'permit' "
                          "AND lower(coalesce(file_type, '')) IN ('csv', 'json') ORDER BY collected_at")
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
    argparse.ArgumentParser(description="Load building permits, inspections and certificates of occupancy.").parse_args(argv)
    setup_logging()
    con = db.connect()
    run = collect(con)
    print(f"permits.py: {run.summary() if hasattr(run, 'summary') else 'done'}")


if __name__ == "__main__":
    main()
