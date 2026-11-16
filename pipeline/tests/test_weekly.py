"""Tests for the weekly run behind the dashboard (scorer/weekly.py). Fake data only."""
from datetime import date

import pytest

from scorer import equation as eq
from scorer import weekly
from scorer.schema import open_db


@pytest.fixture(autouse=True)
def _tests_use_made_up_data(monkeypatch):
    monkeypatch.setenv("BLOOMFIELD_INCLUDE_DEMO", "1")      # these tests use FAKE properties; real runs leave them out


def _add(con, table, **row):
    con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


def hide_office_cfg():
    cfg = eq.load_equation_config()
    cfg["filters"] = {**cfg["filters"], "property_types_excluded": ["Office"]}
    return cfg


def test_weekly_run_filters_ranks_and_compares(tmp_path):
    con = open_db(tmp_path / "t.duckdb")
    weekly.create_tables(con)
    for rid, d, addr, st, amt, ptype in [
        ("a", date(2021, 1, 1), "1 A St", "MI", 20e6, "multifamily"),    # standout, shown
        ("b", date(2021, 1, 1), "2 B St", "IL", 20e6, "office"),         # office: filtered out
        ("c", date(2021, 1, 1), "3 C St", "TX", 1.9e6, "retail"),        # below the $2.5M minimum: filtered out
    ]:
        _add(con, "recorded_loans", record_id=rid, loan_kind="construction", recording_date=d,
             property_address=addr, state=st, amount=amt, grantee="Bank", grantor="Owner LLC")
        _add(con, "projects", project_id="p" + rid, project_name="FAKE " + rid.upper(), property_address=addr,
             state=st, property_type=ptype, months_overdue=0.0, stale_candidate=False)
    weekly.run(con, hide_office_cfg(), date(2026, 11, 2))
    _add(con, "recorded_loans", record_id="d", loan_kind="construction", recording_date=date(2021, 1, 1),
         property_address="4 D St", state="OH", amount=10e6, grantee="Bank", grantor="Owner LLC")
    run2 = weekly.run(con, hide_office_cfg(), date(2026, 11, 9))
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT property_name, rank, shown, flags FROM weekly_flags WHERE run_id = ?", [run2]).fetchall()}
    assert rows["FAKE A"][0] == 1 and rows["FAKE A"][2] == "STANDOUT"
    assert not rows["FAKE B"][1] and not rows["FAKE C"][1]
    ch = weekly.changes(con, run2)
    assert len(ch["new"]) == 1 and ch["previous"] == "week-2026-11-02"


def _cmbs_loan(con, key, addr, amount, name, **kw):
    _add(con, "loans", loan_key=key, source="sec_edgar", deal_name="TEST TRUST 2026-T1", property_name=name,
         property_address=addr, property_state="MI", property_type="MF", current_balance=amount,
         original_balance=amount, report_period=date(2026, 10, 16), **kw)


def test_equal_scores_rank_biggest_loan_first(tmp_path):
    """Two score-0 CMBS properties with the same deal quality: the bigger loan ranks first, not a hash order."""
    con = open_db(tmp_path / "t.duckdb")
    weekly.create_tables(con)
    _cmbs_loan(con, "edgar:T:small", "1 Small St", 3e6, "Small Plaza", dscr=1.4)
    _cmbs_loan(con, "edgar:T:big", "2 Big St", 90e6, "Big Plaza", dscr=1.4)
    run_id = weekly.run(con, None, date(2026, 11, 10))
    rows = con.execute("SELECT property_name, rank, score FROM weekly_flags WHERE run_id = ? ORDER BY rank",
                       [run_id]).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("Big Plaza", 1), ("Small Plaza", 2)]
    assert all(r[2] == 0 for r in rows)                      # still score 0: the order comes from loan size


