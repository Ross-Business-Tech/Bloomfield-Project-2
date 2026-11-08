"""
demo_data.py - builds a demo database with two weeks of made-up data, scores both weeks,
and adds a few research-log entries, so the dashboard has something to show.

    cd pipeline
    python -m scorer.demo_data            # writes data/demo.duckdb
    BLOOMFIELD_DB=data/demo.duckdb streamlit run dashboard/app.py

Everything here is made up: projects, companies, banks, people and numbers.
The input tables copy the column layout of Part A's collectors/common/db.py and
Part B's scout_reader_schema.py, so the scorer reads demo data exactly like real data.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from . import rank
from .store import connect

DEMO_PATH = Path(__file__).resolve().parents[1] / "data" / "demo.duckdb"

INPUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS recorded_loans (
    record_id TEXT PRIMARY KEY, file_id TEXT, state TEXT, county TEXT, instrument_number TEXT,
    document_type TEXT, loan_kind TEXT, recording_date DATE, grantor TEXT, grantee TEXT, amount DOUBLE,
    parcel_number TEXT, property_address TEXT, legal_description TEXT, related_instrument TEXT, collected_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS loans (
    loan_key TEXT PRIMARY KEY, source TEXT, deal_name TEXT, property_name TEXT, property_address TEXT,
    property_city TEXT, property_state TEXT, property_zip TEXT, property_type TEXT, current_balance DOUBLE,
    original_balance DOUBLE, maturity_date DATE, dscr DOUBLE, occupancy DOUBLE, payment_status TEXT,
    servicer_name TEXT, watchlist_flag BOOLEAN, servicer_commentary TEXT, report_period DATE, file_id TEXT,
    collected_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS bank_stats (
    rssd_id TEXT, bank_name TEXT, city TEXT, state TEXT, report_date DATE, cre_construction DOUBLE,
    cre_multifamily DOUBLE, cre_nonfarm_nonres DOUBLE, cre_total DOUBLE, cre_past_due_30_89 DOUBLE,
    cre_past_due_90_plus DOUBLE, cre_nonaccrual DOUBLE, cre_nonaccrual_ratio DOUBLE,
    nonaccrual_change_qoq DOUBLE, rising_two_quarters BOOLEAN, PRIMARY KEY (rssd_id, report_date));
CREATE TABLE IF NOT EXISTS filing_fields (
    file_id TEXT PRIMARY KEY, filing_type TEXT, borrower_name TEXT, developer_name TEXT, project_name TEXT,
    other_parties JSON, property_address TEXT, property_city TEXT, property_state TEXT, property_zip TEXT,
    parcel_number TEXT, property_type TEXT, lender_name TEXT, servicer_name TEXT, loan_kind TEXT,
    loan_amount DOUBLE, amount_owed DOUBLE, recording_date DATE, maturity_date DATE, filing_date DATE,
    sale_date DATE, auction_date DATE, lien_claimant TEXT, lien_amount DOUBLE, lien_work TEXT,
    permit_number TEXT, permit_status TEXT, permit_expiration_date DATE, stated_opening_phrase TEXT,
    page_still_unopened BOOLEAN, leasing_season TEXT, starting_bid DOUBLE, case_number TEXT,
    distress_tags JSON, quotes JSON, confidence TEXT, status TEXT, review_reason TEXT, model_name TEXT,
    prompt_version TEXT, extracted_at TIMESTAMP, input_tokens INTEGER, output_tokens INTEGER, est_cost_usd DOUBLE);
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY, project_name TEXT, developer_name TEXT, property_address TEXT, city TEXT,
    state TEXT, county TEXT, property_type TEXT, units_or_beds INTEGER, main_url TEXT, first_seen_at TIMESTAMP,
    stated_opening_phrase TEXT, opening_deadline DATE, page_still_unopened BOOLEAN, stale_candidate BOOLEAN,
    months_overdue DOUBLE, missed_lease_up BOOLEAN, date_change_count INTEGER, first_stated_phrase TEXT,
    last_checked_at TIMESTAMP, check_status TEXT, notes TEXT);
"""

WEEK1 = date(2026, 11, 2)
WEEK2 = date(2026, 11, 9)

