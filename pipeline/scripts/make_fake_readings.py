"""
make_fake_readings.py - pretend the AI Reader (Part B) already read the fake documents,
so Part D (the Scorer) can be built and demoed without an API key or any cost.

Run AFTER make_fake_data.py, from the pipeline/ folder:
    python scripts/make_fake_data.py
    python scripts/make_fake_readings.py
    python -m scorer

It:
  * reads the fake liens, permits and certificates of occupancy in raw_files and writes
    the filing_fields rows the Reader would have produced (model_name = "fake-reader");
  * adds the Scout's project row for the troubled Ann Arbor project (stale opening date);
  * adds two FAKE "middle of the pack" properties so the ranking has a watch tier:
      - a Phoenix office whose CMBS loan is on the watchlist, DSCR under 1, maturing soon
      - a Chicago hotel with a lis pendens filed against it
Everything is invented; every ID contains "fake". Re-running replaces these rows.
"""
import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.common import config, db  # noqa: E402
from collectors.common.files import store_file  # noqa: E402
from scout_reader_schema import open_db, upsert  # noqa: E402

TODAY = date.today()
NOW = db.utc_now()
COLLECTOR = "make_fake_readings.py"


def _field(text: str, label: str) -> str | None:
    m = re.search(rf"^{label}[^:]*:\s*(.+)$", text, re.M | re.I)
    return m.group(1).strip() if m else None