def test_cmbs_payment_status_is_decoded_in_context(tmp_path):
    """CREFC status codes are shown as plain English (context only, still not scored)."""
    con = open_db(tmp_path / "t.duckdb")
    weekly.create_tables(con)
    _cmbs_loan(con, "edgar:T:late", "3 Late St", 5e6, "Late Plaza", payment_status="3")
    _cmbs_loan(con, "edgar:T:ok", "4 Ok St", 5e6, "Ok Plaza", payment_status="0")
    run_id = weekly.run(con, None, date(2026, 11, 10))
    ctx = dict(con.execute("SELECT property_name, context FROM weekly_flags WHERE run_id = ?", [run_id]).fetchall())
    assert "90-120 days delinquent" in ctx["Late Plaza"] and "payment status 3" not in ctx["Late Plaza"]
    assert "payments current" in ctx["Ok Plaza"]


def test_occupancy_slide_from_edgar_history_scores_above_zero(tmp_path):
    """The occupancy history the Edgar collector appends feeds the Q signal: a real slide lifts the score."""
    con = open_db(tmp_path / "t.duckdb")
    weekly.create_tables(con)
    _cmbs_loan(con, "edgar:T:slide", "5 Slide St", 5e6, "Slide Plaza", occupancy=0.60)
    _add(con, "operating_metrics", link_id="edgar:T:slide", period_end=date(2026, 8, 16), period_months=1,
         occupancy=0.95, source="sec_edgar")
    _add(con, "operating_metrics", link_id="edgar:T:slide", period_end=date(2026, 10, 16), period_months=1,
         occupancy=0.60, source="sec_edgar")
    run_id = weekly.run(con, None, date(2026, 11, 10))
    score, = con.execute("SELECT score FROM weekly_flags WHERE run_id = ?", [run_id]).fetchone()
    assert 0 < score <= 30                                    # one creative signal, anti-fluked: watch-level, not hot


def test_there_is_no_upper_limit_and_the_minimum_is_two_and_a_half_million():
    f = eq.load_equation_config()["filters"]
    assert f["min_loan_size"] == 2_500_000 and f["max_loan_size"] is None
    assert weekly.passes_filters("retail", "TX", 2.5e6, f)[0]                # exactly the minimum is in
    assert not weekly.passes_filters("retail", "TX", 2.4e6, f)[0]            # just under is out
    for big in (50e6, 500e6, 5e9):                                           # any size above the minimum is in
        assert weekly.passes_filters("multifamily", "MI", big, f)[0]
    assert weekly.passes_filters("multifamily", "MI", None, f)[0]            # unknown size is kept, marked as such
    f2 = {**f, "max_loan_size": 100e6}                                       # a cap still works if someone sets one
    assert not weekly.passes_filters("multifamily", "MI", 150e6, f2)[0]


def test_only_real_office_is_hidden_as_office():
    ct = weekly.clean_type
    assert ct("Mixed-use (retail/office)") == "Mixed use"       # was wrongly "Office", so it was filtered out
    assert ct("hotel and office tower") == "Hotel"
    assert ct("Industrial / Office flex") == "Industrial"
    assert ct("Retail with office above") == "Retail"
    assert ct("Class A office") == "Office" and ct("OF") == "Office" and ct("Medical Office Building") == "Office"
    f = eq.load_equation_config()["filters"]
    hide = hide_office_cfg()["filters"]
    assert weekly.passes_filters(ct("Mixed-use (retail/office)"), "MI", 20e6, hide)[0]
    assert not weekly.passes_filters(ct("Class A office"), "MI", 20e6, hide)[0]    # hidden only when the switch is on
    assert weekly.passes_filters(ct("Class A office"), "MI", 20e6, f)[0]           # shown by default


def test_the_run_explains_when_everything_is_filtered_out(tmp_path):
    con = open_db(tmp_path / "t.duckdb")
    weekly.create_tables(con)
    for i in range(3):
        _add(con, "recorded_loans", record_id=f"x{i}", loan_kind="construction", recording_date=date(2021, 1, 1),
             property_address=f"{i} X St", state="MI", amount=20e6, grantee="Bank", grantor="Owner LLC")
        _add(con, "projects", project_id=f"px{i}", project_name=f"FAKE X{i}", property_address=f"{i} X St",
             state="MI", property_type="office", months_overdue=0.0, stale_candidate=False)
    run_id = weekly.run(con, hide_office_cfg(), date(2026, 11, 2))
    fs = weekly.filter_summary(con, run_id)
    assert fs["shown"] == 0 and fs["hidden"] == 3 and fs["reasons"] == {"Office is filtered out": 3}