KH = dict(property_address="410 Sample Way", property_city="Ann Arbor", property_state="MI")


def ins(con, table, **row):
    con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


def filing(con, file_id, filing_type, quote, **fields):
    ins(con, "filing_fields", file_id=file_id, filing_type=filing_type, status="ok", confidence="high",
        quotes=json.dumps({"main": quote}), **fields)


def week1_data(con):
    # 1. Kestrel Hall, Ann Arbor: Bloomfield's example. Stale opening + stuck construction loan + lien.
    ins(con, "projects", project_id="p-kestrel", project_name="Kestrel Hall", developer_name="Kestrel Hall Owner LLC",
        property_address="410 Sample Way", city="Ann Arbor", state="MI", county="Washtenaw",
        property_type="Student housing", units_or_beds=420, main_url="kestrelhall-example.com",
        stated_opening_phrase="Opening Summer 2026", opening_deadline=date(2026, 9, 22), page_still_unopened=True,
        stale_candidate=True, months_overdue=1.5, missed_lease_up=True, date_change_count=2,
        first_stated_phrase="Opening Fall 2025")
    ins(con, "recorded_loans", record_id="r-kh-1", file_id="f-kh-mtg", state="MI", county="Washtenaw",
        document_type="Construction Mortgage", loan_kind="construction", recording_date=date(2023, 3, 14),
        grantor="Kestrel Hall Owner LLC", grantee="Washtenaw Commerce Bank", amount=38_000_000,
        property_address="410 Sample Way")
    ins(con, "recorded_loans", record_id="r-kh-2", state="MI", county="Washtenaw", document_type="Modification",
        loan_kind="extension", recording_date=date(2025, 2, 20), grantor="Kestrel Hall Owner LLC",
        grantee="Washtenaw Commerce Bank", property_address="410 Sample Way")
    ins(con, "recorded_loans", record_id="r-kh-3", state="MI", county="Washtenaw", document_type="Modification",
        loan_kind="extension", recording_date=date(2026, 3, 2), grantor="Kestrel Hall Owner LLC",
        grantee="Washtenaw Commerce Bank", property_address="410 Sample Way")
    filing(con, "f-kh-mtg", "recorded_loan", "This Mortgage secures a loan maturing on March 14, 2026.",
           maturity_date=date(2026, 3, 14), **KH)
    filing(con, "f-kh-lien", "mechanics_lien",
           "Cool Air Mechanical Inc. claims a lien in the amount of $184,500.00 for HVAC labor and materials.",
           lien_claimant="Cool Air Mechanical Inc.", lien_amount=184_500, lien_work="HVAC",
           filing_date=date(2026, 10, 2), borrower_name="Kestrel Hall Owner LLC", **KH)

    # 2. Huron Yards Lofts, Ypsilanti: construction loan past due, website stale.
    ins(con, "projects", project_id="p-huron", project_name="Huron Yards Lofts", developer_name="HY Lofts Partners LLC",
        property_address="88 River Street", city="Ypsilanti", state="MI", property_type="Multifamily",
        stated_opening_phrase="Delivery Q2 2026", opening_deadline=date(2026, 6, 30), stale_candidate=True,
        months_overdue=4.2, missed_lease_up=False, date_change_count=0, main_url="huronyards-example.com")
    ins(con, "recorded_loans", record_id="r-hy-1", state="MI", county="Washtenaw", document_type="Construction Mortgage",
        loan_kind="construction", recording_date=date(2022, 8, 1), grantor="HY Lofts Partners LLC",
        grantee="Huron Valley Community Bank", amount=41_000_000, property_address="88 River St.")

    # 3. Harbor Point Hotel, Tampa: CMBS loan under 1.0x DSCR, on watchlist, maturing soon.
    ins(con, "loans", loan_key="cmbs-hph", source="trepp", deal_name="SAMPLE 2019-C9", property_name="Harbor Point Hotel",
        property_address="1200 Bayfront Drive", property_city="Tampa", property_state="FL", property_type="LO",
        current_balance=28_000_000, maturity_date=date(2027, 1, 15), dscr=0.84, occupancy=0.71,
        payment_status="Current", watchlist_flag=True,
        servicer_commentary="Borrower indicated it may not be able to refinance at maturity; RevPAR down 14% year over year.")

    # 4. Prairie Logistics Center, Indianapolis: lender sued (week 1), foreclosure notice next week.
    filing(con, "f-plc-suit", "lender_lawsuit",
           "Plaintiff Midland Capital Lending seeks judgment of $19,250,000 against Prairie Logistics Owner LLC.",
           borrower_name="Prairie Logistics Owner LLC", project_name="Prairie Logistics Center",
           property_address="5500 Commerce Pkwy", property_city="Indianapolis", property_state="IN",
           property_type="Industrial", lender_name="Midland Capital Lending", loan_amount=21_000_000,
           amount_owed=19_250_000, filing_date=date(2026, 9, 18))

    # 5. Desert Bloom Senior Living, Phoenix: on Ten-X twice, low occupancy.
    for i, d in enumerate([date(2026, 7, 14), date(2026, 11, 20)], 1):
        filing(con, f"f-dbs-auc{i}", "auction_listing",
               "Bank-owned. 132-unit assisted living community. Online auction.",
               project_name="Desert Bloom Senior Living", property_address="3100 E Camelback Road",
               property_city="Phoenix", property_state="AZ", property_type="Assisted living",
               auction_date=d, starting_bid=6_500_000, filing_date=d.replace(day=1))
    ins(con, "loans", loan_key="cmbs-dbs", source="sec_edgar", property_name="Desert Bloom Senior Living",
        property_address="3100 East Camelback Rd", property_city="Phoenix", property_state="AZ", property_type="HC",
        current_balance=14_500_000, dscr=0.55, occupancy=62, payment_status="90+ days delinquent")

    # 6. Midtown Office Tower, Chicago: office, so filtered out by default.
    ins(con, "loans", loan_key="cmbs-mot", source="trepp", property_name="Midtown Office Tower",
        property_address="200 W Sample Street", property_city="Chicago", property_state="IL", property_type="OF",
        current_balance=33_000_000, dscr=0.71, occupancy=0.58, payment_status="60 days delinquent")

    # 7. Gateway Retail Plaza, Dallas: too big for the size range, filtered out.
    ins(con, "loans", loan_key="cmbs-grp", source="trepp", property_name="Gateway Retail Plaza",
        property_address="9000 Gateway Blvd", property_city="Dallas", property_state="TX", property_type="RT",
        current_balance=72_000_000, dscr=0.92, payment_status="Current", watchlist_flag=True)

    # 8. Maple Commons, Lansing: stale this week; next week the city issues a certificate of occupancy.
    ins(con, "projects", project_id="p-maple", project_name="Maple Commons", developer_name="Maple Commons LLC",
        property_address="45 Maple Avenue", city="Lansing", state="MI", property_type="Multifamily",
        stated_opening_phrase="Opening August 2026", opening_deadline=date(2026, 8, 31), stale_candidate=True,
        months_overdue=2.0, date_change_count=1, first_stated_phrase="Opening June 2026")

    # 9. Riverside Flats, Columbus: one small contractor lien, loan size unknown.
    filing(con, "f-rf-lien", "mechanics_lien", "Buckeye Concrete LLC claims $62,300.00 for concrete work.",
           project_name="Riverside Flats", property_address="12 Riverside Dr", property_city="Columbus",
           property_state="OH", property_type="Multifamily", lien_claimant="Buckeye Concrete LLC",
           lien_amount=62_300, lien_work="concrete", filing_date=date(2026, 10, 20))

    # Banks (FFIEC). Washtenaw Commerce Bank is under pressure; Huron Valley is not.
    for q, ratio, rising in [(date(2026, 3, 31), 0.021, False), (date(2026, 6, 30), 0.034, True)]:
        ins(con, "bank_stats", rssd_id="900001", bank_name="Washtenaw Commerce Bank", city="Ann Arbor", state="MI",
            report_date=q, cre_nonaccrual_ratio=ratio, rising_two_quarters=rising)
    ins(con, "bank_stats", rssd_id="900002", bank_name="Huron Valley Community Bank", city="Ypsilanti", state="MI",
        report_date=date(2026, 6, 30), cre_nonaccrual_ratio=0.008, rising_two_quarters=False)


