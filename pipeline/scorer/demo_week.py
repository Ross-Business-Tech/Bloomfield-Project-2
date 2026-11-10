"""
demo_week.py - a made-up demo database for the dashboard, with two weekly runs.

    cd pipeline
    python -m scorer.demo_week                 # writes data/demo.duckdb
    BLOOMFIELD_DB=data/demo.duckdb streamlit run dashboard/app.py

Builds on the team's own pipeline, in order:
  scripts/setup_db.py -> scripts/make_fake_data.py -> scripts/make_fake_readings.py   (Parts A, B)
  python -m matcher                                                                   (Part C)
then adds a few more FAKE properties chosen to show each rule of the equation
(standout, counter-signal, filters, week-over-week changes), and runs scorer.weekly twice.
Everything here is made up.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1]
DEMO_DB = PIPELINE / "data" / "demo.duckdb"
WEEK1, WEEK2 = date(2026, 11, 2), date(2026, 11, 9)


def sh(*args, db):
    subprocess.run([sys.executable, *args, "--db", str(db)], cwd=PIPELINE, check=True,
                   stdout=subprocess.DEVNULL, env={**os.environ, "PYTHONPATH": str(PIPELINE)})


def ins(con, table, **row):
    con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


def loan(con, rid, kind, d, addr, state, county, borrower, lender, amount=None):
    ins(con, "recorded_loans", record_id=rid, state=state, county=county, instrument_number=rid.upper(),
        document_type=kind.title(), loan_kind=kind, recording_date=d, grantor=borrower, grantee=lender,
        amount=amount, property_address=addr, collected_at=datetime(2026, 11, 1))


def filing(con, fid, ftype, addr, city, state, quote, **kw):
    ins(con, "filing_fields", file_id=fid, filing_type=ftype, property_address=addr, property_city=city,
        property_state=state, status="ok", confidence="high", quotes=f'{{"main": {quote!r}}}'.replace("'", '"'), **kw)


def project(con, pid, name, addr, city, state, ptype, phrase, months, **kw):
    ins(con, "projects", project_id=pid, project_name=name, property_address=addr, city=city, state=state,
        property_type=ptype, stated_opening_phrase=phrase, months_overdue=months, stale_candidate=True,
        page_still_unopened=True, **kw)


def market(con, city, state, start, end):
    ins(con, "market_rents", city=city, state=state, period=date(2026, 1, 1), rent_index=start, source="FAKE index")
    ins(con, "market_rents", city=city, state=state, period=date(2026, 10, 1), rent_index=end, source="FAKE index")


def rents(con, pid, readings):
    for d, r in readings:
        ins(con, "rent_observations", project_id=pid, observed_at=d, asking_rent=r, unit_basis="bed")


def week1(con):
    # Team's FAKE Maize Row (Ann Arbor): add the rent and inspection data its R and I need.
    rents(con, "fake-proj-maize-row", [(date(2026, 1, 20), 1295.0), (date(2026, 3, 5), 1290.0),
                                       (date(2026, 9, 10), 1150.0), (date(2026, 10, 25), 1140.0)])
    market(con, "Ann Arbor", "MI", 100.0, 101.5)
    ins(con, "permit_inspections", permit_number="FAKE-BLD-2023-0456", state="MI", city="Ann Arbor",
        inspection_date=date(2026, 4, 2), inspection_type="framing", result="pass")

    # STANDOUT: a lone construction loan, 5+ years old, never paid off.
    a = "88 FAKE River St, Ypsilanti, MI"
    loan(con, "fk-hy-1", "construction", date(2021, 6, 1), a, "MI", "Washtenaw", "FAKE HY Lofts LLC",
         "FAKE Huron Valley Community Bank", 41e6)
    project(con, "fk-hy", "FAKE Huron Yards Lofts", a, "Ypsilanti", "MI", "multifamily", "Delivery Q2 2026", 4.0)

    # Kestrel Hall (Bloomfield's example): every early signal lines up.
    a = "410 FAKE Sample Way, Ann Arbor, MI"
    loan(con, "fk-kh-1", "construction", date(2023, 3, 14), a, "MI", "Washtenaw", "FAKE Kestrel Hall Owner LLC",
         "FAKE Washtenaw Commerce Bank", 38e6)
    loan(con, "fk-kh-2", "extension", date(2025, 2, 20), a, "MI", "Washtenaw", "FAKE Kestrel Hall Owner LLC",
         "FAKE Washtenaw Commerce Bank")
    project(con, "fk-kh", "FAKE Kestrel Hall", a, "Ann Arbor", "MI", "student housing", "Opening Summer 2026", 8.0,
            missed_lease_up=True, date_change_count=2, first_stated_phrase="Opening Fall 2025")
    rents(con, "fk-kh", [(date(2026, 2, 1), 1250.0), (date(2026, 10, 20), 1095.0)])
    filing(con, "fk-kh-bp", "building_permit", a, "Ann Arbor", "MI", "Building permit BP-FK-77 issued.",
           permit_number="BP-FK-77", filing_date=date(2023, 4, 1), project_name="FAKE Kestrel Hall")
    ins(con, "permit_inspections", permit_number="BP-FK-77", state="MI", city="Ann Arbor",
        inspection_date=date(2026, 6, 1), inspection_type="mechanical", result="partial")

    # COUNTER-SIGNAL: loan on the clock, but rent beats the city and the city inspected last week.
    a = "12 FAKE Riverside Dr, Columbus, OH"
    loan(con, "fk-rc-1", "construction", date(2022, 9, 1), a, "OH", "Franklin", "FAKE Riverbend LLC",
         "FAKE Buckeye Bank", 22e6)
    project(con, "fk-rc", "FAKE Riverbend Commons", a, "Columbus", "OH", "multifamily", "Opening Fall 2026", 1.0)
    rents(con, "fk-rc", [(date(2026, 1, 10), 1200.0), (date(2026, 10, 10), 1390.0)])
    market(con, "Columbus", "OH", 100.0, 101.0)
    filing(con, "fk-rc-bp", "building_permit", a, "Columbus", "OH", "Permit BP-FK-12 issued.",
           permit_number="BP-FK-12", filing_date=date(2023, 1, 5), project_name="FAKE Riverbend Commons")
    ins(con, "permit_inspections", permit_number="BP-FK-12", state="OH", city="Columbus",
        inspection_date=date(2026, 10, 28), inspection_type="final framing", result="pass")

    # Filtered out: office (Bloomfield doesn't buy it) and a loan above $50M.
    a = "200 FAKE W Sample St, Chicago, IL"
    loan(con, "fk-mo-1", "construction", date(2021, 1, 15), a, "IL", "Cook", "FAKE Midtown Office LLC",
         "FAKE Lakeshore Bank", 33e6)
    project(con, "fk-mo", "FAKE Midtown Office Center", a, "Chicago", "IL", "office", "Available 2025", 10.0)
    a = "9000 FAKE Gateway Blvd, Dallas, TX"
    loan(con, "fk-gp-1", "construction", date(2021, 3, 1), a, "TX", "Dallas", "FAKE Gateway Retail LLC",
         "FAKE Lone Star Bank", 72e6)
    project(con, "fk-gp", "FAKE Gateway Plaza", a, "Dallas", "TX", "retail", "Opening 2025", 11.0)

    # Drops off next week: a late opening the city later signs off on.
    a = "45 FAKE Maple Ave, Lansing, MI"
    loan(con, "fk-mc-1", "construction", date(2023, 9, 1), a, "MI", "Ingham", "FAKE Maple Commons LLC",
         "FAKE Capital Area Bank", 16e6)
    project(con, "fk-mc", "FAKE Maple Commons", a, "Lansing", "MI", "multifamily", "Opening Spring 2026", 12.0)


def week2(con):
    # Kestrel Hall: a UCC sale notice runs in the WSJ (late signal: small add-on, stage -> actionable).
    filing(con, "fk-kh-ucc", "ucc_sale_notice", "410 FAKE Sample Way, Ann Arbor, MI", "Ann Arbor", "MI",
           "NOTICE OF UCC SALE: 100% of the membership interests in FAKE Kestrel Hall Owner LLC will be sold on "
           "December 10, 2026.", filing_date=date(2026, 11, 5), sale_date=date(2026, 12, 10),
           borrower_name="FAKE Kestrel Hall Owner LLC", project_name="FAKE Kestrel Hall")
    # Maple Commons opens and pays off its construction loan with a permanent loan.
    con.execute("UPDATE projects SET stale_candidate = FALSE, page_still_unopened = FALSE WHERE project_id = 'fk-mc'")
    loan(con, "fk-mc-2", "permanent", date(2026, 11, 4), "45 FAKE Maple Ave, Lansing, MI", "MI", "Ingham",
         "FAKE Maple Commons LLC", "FAKE Great Lakes Life", 15e6)
    # New this week: a construction loan that just crossed 4 years with two extensions.
    a = "700 FAKE Summit Ridge Rd, Grand Rapids, MI"
    loan(con, "fk-sr-1", "construction", date(2022, 10, 1), a, "MI", "Kent", "FAKE Summit Ridge LLC",
         "FAKE West Michigan Bank", 17.5e6)
    loan(con, "fk-sr-2", "extension", date(2025, 10, 1), a, "MI", "Kent", "FAKE Summit Ridge LLC", "FAKE West Michigan Bank")
    loan(con, "fk-sr-3", "modification", date(2026, 11, 3), a, "MI", "Kent", "FAKE Summit Ridge LLC", "FAKE West Michigan Bank")
    project(con, "fk-sr", "FAKE Summit Ridge Apartments", a, "Grand Rapids", "MI", "multifamily",
            "Opening Spring 2026", 7.0)


def research_log(con):
    ids = dict(con.execute("SELECT property_name, property_id FROM weekly_flags WHERE run_id = ?",
                           [f"week-{WEEK2}"]).fetchall())
    entries = [
        ("2026-10-21 10:00", "fake-lakeview", "FAKE Lakeview Apartments (earlier flag)", "deal_closed",
         "Closed a $12.5M bridge loan after a loan-clock + opening-delay flag.", 12_500_000),
        ("2026-11-03 09:30", ids.get("FAKE Kestrel Hall"), "FAKE Kestrel Hall", "looked_into",
         "No certificate of occupancy; construction loan extended.", None),
        ("2026-11-06 14:00", ids.get("FAKE Kestrel Hall"), "FAKE Kestrel Hall", "called",
         "Reached the (sample) principal. Open to a bridge loan.", None),
        ("2026-11-04 11:15", ids.get("FAKE Huron Yards Lofts"), "FAKE Huron Yards Lofts", "looked_into",
         "Loan recorded 2021, nothing since. Checking with the county.", None),
    ]
    for i, (when, pid, name, status, note, amt) in enumerate(entries, 1):
        ins(con, "research_log", log_id=f"demo-{i}", property_id=pid or name, property_name=name, status=status,
            note=note, deal_amount=amt, researcher="Demo researcher", logged_at=datetime.fromisoformat(when))


def build(path: Path = DEMO_DB) -> Path:
    from . import weekly
    from .schema import open_db

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    sh("scripts/setup_db.py", db=path)
    sh("scripts/make_fake_data.py", db=path)
    sh("scripts/make_fake_readings.py", db=path)

    con = open_db(path)
    weekly.create_tables(con)
    week1(con)
    con.close()
    sh("-m", "matcher", "--as-of", WEEK1.isoformat(), db=path)
    con = open_db(path)
    weekly.run(con, None, WEEK1)
    week2(con)
    con.close()
    sh("-m", "matcher", "--as-of", WEEK2.isoformat(), db=path)
    con = open_db(path)
    weekly.run(con, None, WEEK2)
    research_log(con)
    con.close()
    return path


if __name__ == "__main__":
    p = build()
    print(f"Demo database: {p}")
    from .weekly import main
    main(["--db", str(p), "--as-of", WEEK2.isoformat()])