def test_office_is_shown_by_default_and_can_be_hidden(tmp_path):
    f = eq.load_equation_config()["filters"]
    assert f["property_types_excluded"] == []                                      # nothing is hidden by default
    con = open_db(tmp_path / "t.duckdb")
    weekly.create_tables(con)
    _add(con, "recorded_loans", record_id="o", loan_kind="construction", recording_date=date(2021, 1, 1),
         property_address="1 O St", state="MI", amount=20e6, grantee="Bank", grantor="Owner LLC")
    _add(con, "projects", project_id="po", project_name="FAKE OFFICE", property_address="1 O St", state="MI",
         property_type="office", months_overdue=0.0, stale_candidate=False)
    run_id = weekly.run(con, eq.load_equation_config(), date(2026, 11, 2))
    shown, rank = con.execute("SELECT shown, rank FROM weekly_flags WHERE run_id = ?", [run_id]).fetchone()
    assert shown and rank == 1                                                     # the office property is on the list
    # ...but it does not earn the "a type Bloomfield buys" deal-quality points
    row = con.execute("SELECT deal_parts FROM weekly_flags WHERE run_id = ?", [run_id]).fetchone()[0]
    assert "Property type" not in row


def test_zero_signal_properties_are_scored_and_kept_not_dropped(tmp_path):
    """Score EVERYTHING: a property with no signals and no context is still scored and stored -
    'nothing found yet' is an honest, queryable result - it just stays off the ranked list."""
    con = open_db(tmp_path / "t.duckdb")
    weekly.create_tables(con)
    _add(con, "recorded_loans", record_id="quiet1", loan_kind="refinance", recording_date=date(2010, 1, 1),
         property_address="1 Quiet Way", state="MI", amount=5e6, grantee="Bank", grantor="Owner LLC")
    run_id = weekly.run(con, eq.load_equation_config(), date(2026, 11, 2))
    row = con.execute("SELECT score, shown, filter_reason, rank FROM weekly_flags WHERE run_id = ?", [run_id]).fetchone()
    assert row is not None, "a zero-signal property must still be scored and stored, never silently dropped"
    score, shown, reason, rank = row
    assert score == 0 and not shown and rank is None
    assert reason == "no distress signals or context yet (missing data is not proof of absence)"


def test_properties_known_only_from_public_loan_data_are_listed_not_hidden(tmp_path):
    """Real SEC data gives loans with no early warning signs (score 0). They pass the filters, so they must be listed."""
    con = open_db(tmp_path / "t.duckdb")
    weekly.create_tables(con)
    for i, bal in enumerate([60e6, 10e6, 2.4e6, 3.1e6]):
        _add(con, "loans", loan_key=f"edgar:T{i}:{i}", source="sec_edgar", deal_name=f"FAKE Trust {i}",
             property_name=f"FAKE Promenade {i}", property_address=f"{i} Promenade Blvd", property_city="Rogers",
             property_state="AR", property_type="RT", current_balance=bal, original_balance=bal, dscr=1.82)
    _add(con, "recorded_loans", record_id="sig", loan_kind="construction", recording_date=date(2021, 1, 1),
         property_address="9 Signal St", state="MI", amount=20e6, grantee="Bank", grantor="Owner LLC")
    run_id = weekly.run(con, eq.load_equation_config(), date(2026, 11, 2))
    rows = con.execute("SELECT property_name, shown, rank, score, filter_reason FROM weekly_flags WHERE run_id = ? "
                       "ORDER BY rank NULLS LAST", [run_id]).fetchall()
    shown = [r for r in rows if r[1]]
    assert [r[0] for r in rows if r[2] is not None][0] != "FAKE Promenade 0"        # a property with a signal ranks first
    assert all(r[2] is not None for r in shown)                                      # every shown property has a rank (is listed)
    names = {r[0] for r in shown}
    assert {"FAKE Promenade 0", "FAKE Promenade 1", "FAKE Promenade 3"} <= names     # $60M is no longer "too big"
    hidden = [r for r in rows if not r[1]]
    assert [r[0] for r in hidden] == ["FAKE Promenade 2"] and "below the $2.5M" in hidden[0][4]   # only the $2.4M loan is hidden