def week2_data(con):
    # Kestrel Hall gets worse: the contractor sues, and a UCC sale notice runs in the WSJ.
    filing(con, "f-kh-suit", "lis_pendens",
           "Cool Air Mechanical Inc. v. Kestrel Hall Owner LLC: action to foreclose construction lien.",
           filing_date=date(2026, 11, 4), borrower_name="Kestrel Hall Owner LLC", **KH)
    filing(con, "f-kh-ucc", "ucc_sale_notice",
           "NOTICE OF UCC SALE: 100% of the membership interests in Kestrel Hall Owner LLC will be sold at public "
           "auction on December 10, 2026 at 10:00 a.m.",
           filing_date=date(2026, 11, 5), sale_date=date(2026, 12, 10), borrower_name="Kestrel Hall Owner LLC",
           lender_name="Northgate Mezz Capital", **KH)
    # Prairie Logistics: lawsuit turns into a foreclosure sale date.
    filing(con, "f-plc-fc", "foreclosure_notice",
           "Sheriff's sale of the property at 5500 Commerce Parkway will be held on December 1, 2026.",
           project_name="Prairie Logistics Center", property_address="5500 Commerce Parkway",
           property_city="Indianapolis", property_state="IN", property_type="Industrial",
           amount_owed=19_250_000, filing_date=date(2026, 11, 3), sale_date=date(2026, 12, 1))
    # Maple Commons: city issues a certificate of occupancy, so it drops off.
    filing(con, "f-maple-co", "certificate_of_occupancy", "Certificate of Occupancy issued 11/04/2026.",
           property_address="45 Maple Ave", property_city="Lansing", property_state="MI")
    # A brand-new flag: Summit Ridge Apartments, Grand Rapids, unpaid taxes heading to a tax sale.
    filing(con, "f-sra-tax", "tax_delinquency",
           "Parcel 41-14-200-001: 2024 and 2025 taxes unpaid, $412,880.15 due. Subject to tax sale.",
           project_name="Summit Ridge Apartments", property_address="700 Summit Ridge Road",
           property_city="Grand Rapids", property_state="MI", property_type="Multifamily",
           amount_owed=412_880, loan_amount=17_500_000, filing_date=date(2026, 11, 2), sale_date=date(2027, 2, 3))


