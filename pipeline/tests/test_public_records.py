"""Tests for collectors/public_records.py: county tax, assessor, code-violation and listing exports -> the
general creative-signal tables. Made-up data only."""
from datetime import date

from collectors import public_records
from collectors.common import config, inbox
from scorer import creative as cr
from scorer import equation as eq
from scorer.group import Property
from scout_reader_schema import create_scout_reader_tables

TAX_CSV = """Parcel ID,Situs Address,Tax Year,Balance Due,Due Date,Payment Status
12-34,500 FAKE Harbor Blvd,2025,"$41,250.00",2025-11-30,Delinquent
56-78,600 FAKE Paid St,2025,0,2025-11-30,Paid
"""
ASSESSOR_CSV = """Parcel,Site Address,Roll Year,Just Value
12-34,500 FAKE Harbor Blvd,2023,"12,000,000"
12-34,500 FAKE Harbor Blvd,2026,"8,400,000"
,,2023,"90,000,000,000"
,,2026,"94,500,000,000"
"""
CODE_CSV = """Case Date,Address,Case Type,Case Status,Violation Description
2026-05-01,500 FAKE Harbor Blvd,Code,Open,Unsafe structure - balcony
2026-02-01,500 FAKE Harbor Blvd,Fire,Closed,Extinguisher
"""
LISTING_CSV = """Date,Address,Available SF,Building SF,URL
2025-06-01,500 FAKE Harbor Blvd,5000,100000,https://example.com/a
2026-09-01,500 FAKE Harbor Blvd,30000,100000,https://example.com/b
"""


def drop(source, name, text, state="FL", county="pinellas"):
    folder = config.inbox_dir() / source / state / county
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text)


def test_public_record_exports_feed_the_general_signals(con):
    create_scout_reader_tables(con)
    drop("tax_collector", "delinquent.csv", TAX_CSV)
    drop("assessor", "roll.csv", ASSESSOR_CSV)
    drop("code_enforcement", "cases.csv", CODE_CSV)
    drop("listing", "availability.csv", LISTING_CSV)
    inbox.load_inbox(con)
    public_records.collect(con)
    public_records.collect(con)                                   # loading twice adds nothing

    assert con.execute("SELECT count(*) FROM property_tax_status").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM assessed_values").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM county_assessment_trend").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM compliance_records").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM space_availability").fetchone()[0] == 2
    due = con.execute("SELECT amount_due, state, county FROM property_tax_status WHERE parcel = '12-34'").fetchone()
    assert due[0] == 41_250.0 and due[1] == "FL" and due[2]

    # A bank loan on an office building, nothing from the SEC: the public records alone score it.
    cfg = eq.load_equation_config()
    rents, market, inspections, extra = eq.load_inputs(con)
    p = Property("harbor", filings=[{"file_id": "x", "property_address": "500 FAKE Harbor Blvd",
                                     "property_state": "FL", "property_type": "office"}])
    as_of = date(2026, 11, 1)
    by = {s.code: s for s in eq.creative_signals(p, {"state": "FL"}, cfg, as_of, rents, market, inspections, extra)}
    assert by["T"].known and by["T"].value > 0.9                 # ~11 months past due
    assert by["U"].value == 1.0                                   # open unsafe-structure case
    assert by["W"].known and "vs. county" in by["W"].why         # -30% vs. county +5%
    assert by["Q"].known and by["Q"].value == 1.0                # 95% -> 70% listed-space occupancy
    assert not by["V"].known and "don't have" in by["V"].why     # no loan-level income: honest "don't have"
    assert isinstance(by["T"], cr.Sig)
