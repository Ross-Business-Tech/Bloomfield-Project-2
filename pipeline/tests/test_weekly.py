"""Tests for the weekly run behind the dashboard (scorer/weekly.py). Fake data only."""
from datetime import date

from scorer import equation as eq
from scorer import weekly
from scorer.schema import open_db


def _add(con, table, **row):
    con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


def test_weekly_run_filters_ranks_and_compares(tmp_path):
    con = open_db(tmp_path / "t.duckdb")
    weekly.create_tables(con)
    for rid, d, addr, st, amt, ptype in [
        ("a", date(2021, 1, 1), "1 A St", "MI", 20e6, "multifamily"),    # standout, shown
        ("b", date(2021, 1, 1), "2 B St", "IL", 20e6, "office"),         # office: filtered out
        ("c", date(2021, 1, 1), "3 C St", "TX", 90e6, "retail"),         # too big: filtered out
    ]:
        _add(con, "recorded_loans", record_id=rid, loan_kind="construction", recording_date=d,
             property_address=addr, state=st, amount=amt, grantee="Bank", grantor="Owner LLC")
        _add(con, "projects", project_id="p" + rid, project_name="FAKE " + rid.upper(), property_address=addr,
             state=st, property_type=ptype, months_overdue=0.0, stale_candidate=False)
    weekly.run(con, eq.load_equation_config(), date(2026, 11, 2))
    _add(con, "recorded_loans", record_id="d", loan_kind="construction", recording_date=date(2021, 1, 1),
         property_address="4 D St", state="OH", amount=10e6, grantee="Bank", grantor="Owner LLC")
    run2 = weekly.run(con, eq.load_equation_config(), date(2026, 11, 9))
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT property_name, rank, shown, flags FROM weekly_flags WHERE run_id = ?", [run2]).fetchall()}
    assert rows["FAKE A"][0] == 1 and rows["FAKE A"][2] == "STANDOUT"
    assert not rows["FAKE B"][1] and not rows["FAKE C"][1]
    ch = weekly.changes(con, run2)
    assert len(ch["new"]) == 1 and ch["previous"] == "week-2026-11-02"
