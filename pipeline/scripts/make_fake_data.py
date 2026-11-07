"""
make_fake_data.py - fills every table with a small FAKE data set so Parts B, C
and D can start before real data exists.

Everything here is invented. Names, addresses and numbers are not real; every
fake ID starts with "fake-" (or contains FAKE) so it is easy to spot and remove.

It contains:
  * ONE "troubled" Ann Arbor student-housing project (the demo case):
      - a construction mortgage recorded ~3.5 years ago, one modification after it,
        and NO permanent loan and NO release
      - a mechanic's lien from an HVAC contractor filed last month
      - a building permit extended twice, and NO certificate of occupancy
      - a lender (a fake bank) whose nonaccruals rose two quarters in a row
  * 20 ordinary, healthy-looking properties for the scorer to rank against.

Run from the pipeline/ folder:   python scripts/make_fake_data.py
Running it again refreshes the fake rows (real data is never touched).
"""
import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.common import config, db  # noqa: E402
from collectors.common.files import store_file  # noqa: E402

COLLECTOR = "make_fake_data.py"
TODAY = date.today()
NOW = db.utc_now()


# ---------------------------------------------------------------------------
# Cleaning out earlier fake rows so the script can be re-run
# ---------------------------------------------------------------------------
def remove_old_fake_data(con) -> None:
    for (path,) in con.execute("SELECT file_path FROM raw_files WHERE file_id LIKE 'fake-%'").fetchall():
        config.from_stored_path(path).unlink(missing_ok=True)
    con.execute("DELETE FROM raw_files WHERE file_id LIKE 'fake-%'")
    con.execute("DELETE FROM recorded_loans WHERE record_id LIKE 'fake-%'")
    con.execute("DELETE FROM loans WHERE loan_key LIKE '%FAKE%'")
    con.execute("DELETE FROM bank_stats WHERE rssd_id LIKE '9990%'")


# ---------------------------------------------------------------------------
# Small helpers to add rows
# ---------------------------------------------------------------------------
def add_file(con, fid, text, *, source, category, state, county, city, ext="txt",
             record_id=None, notes="FAKE DATA for development") -> str:
    """Save a small fake document to disk and register it in raw_files."""
    body = "*** FAKE DOCUMENT - NOT A REAL RECORD ***\n" + text
    store_file(con, body.encode(), source=source, doc_category=category,
               collector_name=COLLECTOR, state=state, county=county, city=city,
               url=None, source_record_id=record_id, notes=notes, ext=ext, file_id=fid)
    return fid


def add_loan_record(con, rid, fid, state, county, inst, doc_type, kind, rec_date,
                    grantor, grantee, amount=None, parcel=None, address=None,
                    related=None, legal=None) -> None:
    db.insert_row(con, "recorded_loans", {
        "record_id": rid, "file_id": fid, "state": state, "county": county,
        "instrument_number": inst, "document_type": doc_type, "loan_kind": kind,
        "recording_date": rec_date, "grantor": grantor, "grantee": grantee,
        "amount": amount, "parcel_number": parcel, "property_address": address,
        "legal_description": legal, "related_instrument": related, "collected_at": NOW,
    })


