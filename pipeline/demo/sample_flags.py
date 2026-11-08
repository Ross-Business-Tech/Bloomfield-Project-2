"""
sample_flags.py - adds a larger set of INVENTED flagged properties to the demo database, so the
demo shows a realistic ranked list (about 10 Tier 1 and 14 Tier 2 properties) instead of one.

Run by the demo builder after make_fake_readings.py and before the matcher:
    python demo/sample_flags.py

Everything is made up. Every id starts with "fake-flag-", every name contains "FAKE", and every
reading says model "sample-reader". Quotes are copied word-for-word from the sample documents,
exactly as the real AI Reader is required to do. Re-running replaces these rows.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.common import db  # noqa: E402
from collectors.common.files import store_file  # noqa: E402
from scout_reader_schema import open_db, upsert  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from make_fake_data import bank_rows  # noqa: E402

TODAY = date.today()
NOW = db.utc_now()
COLLECTOR = "sample_flags.py"
MODEL = "sample-reader"
BANNER = "*** FAKE DOCUMENT - NOT A REAL RECORD ***\n"

PLACES = {
    "Tempe": ("AZ", "Maricopa County", "85281"), "Phoenix": ("AZ", "Maricopa County", "85004"),
    "Tucson": ("AZ", "Pima County", "85701"), "Austin": ("TX", "Travis County", "78701"),
    "Houston": ("TX", "Harris County", "77002"), "Dallas": ("TX", "Dallas County", "75201"),
    "Miami": ("FL", "Miami-Dade County", "33131"), "Orlando": ("FL", "Orange County", "32801"),
    "Chicago": ("IL", "Cook County", "60607"), "Champaign": ("IL", "Champaign County", "61820"),
    "Detroit": ("MI", "Wayne County", "48226"), "Ypsilanti": ("MI", "Washtenaw County", "48197"),
    "Lansing": ("MI", "Ingham County", "48933"), "Grand Rapids": ("MI", "Kent County", "49503"),
    "Tampa": ("FL", "Hillsborough County", "33602"), "San Antonio": ("TX", "Bexar County", "78205"),
}

# Lenders we invent (rssd ids start 9990 so the demo cleanup recognises them).
BANKS = {
    "Lakeshore Fakeland Bank": ("9990011", "Fakeland", "MI", [0.011, 0.013, 0.017, 0.024, 0.033, 0.041]),
    "Sunbelt Example Bank": ("9990012", "Exampleton", "AZ", [0.015, 0.016, 0.020, 0.026, 0.031, 0.038]),
    "Gulf Placeholder Trust": ("9990013", "Placeville", "TX", [0.009, 0.010, 0.012, 0.014, 0.019, 0.025]),
    "Prairie Sample Savings": ("9990014", "Sampleburg", "IL", [0.010, 0.010, 0.011, 0.010, 0.011, 0.010]),
}

# Each property: id, name, street, city, type, units, borrower, lender, then evidence tags.
#   loan:   (months ago, amount, number of modifications)
#   lien:   (amount, days ago, claimant, work)
#   permit: (issued days ago, extensions, expired?)
#   proj:   (months overdue, missed lease-up?, date changes, stated phrase)
#   cmbs:   dict(dscr, occ, watch, status, maturity_days, balance)
#   legal:  list of (filing_type, tag, days ago, filer/extra)
P = [
    # ------------------------------- Tier 1 -------------------------------
    dict(id="tempe-sun-devil", name="FAKE Sun Devil Crossing Student Lofts", street="410 Example Mill Ave", city="Tempe",
         ptype="student_housing", units=288, borrower="Sun Devil Crossing FAKE LLC", lender="Sunbelt Example Bank",
         loan=(40, 46_000_000, 1), lien=(1_150_000, 40, "Desert Mechanical FAKE Inc.", "mechanical and plumbing work"),
         permit=(1000, 1, False)),
    dict(id="austin-congress", name="FAKE Congress Row Apartments", street="900 Sample Congress Ave", city="Austin",
         ptype="multifamily", units=240, borrower="Congress Row FAKE Partners LP", lender="Gulf Placeholder Trust",
         loan=(38, 52_000_000, 0), lien=(870_000, 55, "Lone Star Concrete FAKE LLC", "concrete and framing"),
         permit=(1150, 0, True), proj=(7, False, 0, "Opening Spring 2026")),
    dict(id="miami-brickell-hotel", name="FAKE Brickell Bay Hotel", street="150 Example Bay Dr", city="Miami", ptype="hotel",
         units=310, borrower="Brickell Bay Hospitality FAKE LLC", lender="Placeholder Trust Bank",
         cmbs=dict(dscr=0.81, occ=0.64, watch=True, status="60+ days delinquent", maturity_days=150, balance=61_000_000),
         legal=[("lis_pendens", "lender_lawsuit", 45, "Placeholder Trust Bank")]),
    dict(id="chicago-west-loop", name="FAKE West Loop Mixed-Use Tower", street="520 Example Fulton St", city="Chicago",
         ptype="mixed_use", units=180, borrower="West Loop Tower FAKE LLC", lender="Prairie Sample Savings",
         loan=(44, 71_000_000, 2), lien=(2_400_000, 30, "Great Lakes Steel FAKE Co.", "structural steel erection"),
         legal=[("foreclosure_notice", "foreclosure_scheduled", 20, "Prairie Sample Savings")]),
    dict(id="detroit-senior", name="FAKE Riverfront Senior Residences", street="88 Sample Jefferson Ave", city="Detroit",
         ptype="senior_housing", units=140, borrower="Riverfront Senior FAKE LLC", lender="Lakeshore Fakeland Bank",
         loan=(36, 29_000_000, 1), legal=[("receivership", "receiver_appointed", 35, "Wayne County Circuit Court")]),
    dict(id="phoenix-industrial", name="FAKE Sky Harbor Logistics Center", street="2200 Example Buckeye Rd", city="Phoenix",
         ptype="industrial", units=None, borrower="Sky Harbor Logistics FAKE LLC", lender="Sunbelt Example Bank",
         cmbs=dict(dscr=0.93, occ=0.74, watch=True, status="Current", maturity_days=-45, balance=38_500_000),
         legal=[("ucc_sale_notice", "ucc_sale_scheduled", 18, "Mezzanine lender (sample)")]),
    dict(id="orlando-retail", name="FAKE Orange Blossom Plaza", street="1500 Example Colonial Dr", city="Orlando", ptype="retail",
         units=None, borrower="Orange Blossom Retail FAKE LLC", lender="Placeholder Trust Bank",
         cmbs=dict(dscr=0.91, occ=0.69, watch=True, status="Current", maturity_days=200, balance=24_000_000),
         legal=[("lis_pendens", "lis_pendens", 70, "Placeholder Trust Bank"), ("tax_delinquency", "tax_delinquent", 30, "County treasurer (sample)")]),
    dict(id="ypsilanti-student", name="FAKE Huron Ridge Student Commons", street="305 Example Huron River Dr", city="Ypsilanti",
         ptype="student_housing", units=196, borrower="Huron Ridge FAKE LLC", lender="Lakeshore Fakeland Bank",
         loan=(41, 33_000_000, 1), lien=(1_020_000, 28, "Washtenaw Roofing FAKE Inc.", "roofing and waterproofing"),
         proj=(10, True, 2, "Now leasing for Fall 2025")),
    dict(id="houston-midtown", name="FAKE Midtown Gardens Apartments", street="1800 Sample Main St", city="Houston",
         ptype="multifamily", units=265, borrower="Midtown Gardens FAKE LP", lender="Gulf Placeholder Trust",
         cmbs=dict(dscr=1.08, occ=0.82, watch=False, status="Current", maturity_days=170, balance=44_000_000),
         legal=[("auction_listing", "listed_for_auction", 22, "Online auction (sample)"), ("receivership", "receiver_appointed", 50, "Harris County District Court")]),
    dict(id="tucson-hotel", name="FAKE Catalina Foothills Resort", street="7000 Example Ina Rd", city="Tucson", ptype="hotel",
         units=150, borrower="Catalina Resort FAKE LLC", lender="Sunbelt Example Bank",
         loan=(46, 38_000_000, 0), permit=(1300, 0, True), legal=[("tax_delinquency", "construction_stalled", 60, "Contractor notice (sample)")]),
    # ------------------------------- Tier 2 -------------------------------
    dict(id="dallas-deep-ellum", name="FAKE Deep Ellum Lofts", street="2700 Example Elm St", city="Dallas", ptype="multifamily",
         units=170, borrower="Deep Ellum Lofts FAKE LP", lender="Gulf Placeholder Trust", loan=(37, 31_000_000, 1)),
    dict(id="tampa-channelside", name="FAKE Channelside Apartments", street="601 Sample Channel Dr", city="Tampa", ptype="multifamily",
         units=220, borrower="Channelside FAKE LLC", lender="Placeholder Trust Bank", loan=(35, 42_000_000, 1)),
    dict(id="champaign-campustown", name="FAKE Campustown Commons", street="610 Example Green St", city="Champaign",
         ptype="student_housing", units=204, borrower="Campustown Commons FAKE LLC", lender="Prairie Sample Savings",
         loan=(34, 27_000_000, 1)),
    dict(id="lansing-capitol", name="FAKE Capitol Square Offices", street="120 Example Capitol Ave", city="Lansing", ptype="office",
         units=None, borrower="Capitol Square FAKE LLC", lender="Lakeshore Fakeland Bank",
         lien=(640_000, 50, "Capital City Electric FAKE Inc.", "electrical rewiring"), permit=(1000, 0, True)),
    dict(id="grand-rapids-lofts", name="FAKE Monroe Avenue Lofts", street="330 Sample Monroe Ave NW", city="Grand Rapids",
         ptype="mixed_use", units=96, borrower="Monroe Lofts FAKE LLC", lender="Lakeshore Fakeland Bank",
         lien=(520_000, 35, "Grand River Glass FAKE Co.", "curtain wall glazing"), proj=(6, False, 1, "Opening Fall 2025")),
    dict(id="san-antonio-retail", name="FAKE Riverwalk Marketplace", street="400 Example Commerce St", city="San Antonio",
         ptype="retail", units=None, borrower="Riverwalk Marketplace FAKE LP", lender="Gulf Placeholder Trust",
         cmbs=dict(dscr=0.97, occ=0.78, watch=True, status="Current", maturity_days=300, balance=19_800_000)),
    dict(id="dallas-office", name="FAKE Uptown Office Park", street="3300 Sample McKinney Ave", city="Dallas", ptype="office",
         units=None, borrower="Uptown Office FAKE LP", lender="Gulf Placeholder Trust",
         cmbs=dict(dscr=0.98, occ=0.76, watch=True, status="Current", maturity_days=210, balance=33_000_000)),
    dict(id="houston-industrial", name="FAKE Ship Channel Distribution Park", street="9100 Example Navigation Blvd", city="Houston",
         ptype="industrial", units=None, borrower="Ship Channel FAKE LLC", lender="Gulf Placeholder Trust",
         cmbs=dict(dscr=0.99, occ=0.79, watch=True, status="Current", maturity_days=140, balance=26_500_000)),
    dict(id="miami-wynwood", name="FAKE Wynwood Collective", street="2250 Example NW 2nd Ave", city="Miami", ptype="mixed_use",
         units=120, borrower="Wynwood Collective FAKE LLC", lender="Placeholder Trust Bank",
         proj=(9, False, 1, "Opening Winter 2025"), legal=[("tax_delinquency", "tax_delinquent", 40, "County tax collector (sample)")]),
    dict(id="tempe-student-2", name="FAKE Rio Salado Student Flats", street="1100 Sample Rio Salado Pkwy", city="Tempe",
         ptype="student_housing", units=180, borrower="Rio Salado FAKE LLC", lender="Sunbelt Example Bank",
         proj=(12, True, 1, "Now leasing for Fall 2025"),
         legal=[("tax_delinquency", "tax_delinquent", 45, "County treasurer (sample)")]),
    dict(id="austin-east", name="FAKE East Sixth Residences", street="1400 Example E 6th St", city="Austin", ptype="multifamily",
         units=150, borrower="East Sixth FAKE LLC", lender="Gulf Placeholder Trust",
         permit=(1050, 0, True), lien=(410_000, 90, "Capital Drywall FAKE LLC", "drywall and finishes")),
    dict(id="detroit-lien-lender", name="FAKE Corktown Flats", street="1900 Sample Michigan Ave", city="Detroit", ptype="multifamily",
         units=110, borrower="Corktown Flats FAKE LLC", lender="Lakeshore Fakeland Bank",
         lien=(780_000, 60, "Motor City Plumbing FAKE Inc.", "plumbing risers"), permit=(1100, 0, True)),
    dict(id="orlando-lis", name="FAKE Lake Eola Townhomes", street="45 Example Eola Dr", city="Orlando", ptype="multifamily",
         units=64, borrower="Lake Eola FAKE LLC", lender="Placeholder Trust Bank",
         legal=[("lis_pendens", "lis_pendens", 40, "Placeholder Trust Bank")]),
    dict(id="chicago-ucc", name="FAKE Pilsen Artisan Lofts", street="1750 Example 18th St", city="Chicago", ptype="mixed_use",
         units=84, borrower="Pilsen Artisan FAKE LLC", lender="Prairie Sample Savings",
         legal=[("ucc_sale_notice", "ucc_sale_scheduled", 25, "Mezzanine lender (sample)")]),
    dict(id="tucson-auction", name="FAKE Barrio Student Housing", street="260 Sample 4th Ave", city="Tucson", ptype="student_housing",
         units=122, borrower="Barrio Student FAKE LLC", lender="Sunbelt Example Bank",
         legal=[("auction_listing", "listed_for_auction", 15, "Online auction (sample)"), ("tax_delinquency", "tax_delinquent", 35, "County treasurer (sample)")]),
]


def _address(p: dict) -> str:
    st, _, zp = PLACES[p["city"]]
    return f"{p['street']}, {p['city']}, {st} {zp}"


def _add_file(con, fid: str, text: str, *, category: str, p: dict, source: str = "recorder", ext: str = "txt",
              record_id: str | None = None) -> str:
    st, county, _ = PLACES[p["city"]]
    store_file(con, (BANNER + text).encode(), source=source, doc_category=category, collector_name=COLLECTOR,
               state=st, county=county, city=p["city"], url=None, source_record_id=record_id,
               notes="FAKE DATA for the demo", ext=ext, file_id=fid)
    return fid


def _reading(fid: str, ftype: str, p: dict, **fields) -> dict:
    st, _, zp = PLACES[p["city"]]
    row = {"file_id": fid, "filing_type": ftype, "confidence": "high", "status": "ok", "model_name": MODEL,
           "prompt_version": "sample", "extracted_at": NOW, "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0,
           "borrower_name": p["borrower"], "project_name": p["name"], "property_address": _address(p),
           "property_city": p["city"], "property_state": st, "property_zip": zp, "parcel_number": p["parcel"],
           "property_type": p["ptype"]}
    row.update(fields)
    for k in ("distress_tags", "quotes"):
        if isinstance(row.get(k), (list, dict)):
            row[k] = json.dumps(row[k])
    return row


def clean(con) -> None:
    for (path,) in con.execute("SELECT file_path FROM raw_files WHERE file_id LIKE 'fake-flag-%'").fetchall():
        try:
            from collectors.common import config
            config.from_stored_path(path).unlink(missing_ok=True)
        except Exception:
            pass
    for t, col in (("raw_files", "file_id"), ("filing_fields", "file_id"), ("recorded_loans", "record_id"),
                   ("projects", "project_id")):
        con.execute(f"DELETE FROM {t} WHERE {col} LIKE 'fake-flag-%'")
    con.execute("DELETE FROM loans WHERE loan_key LIKE 'edgar:FAKEFLAG-%'")
    con.execute("DELETE FROM bank_stats WHERE rssd_id BETWEEN '9990010' AND '9990019'")


def add_property(con, n: int, p: dict) -> None:
    pid = f"fake-flag-{p['id']}"
    st, county, zp = PLACES[p["city"]]
    p["parcel"] = f"FAKE-{st}-{7000 + n:04d}-{n:03d}"
    addr = _address(p)

    if "loan" in p:
        months, amount, mods = p["loan"]
        rec = TODAY - timedelta(days=int(months * 30.44))
        inst = f"FAKE-{rec.year}-{800000 + n * 17}"
        text = (f"CONSTRUCTION MORTGAGE\nInstrument: {inst}\nRecorded: {rec}\nMortgagor (borrower): {p['borrower']}\n"
                f"Mortgagee (lender): {p['lender']}\nPrincipal amount: ${amount:,.0f}\nProperty: {p['name']}, {addr}\n"
                f"Parcel: {p['parcel']}\nPurpose: construction of the {p['ptype'].replace('_', ' ')} project.\n")
        f = _add_file(con, f"{pid}-loan", text, category="recorded_loan", p=p, record_id=inst)
        db.insert_row(con, "recorded_loans", {
            "record_id": f"{pid}-rl-1", "file_id": f, "state": st, "county": county, "instrument_number": inst,
            "document_type": "CONSTRUCTION MORTGAGE", "loan_kind": "construction", "recording_date": rec,
            "grantor": p["borrower"], "grantee": p["lender"], "amount": amount, "parcel_number": p["parcel"],
            "property_address": addr, "related_instrument": None, "collected_at": NOW})
        for i in range(mods):
            mdate = rec + timedelta(days=int(330 + 300 * i))
            minst = f"FAKE-{mdate.year}-{900000 + n * 17 + i}"
            mt = (f"MORTGAGE MODIFICATION AGREEMENT\nInstrument: {minst}\nRecorded: {mdate}\nModifies mortgage: {inst}\n"
                  f"Borrower: {p['borrower']}\nLender: {p['lender']}\nTerms: maturity extended 12 months.\nParcel: {p['parcel']}\n")
            mf = _add_file(con, f"{pid}-mod{i + 1}", mt, category="recorded_loan", p=p, record_id=minst)
            db.insert_row(con, "recorded_loans", {
                "record_id": f"{pid}-rl-{i + 2}", "file_id": mf, "state": st, "county": county, "instrument_number": minst,
                "document_type": "MODIFICATION OF MORTGAGE", "loan_kind": "modification", "recording_date": mdate,
                "grantor": p["borrower"], "grantee": p["lender"], "amount": None, "parcel_number": p["parcel"],
                "property_address": addr, "related_instrument": inst, "collected_at": NOW})

    if "loan" in p and not any(k in p for k in ("lien", "permit", "proj", "legal", "cmbs")):
        # Only recorded loans exist for this one; a short project profile gives it a readable name.
        f = _add_file(con, f"{pid}-profile", f"PROJECT PROFILE\nProject: {p['name']}\nProperty: {addr}\nParcel: {p['parcel']}\n",
                      category="other", p=p, source="public_notice")
        upsert(con, "filing_fields", _reading(f, "other", p, distress_tags=[], quotes={"project_name": f"Project: {p['name']}"}))

    if "lien" in p:
        amount, days, claimant, work = p["lien"]
        rec = TODAY - timedelta(days=days)
        inst = f"FAKE-{rec.year}-{700000 + n * 13}"
        amt_line = f"${amount:,.0f} for unpaid {work}."
        text = (f"CLAIM OF LIEN\nInstrument: {inst}\nRecorded: {rec}\nClaimant: {claimant}\nOwner/Debtor: {p['borrower']}\n"
                f"Property: {p['name']}, {addr}\nParcel: {p['parcel']}\nAmount claimed: {amt_line}\n")
        f = _add_file(con, f"{pid}-lien", text, category="mechanics_lien", p=p, record_id=inst)
        upsert(con, "filing_fields", _reading(
            f, "mechanics_lien", p, lien_claimant=claimant, lien_amount=float(amount), lien_work=work,
            recording_date=rec, distress_tags=["contractor_unpaid"],
            quotes={"lien_amount": f"Amount claimed: {amt_line}", "lien_claimant": f"Claimant: {claimant}"}))

    if "permit" in p:
        issued_days, exts, expired = p["permit"]
        issued = TODAY - timedelta(days=issued_days)
        exp = TODAY - timedelta(days=60) if expired else issued + timedelta(days=1250)
        num = f"FAKE-BLD-{issued.year}-{1000 + n}"
        status = "Expired" if expired else ("Active - Extended" if exts else "Active")
        doc = {"document": "BUILDING PERMIT", "permit_number": num, "address": addr, "issue_date": issued.isoformat(),
               "status": status, "expiration_date": exp.isoformat(),
               "project_description": f"New construction, {p['ptype'].replace('_', ' ')}", "note": "FAKE DATA",
               "extension_history": [{"approved": (issued + timedelta(days=500 + 300 * i)).isoformat(),
                                      "new_expiration": exp.isoformat()} for i in range(exts)]}
        f = _add_file(con, f"{pid}-permit", json.dumps(doc, indent=2), category="building_permit", p=p,
                      source="permit", ext="json", record_id=num)
        upsert(con, "filing_fields", _reading(
            f, "building_permit", p, permit_number=num, permit_status=status, permit_expiration_date=exp,
            filing_date=issued, distress_tags=["permit_extended"] if exts else (["permit_expired"] if expired else []),
            quotes={"permit_status": f'"status": "{status}"'}))

    if "proj" in p:
        over, missed, changes, phrase = p["proj"]
        upsert(con, "projects", {
            "project_id": f"{pid}-proj", "project_name": p["name"], "developer_name": p["borrower"],
            "property_address": addr, "city": p["city"], "state": st, "county": county,
            "property_type": p["ptype"], "units_or_beds": p.get("units"), "main_url": "https://example.org/fake",
            "first_seen_at": NOW, "stated_opening_phrase": phrase,
            "opening_deadline": TODAY - timedelta(days=int(over * 30.44)), "page_still_unopened": True,
            "stale_candidate": True, "months_overdue": float(over), "missed_lease_up": missed,
            "date_change_count": changes, "first_stated_phrase": phrase, "last_checked_at": NOW,
            "check_status": "ok", "notes": "FAKE DATA"})

    if "cmbs" in p:
        c = p["cmbs"]
        db.insert_row(con, "loans", {
            "loan_key": f"edgar:FAKEFLAG-{n:02d}:{n:04d}", "source": "sec_edgar", "deal_name": f"FAKE Trust 2020-F{n:02d}",
            "property_name": p["name"], "property_address": p["street"], "property_city": p["city"],
            "property_state": st, "property_zip": zp, "property_type": p["ptype"].replace("_", " ").title(),
            "current_balance": c["balance"], "original_balance": c["balance"] * 1.04,
            "maturity_date": TODAY + timedelta(days=c["maturity_days"]), "dscr": c["dscr"], "occupancy": c["occ"],
            "payment_status": c["status"], "servicer_name": "Example Servicing FAKE", "watchlist_flag": c["watch"],
            "servicer_commentary": "FAKE: servicer comment for the demo.",
            "report_period": TODAY.replace(day=1) - timedelta(days=1), "file_id": None, "collected_at": NOW,
        }, replace=True)

    for j, (ftype, tag, days, filer) in enumerate(p.get("legal", [])):
        filed = TODAY - timedelta(days=days)
        case = f"FAKE-{filed.year}-{st}-{5000 + n * 7 + j}"
        titles = {"lis_pendens": "NOTICE OF LIS PENDENS", "foreclosure_notice": "NOTICE OF TRUSTEE'S SALE",
                  "receivership": "ORDER APPOINTING RECEIVER", "ucc_sale_notice": "NOTICE OF UCC SALE OF MEMBERSHIP INTERESTS",
                  "auction_listing": "AUCTION LISTING", "tax_delinquency": "DELINQUENT PROPERTY TAX NOTICE"}
        title = titles[ftype]
        extra = {}
        body = f"{title}\nCase No. {case}\nFiled: {filed}\nFiled by: {filer}\nParty: {p['borrower']}\nProperty: {p['name']}, {addr}\nParcel: {p['parcel']}\n"
        if ftype in ("foreclosure_notice", "ucc_sale_notice", "auction_listing"):
            when = TODAY + timedelta(days=30 + 5 * j)
            body += f"Sale date: {when}\n"
            extra["sale_date" if ftype != "auction_listing" else "auction_date"] = when
        f = _add_file(con, f"{pid}-{ftype}", body, category=ftype, p=p, source="public_notice", record_id=case)
        upsert(con, "filing_fields", _reading(
            f, ftype, p, case_number=case, filing_date=filed, lender_name=p["lender"] if ftype != "tax_delinquency" else None,
            distress_tags=[tag], quotes={"filing_type": title, "case_number": f"Case No. {case}"}, **extra))


def main() -> None:
    con = open_db(None)
    clean(con)
    for name, (rssd, city, st, ratios) in BANKS.items():
        for r in bank_rows(rssd, name, city, st, 350_000_000, ratios):
            db.insert_row(con, "bank_stats", r)
    for n, p in enumerate(P, start=1):
        add_property(con, n, p)
    print(f"Sample flags: {len(P)} invented properties with evidence, {len(BANKS)} invented banks.")


if __name__ == "__main__":
    main()