def research_log(con):
    entries = [
        ("2026-10-21 10:00", "addr:lakeview", "Lakeview Apartments (earlier flag)", "deal_closed",
         "Closed a $12.5M rescue loan after a stale opening + stuck construction loan flag.", 12_500_000),
        ("2026-11-03 09:30", "410 sample way|MI", "Kestrel Hall", "looked_into",
         "Confirmed no CO with the city; construction loan extended twice.", None),
        ("2026-11-05 14:00", "410 sample way|MI", "Kestrel Hall", "called",
         "Reached the developer's principal (sample contact). Open to a bridge loan.", None),
        ("2026-11-04 11:15", "5500 commerce pkwy|IN", "Prairie Logistics Center", "passed",
         "Lender already negotiating a deed in lieu.", None),
    ]
    for i, (when, key, name, status, note, amt) in enumerate(entries, 1):
        ins(con, "research_log", log_id=f"demo-{i}", property_key=key, property_name=name, status=status,
            note=note, deal_amount=amt, researcher="Demo researcher", logged_at=datetime.fromisoformat(when))


def build(path: str | Path = DEMO_PATH) -> Path:
    path = Path(path)
    if str(path) != ":memory:" and path.exists():
        path.unlink()
    con = connect(path)
    con.execute(INPUT_SCHEMA)
    settings = rank.load_settings()
    week1_data(con)
    rank.run(con, settings, WEEK1)
    week2_data(con)
    rank.run(con, settings, WEEK2)
    research_log(con)
    con.close()
    return path


if __name__ == "__main__":
    p = build()
    print(f"Demo database written to {p}")
    from .rank import main
    main(["--db", str(p), "--date", WEEK2.isoformat()])