def _money(s: str | None) -> float | None:
    m = re.search(r"\$([\d,]+)", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def _split_address(addr: str | None):
    """'1000 Example Campus Way, Ann Arbor, MI 48104' -> (street, city, state, zip)."""
    parts = [p.strip() for p in (addr or "").split(",")]
    street = parts[0] if parts else None
    city = parts[1] if len(parts) > 1 else None
    st, zp = None, None
    if len(parts) > 2:
        bits = parts[2].split()
        st = bits[0] if bits else None
        zp = bits[1] if len(bits) > 1 else None
    return street, city, st, zp


def base_row(file_id: str, filing_type: str) -> dict:
    return {"file_id": file_id, "filing_type": filing_type, "confidence": "high", "status": "ok",
            "model_name": "fake-reader", "prompt_version": "fake", "extracted_at": NOW,
            "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0}


def read_lien(file_id: str, text: str) -> dict:
    prop = _field(text, "Property") or ""
    address = prop.split(", ", 1)[1] if ", " in prop else prop
    street, city, st, zp = _split_address(address)
    claimant = _field(text, "Claimant")
    amount_line = _field(text, "Amount claimed")
    row = base_row(file_id, "mechanics_lien")
    row.update({
        "borrower_name": _field(text, "Owner"), "project_name": prop.split(",")[0],
        "property_address": address, "property_city": city, "property_state": st, "property_zip": zp,
        "parcel_number": _field(text, "Parcel"), "lien_claimant": claimant.split(" (")[0] if claimant else None,
        "lien_amount": _money(amount_line), "lien_work": "HVAC installation" if "HVAC" in text else None,
        "recording_date": date.fromisoformat(_field(text, "Recorded")),
        "distress_tags": json.dumps(["contractor_unpaid"]),
        "quotes": json.dumps({"lien_amount": f"Amount claimed: {amount_line}", "lien_claimant": f"Claimant: {claimant}"}),
    })
    return row


def read_permit(file_id: str, doc: dict, category: str, recorder_parcels: dict) -> dict:
    street, city, st, zp = _split_address(doc.get("address"))
    row = base_row(file_id, category)
    tags = ["permit_extended"] if doc.get("extension_history") else []
    row.update({
        "property_address": doc.get("address"), "property_city": city, "property_state": st,
        "parcel_number": recorder_parcels.get((doc.get("address") or "").split(",")[0].lower()),
        "permit_number": doc.get("permit_number"), "permit_status": doc.get("status"),
        "permit_expiration_date": date.fromisoformat(doc["expiration_date"]) if doc.get("expiration_date") else None,
        "filing_date": date.fromisoformat(doc["issue_date"]) if doc.get("issue_date") else None,
        "distress_tags": json.dumps(tags),
        "quotes": json.dumps({"permit_status": f'"status": "{doc.get("status")}"'}),
    })
    return row


def fake_readings(con) -> int:
    con.execute("DELETE FROM filing_fields WHERE model_name = 'fake-reader'")
    # The Reader often can't see a parcel number on a permit; we borrow the recorder's so the
    # fallback grouping works the same way Part C would.
    recorder_parcels = {(a or "").split(",")[0].lower(): p for a, p in con.execute(
        "SELECT property_address, parcel_number FROM recorded_loans WHERE record_id LIKE 'fake-%'").fetchall()}
    n = 0
    for fid, category, path in con.execute("""
            SELECT file_id, doc_category, file_path FROM raw_files
            WHERE file_id LIKE 'fake-%' AND doc_category IN
                  ('mechanics_lien', 'building_permit', 'certificate_of_occupancy')""").fetchall():
        text = config.from_stored_path(path).read_text(encoding="utf-8")
        if category == "mechanics_lien":
            row = read_lien(fid, text)
        else:
            body = text.split("\n", 1)[1]          # drop the "*** FAKE DOCUMENT ***" banner
            row = read_permit(fid, json.loads(body), category, recorder_parcels)
        upsert(con, "filing_fields", row)
        n += 1
    return n


def fake_scout_project(con) -> None:
    upsert(con, "projects", {
        "project_id": "fake-proj-maize-row", "project_name": "FAKE Maize Row Student Lofts",
        "developer_name": "Maize Row Holdings FAKE LLC",
        "property_address": "1000 Example Campus Way, Ann Arbor, MI 48104",
        "city": "Ann Arbor", "state": "MI", "county": "Washtenaw County",
        "property_type": "student_housing", "units_or_beds": 212,
        "main_url": "https://example.org/fake-maize-row", "first_seen_at": NOW,
        "stated_opening_phrase": "Now leasing for Fall 2025",
        "opening_deadline": date(TODAY.year - 1, 8, 31),
        "page_still_unopened": True, "stale_candidate": True, "months_overdue": 14.0,
        "missed_lease_up": True, "date_change_count": 2, "first_stated_phrase": "Opening Fall 2024",
        "last_checked_at": NOW, "check_status": "ok", "notes": "FAKE DATA",
    })


def fake_middle_properties(con) -> None:
    # 1) Phoenix office: struggling CMBS loan.
    db.insert_row(con, "loans", {
        "loan_key": "edgar:FAKETRUST-2019-C77:0077", "source": "sec_edgar",
        "deal_name": "FAKE Trust 2019-C77", "property_name": "FAKE Camelback Office Plaza",
        "property_address": "4400 Sample Avenue", "property_city": "Phoenix",
        "property_state": "AZ", "property_zip": "00000", "property_type": "Office",
        "current_balance": 27_400_000, "original_balance": 29_000_000,
        "maturity_date": TODAY + timedelta(days=240), "dscr": 0.88, "occupancy": 0.71,
        "payment_status": "Current", "servicer_name": "Example Servicing FAKE",
        "watchlist_flag": True,
        "servicer_commentary": "FAKE: Largest tenant (32% NRA) vacating at lease expiration; borrower exploring options.",
        "report_period": TODAY.replace(day=1) - timedelta(days=1), "file_id": "fake-edgar-trust-xml",
        "collected_at": NOW,
    }, replace=True)

    # 2) Chicago hotel: lis pendens filed by its lender.
    fid = "fake-chicago-lis-pendens"
    con.execute("DELETE FROM raw_files WHERE file_id = ?", [fid])
    filed = TODAY - timedelta(days=60)
    store_file(con, (
        "*** FAKE DOCUMENT - NOT A REAL RECORD ***\nNOTICE OF LIS PENDENS\n"
        f"Case No. FAKE-2026-CH-01234\nFiled: {filed}\nPlaintiff: Placeholder Trust Bank\n"
        "Defendant: Example Lakeside Hotel FAKE LLC\n"
        "Property: FAKE Lakeside Hotel, 77 Example Drive, Chicago, IL 60601\n").encode(),
        source="county_court", doc_category="lis_pendens", collector_name=COLLECTOR,
        state="IL", county="Cook County", city="Chicago", url=None,
        source_record_id="FAKE-2026-CH-01234", notes="FAKE DATA for development", ext="txt", file_id=fid)
    row = base_row(fid, "lis_pendens")
    row.update({
        "borrower_name": "Example Lakeside Hotel FAKE LLC", "project_name": "FAKE Lakeside Hotel",
        "property_address": "77 Example Drive, Chicago, IL 60601", "property_city": "Chicago",
        "property_state": "IL", "lender_name": "Placeholder Trust Bank", "property_type": "hotel",
        "case_number": "FAKE-2026-CH-01234", "filing_date": filed,
        "distress_tags": json.dumps(["lender_lawsuit"]),
        "quotes": json.dumps({"filing_type": "NOTICE OF LIS PENDENS", "case_number": "Case No. FAKE-2026-CH-01234"}),
    })
    upsert(con, "filing_fields", row)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fake Reader output so the Scorer can be demoed.")
    ap.add_argument("--db", help="database file (default: data/bloomfield.duckdb)")
    args = ap.parse_args()
    con = open_db(args.db)
    if not con.execute("SELECT count(*) FROM raw_files WHERE file_id LIKE 'fake-annarbor-%'").fetchone()[0]:
        sys.exit("Run python scripts/make_fake_data.py first.")
    n = fake_readings(con)
    fake_scout_project(con)
    fake_middle_properties(con)
    print(f"Fake readings: {n} documents 'read', 1 Scout project, 2 extra watch-list properties.")
    print("Next: python -m scorer")


if __name__ == "__main__":
    main()