def bank_rows(rssd, name, city, state, total_start, ratios, growth=0.01):
    """
    Build 6 quarters of bank_stats rows from a list of nonaccrual ratios.
    Also fills the trend columns (same rules the real ffiec.py will use):
      nonaccrual_change_qoq  = this quarter's ratio minus last quarter's ratio
      rising_two_quarters    = the ratio rose this quarter AND last quarter
    """
    # The most recent quarter-ends that are at least 30 days old, oldest first.
    qends = []
    year, quarter_ends = TODAY.year, [(12, 31), (9, 30), (6, 30), (3, 31)]
    while len(qends) < len(ratios):
        for m, day in quarter_ends:
            q = date(year, m, day)
            if q <= TODAY - timedelta(days=30) and len(qends) < len(ratios):
                qends.append(q)
        year -= 1
    qends = sorted(qends)

    rows, prev_ratio, prev_change = [], None, None
    for i, (qe, ratio) in enumerate(zip(qends, ratios)):
        total = total_start * (1 + growth) ** i
        constr, multi = total * 0.25, total * 0.30
        nonres = total - constr - multi
        nonaccrual = total * ratio
        change = None if prev_ratio is None else ratio - prev_ratio
        rising = bool(change is not None and prev_change is not None and change > 0 and prev_change > 0)
        rows.append({
            "rssd_id": rssd, "bank_name": name, "city": city, "state": state,
            "report_date": qe, "cre_construction": round(constr), "cre_multifamily": round(multi),
            "cre_nonfarm_nonres": round(nonres), "cre_total": round(total),
            "cre_past_due_30_89": round(total * ratio * 0.5),
            "cre_past_due_90_plus": round(total * ratio * 0.4),
            "cre_nonaccrual": round(nonaccrual),
            "cre_nonaccrual_ratio": nonaccrual / total if total else None,
            "nonaccrual_change_qoq": change, "rising_two_quarters": rising,
        })
        prev_ratio, prev_change = ratio, change
    return rows


# ---------------------------------------------------------------------------
# 1. The troubled Ann Arbor student housing project
# ---------------------------------------------------------------------------
def make_troubled_project(con) -> None:
    state, county, city = "MI", "Washtenaw County", "Ann Arbor"
    prop = "FAKE Maize Row Student Lofts"
    address = "1000 Example Campus Way, Ann Arbor, MI 48104"
    parcel = "FAKE-09-12-345-001"
    borrower = "Maize Row Holdings FAKE LLC"
    lender = "First Fakeland Bank"                 # fake bank; its rssd_id is 9990001
    orig_inst = "FAKE-2023-0001234"
    mortgage_date = TODAY - timedelta(days=int(365.25 * 3.5))     # about 3.5 years ago
    mod_date = TODAY - timedelta(days=400)
    lien_date = TODAY - timedelta(days=25)                        # "last month"

    # -- construction mortgage (no permanent loan or release ever follows) --
    f1 = add_file(con, "fake-annarbor-construction-mortgage", (
        f"CONSTRUCTION MORTGAGE\nInstrument: {orig_inst}\nRecorded: {mortgage_date}\n"
        f"Mortgagor (borrower): {borrower}\nMortgagee (lender): {lender}\n"
        f"Principal amount: $38,500,000\nProperty: {prop}, {address}\nParcel: {parcel}\n"
        "Purpose: construction of a 212-bed student housing building.\n"
        "Maturity: 36 months from recording.\n"),
        source="recorder", category="recorded_loan", state=state, county=county,
        city=city, record_id=orig_inst)
    add_loan_record(con, "fake-rl-annarbor-1", f1, state, county, orig_inst,
                    "CONSTRUCTION MORTGAGE", "construction", mortgage_date, borrower, lender,
                    38_500_000, parcel, address)

    # -- one modification (recorded later; refers back to the original) --
    mod_inst = "FAKE-2025-0004321"
    f2 = add_file(con, "fake-annarbor-modification", (
        f"MORTGAGE MODIFICATION AGREEMENT\nInstrument: {mod_inst}\nRecorded: {mod_date}\n"
        f"Modifies mortgage: {orig_inst}\nBorrower: {borrower}\nLender: {lender}\n"
        "Terms: maturity extended 12 months; interest reserve increased.\n"
        f"Parcel: {parcel}\n"),
        source="recorder", category="recorded_loan", state=state, county=county,
        city=city, record_id=mod_inst)
    add_loan_record(con, "fake-rl-annarbor-2", f2, state, county, mod_inst,
                    "MODIFICATION OF MORTGAGE", "modification", mod_date, borrower, lender,
                    None, parcel, address, related=orig_inst)

    # -- mechanic's lien from an HVAC contractor, filed last month --
    lien_inst = "FAKE-2026-0009876"
    add_file(con, "fake-annarbor-hvac-lien", (
        f"CLAIM OF LIEN (CONSTRUCTION LIEN ACT)\nInstrument: {lien_inst}\nRecorded: {lien_date}\n"
        "Claimant: Fakeville Heating & Cooling FAKE Inc. (HVAC contractor)\n"
        f"Owner/Debtor: {borrower}\nProperty: {prop}, {address}\nParcel: {parcel}\n"
        "Amount claimed: $1,240,000 for unpaid HVAC installation work.\n"),
        source="recorder", category="mechanics_lien", state=state, county=county,
        city=city, record_id=lien_inst)

    # -- building permit extended twice; no certificate of occupancy exists --
    issued = TODAY - timedelta(days=1100)
    orig_exp = issued + timedelta(days=540)
    ext1 = issued + timedelta(days=900)
    ext2 = issued + timedelta(days=1280)
    permit = {
        "permit_number": "FAKE-BLD-2023-0456", "address": address,
        "issue_date": issued.isoformat(), "status": "Active - Extended",
        "expiration_date": ext2.isoformat(), "original_expiration_date": orig_exp.isoformat(),
        "project_description": "New construction, 6-story student housing, 212 beds",
        "project_value": 41_000_000,
        "extension_history": [
            {"approved": (issued + timedelta(days=520)).isoformat(), "new_expiration": ext1.isoformat()},
            {"approved": (issued + timedelta(days=880)).isoformat(), "new_expiration": ext2.isoformat()},
        ],
        "note": "FAKE DATA",
    }
    add_file(con, "fake-annarbor-permit", json.dumps(permit, indent=2), source="permit",
             category="building_permit", state=state, county=county, city=city, ext="json",
             record_id=permit["permit_number"])

    # -- the lender and its worsening nonaccruals (rising 3 quarters in a row) --
    for r in bank_rows("9990001", lender, "Fakeland", "MI", 420_000_000,
                       [0.012, 0.011, 0.014, 0.021, 0.032, 0.046]):
        db.insert_row(con, "bank_stats", r)