def test_interrupted_publish_preserves_last_completed_run(tmp_path, monkeypatch):
    """A failure inside the publish transaction (e.g. deploy kill surfaced as an error) must
    roll back: the last completed run stays fully visible in weekly_runs and weekly_flags."""
    con = open_db(tmp_path / "t.duckdb")
    weekly.create_tables(con)
    _add(con, "recorded_loans", record_id="a", loan_kind="construction", recording_date=date(2021, 1, 1),
         property_address="1 A St", state="MI", amount=20e6, grantee="Bank", grantor="Owner LLC")
    _add(con, "projects", project_id="pa", project_name="FAKE A", property_address="1 A St",
         state="MI", property_type="multifamily", months_overdue=0.0, stale_candidate=False)
    run1 = weekly.run(con, eq.load_equation_config(), date(2026, 11, 2))
    before = con.execute("SELECT * FROM weekly_flags WHERE run_id = ?", [run1]).fetchall()

    from collectors.common import db as _db
    def boom():
        raise RuntimeError("deploy killed the container here")
    monkeypatch.setattr(_db, "utc_now", boom)          # fails inside the publish transaction
    with pytest.raises(RuntimeError):
        weekly.run(con, eq.load_equation_config(), date(2026, 11, 9))

    assert con.execute("SELECT run_id FROM weekly_runs").fetchall() == [(run1,)]
    assert con.execute("SELECT * FROM weekly_flags WHERE run_id = ?", [run1]).fetchall() == before
    assert con.execute("SELECT count(*) FROM weekly_flags WHERE run_id = 'week-2026-11-09'").fetchone()[0] == 0
    assert weekly.changes(con, run1)["current"] == run1          # app-facing view unaffected


def test_process_kill_mid_swap_preserves_last_completed_run(tmp_path):
    """A real process kill INSIDE the publish transaction (deploy swap) must leave the last
    completed run visible: DuckDB rolls the uncommitted swap back on next open."""
    import subprocess, sys, textwrap
    con = open_db(tmp_path / "t.duckdb")
    weekly.create_tables(con)
    _add(con, "recorded_loans", record_id="a", loan_kind="construction", recording_date=date(2021, 1, 1),
         property_address="1 A St", state="MI", amount=20e6, grantee="Bank", grantor="Owner LLC")
    _add(con, "projects", project_id="pa", project_name="FAKE A", property_address="1 A St",
         state="MI", property_type="multifamily", months_overdue=0.0, stale_candidate=False)
    run1 = weekly.run(con, eq.load_equation_config(), date(2026, 11, 2))
    n1 = con.execute("SELECT count(*) FROM weekly_flags WHERE run_id = ?", [run1]).fetchone()[0]
    con.close()

    killer = textwrap.dedent("""
        import sys, os
        from datetime import date
        from collectors.common import db as _db
        from scorer.schema import open_db
        from scorer import weekly, equation as eq
        _db.utc_now = lambda: os._exit(9)          # die inside the publish transaction
        con = open_db(sys.argv[1])
        weekly.create_tables(con)
        weekly.run(con, eq.load_equation_config(), date(2026, 11, 9))
    """)
    r = subprocess.run([sys.executable, "-c", killer, str(tmp_path / "t.duckdb")],
                       capture_output=True, text=True)
    assert r.returncode != 0, f"killer process should have died: {r.stdout}{r.stderr}"

    con = open_db(tmp_path / "t.duckdb")
    assert con.execute("SELECT run_id FROM weekly_runs").fetchall() == [(run1,)]
    assert con.execute("SELECT count(*) FROM weekly_flags WHERE run_id = ?", [run1]).fetchone()[0] == n1
    assert con.execute("SELECT count(*) FROM weekly_flags WHERE run_id = 'week-2026-11-09'").fetchone()[0] == 0
    assert weekly.changes(con, run1)["current"] == run1