# ---------------------------------------------------------------------------
# 2. Twenty ordinary, healthy-looking properties
# ---------------------------------------------------------------------------
PLACES = [("MI", "Washtenaw County", "Ann Arbor"), ("MI", "Wayne County", "Detroit"),
          ("IL", "Cook County", "Chicago"), ("AZ", "Maricopa County", "Phoenix"),
          ("TX", "Travis County", "Austin"), ("FL", "Miami-Dade County", "Miami")]
KINDS = ["Garden Apartments", "Shopping Center", "Industrial Warehouse", "Medical Office",
         "Self Storage", "Hotel", "Senior Living", "Student Housing"]
LENDERS = ["Sample Community Bank", "Fiction Federal Bank", "Example Savings & Loan",
           "Placeholder Trust Bank", "Imaginary National Bank"]


def make_healthy_properties(con) -> None:
    rng = random.Random(42)           # fixed seed: same fake data every time
    # Healthy banks: low, flat or falling nonaccruals (never two rises in a row).
    for i, name in enumerate(LENDERS, start=2):
        base = 0.003 + 0.001 * i
        wobble = [base, base * 0.95, base * 1.03, base * 0.97, base * 1.02, base * 0.99]
        for r in bank_rows(f"999000{i}", name, "Faketown", rng.choice(["MI", "IL", "TX"]),
                           rng.randint(300, 900) * 1_000_000, wobble):
            db.insert_row(con, "bank_stats", r)

    xml_id = add_file(con, "fake-edgar-trust-xml", "<fake-abs-ee>placeholder</fake-abs-ee>",
                      source="sec_edgar", category="other", state=None, county=None, city=None,
                      ext="xml", notes="FAKE DATA: stands in for an EX-102 file")

    for n in range(1, 21):
        state, county, city = rng.choice(PLACES)
        kind = rng.choice(KINDS)
        prop = f"FAKE {kind} #{n:02d}"
        address = f"{rng.randint(100, 9999)} Sample Street, {city}, {state}"
        parcel = f"FAKE-{n:04d}"
        borrower = f"Example Property {n:02d} FAKE LLC"
        lender = rng.choice(LENDERS)
        amount = rng.randint(5, 60) * 1_000_000

        def make_record(tag, doc_type, loan_kind, days_ago, related=None, amt=amount):
            inst = f"FAKE-H{n:02d}-{tag}"
            rec = TODAY - timedelta(days=days_ago)
            fid = add_file(con, f"fake-healthy-{n:02d}-{tag.lower()}", (
                f"{doc_type}\nInstrument: {inst}\nRecorded: {rec}\nBorrower: {borrower}\n"
                f"Lender: {lender}\nAmount: {f'${amt:,}' if amt else 'n/a'}\nProperty: {prop}, {address}\n"
                + (f"Refers to: {related}\n" if related else "")),
                source="recorder", category="recorded_loan", state=state, county=county,
                city=city, record_id=inst)
            add_loan_record(con, f"fake-rl-healthy-{n:02d}-{tag.lower()}", fid, state, county,
                            inst, doc_type, loan_kind, rec, borrower, lender, amt, parcel,
                            address, related=related)
            return inst

        if n % 2 == 0:
            # A construction loan that was properly replaced by a permanent loan, then released.
            c_inst = make_record("C", "CONSTRUCTION MORTGAGE", "construction", rng.randint(1700, 2200))
            make_record("P", "MORTGAGE", "permanent", rng.randint(700, 1100), amt=amount + 2_000_000)
            make_record("R", "DISCHARGE OF MORTGAGE", "release", rng.randint(600, 690), related=c_inst, amt=None)
        else:
            make_record("P", "MORTGAGE", "permanent", rng.randint(400, 2500))

        # About 60% have a permit that finished normally with a certificate of occupancy.
        if n % 5 in (0, 1, 2):
            issued = TODAY - timedelta(days=rng.randint(1000, 1600))
            expires = issued + timedelta(days=540)
            permit = {"permit_number": f"FAKE-BLD-H{n:02d}", "address": address,
                      "issue_date": issued.isoformat(), "status": "Finaled",
                      "expiration_date": expires.isoformat(),
                      "project_description": f"{kind} - FAKE", "project_value": amount, "note": "FAKE DATA"}
            add_file(con, f"fake-healthy-{n:02d}-permit", json.dumps(permit), source="permit",
                     category="building_permit", state=state, county=county, city=city,
                     ext="json", record_id=permit["permit_number"])
            co = {"permit_number": permit["permit_number"], "address": address,
                  "issue_date": (issued + timedelta(days=480)).isoformat(),
                  "status": "Issued", "note": "FAKE DATA"}
            add_file(con, f"fake-healthy-{n:02d}-co", json.dumps(co), source="permit",
                     category="certificate_of_occupancy", state=state, county=county, city=city,
                     ext="json", record_id=permit["permit_number"])

        # About half also appear as CMBS loans in good standing.
        if n % 2 == 1:
            db.insert_row(con, "loans", {
                "loan_key": f"edgar:FAKETRUST-2022-C{n:02d}:{n:04d}", "source": "sec_edgar",
                "deal_name": f"FAKE Trust 2022-C{n:02d}", "property_name": prop,
                "property_address": address.split(",")[0], "property_city": city,
                "property_state": state, "property_zip": "00000",
                "property_type": kind, "current_balance": amount * 0.95, "original_balance": amount,
                "maturity_date": TODAY + timedelta(days=rng.randint(400, 2500)),
                "dscr": round(rng.uniform(1.35, 2.2), 2), "occupancy": round(rng.uniform(0.9, 0.99), 3),
                "payment_status": "Current", "servicer_name": "Example Servicing FAKE",
                "watchlist_flag": None, "servicer_commentary": None,
                "report_period": date(TODAY.year, TODAY.month, 1) - timedelta(days=1),
                "file_id": xml_id, "collected_at": NOW,
            }, replace=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill the database with a small FAKE data set.")
    parser.add_argument("--db", help="database file (default: data/bloomfield.duckdb)")
    args = parser.parse_args()

    con = db.connect(args.db)
    remove_old_fake_data(con)
    make_troubled_project(con)
    make_healthy_properties(con)
    print("Fake data loaded:")
    for t in ("raw_files", "recorded_loans", "loans", "bank_stats"):
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"  {t:<16} {n} rows")


if __name__ == "__main__":
    main()
