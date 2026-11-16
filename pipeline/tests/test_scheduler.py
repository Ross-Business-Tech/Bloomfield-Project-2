"""Tests for the dashboard's optional weekly collector (dashboard/scheduler.py). No network."""
import time
from datetime import date

import pytest

from collectors.common import db
from dashboard import scheduler
from scorer.schema import open_db


@pytest.fixture(autouse=True)
def _clean_scheduler():
    scheduler.stop()
    yield
    scheduler.stop()


def _wait_for_cycle(seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if scheduler.status()["cycles"] >= 1:
            return
        time.sleep(0.1)


def _add(con, table, **row):
    con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                list(row.values()))


def _seed(con):
    _add(con, "recorded_loans", record_id="r1", loan_kind="construction", recording_date=date(2021, 1, 1),
         property_address="1 A St", state="MI", amount=20e6, grantee="Bank", grantor="Owner LLC")
    _add(con, "projects", project_id="p1", project_name="FAKE A", property_address="1 A St", state="MI",
         property_type="multifamily", months_overdue=0.0, stale_candidate=False)


def test_off_by_default(env, monkeypatch):
    monkeypatch.delenv("BLOOMFIELD_SCHEDULER", raising=False)
    st = scheduler.ensure_started()
    assert st["enabled"] is False
    assert st["started"] is False
    scheduler.stop()


def test_cycle_collects_and_scores(env, monkeypatch, tmp_path):
    monkeypatch.setenv("BLOOMFIELD_SCHEDULER", "1")
    monkeypatch.setenv("BLOOMFIELD_SCHEDULER_FIRST_DELAY_SECONDS", "0")
    monkeypatch.setenv("BLOOMFIELD_SCHEDULER_INTERVAL_HOURS", "100000")
    monkeypatch.setenv("BLOOMFIELD_SCHEDULER_ONLY", "inbox")   # inbox only: no website is touched
    db_file = str(tmp_path / "sched.duckdb")
    con = open_db(db_file)
    _seed(con)

    st = scheduler.ensure_started(db_file)
    assert st["enabled"] is True and st["started"] is True
    _wait_for_cycle()
    s = scheduler.status()
    scheduler.stop()

    assert s["cycles"] == 1, s
    assert s["last_ok"] is True, s
    assert s["last_run_id"] == f"week-{date.today().isoformat()}"

    # The seeded property was scored into this week's flags, visible to the dashboard.
    rows = con.execute("SELECT property_name, rank FROM weekly_flags WHERE run_id = ?",
                       [s["last_run_id"]]).fetchall()
    assert ("FAKE A", 1) in rows
    # The collectors logged their run in collector_runs, same as a manual run_all.
    logged = con.execute("SELECT collector_name FROM collector_runs").fetchall()
    assert any("inbox" in r[0] for r in logged)
    # A second writer in the same process (the dashboard saving a research note) works alongside.
    _add(con, "research_log", log_id="n1", property_id="p1", property_name="FAKE A",
         status="looked_into", note="checked it", deal_amount=None, researcher="tester",
         logged_at=db.utc_now())
    con.close()


def test_failing_cycle_is_recorded_not_raised(env, monkeypatch, tmp_path):
    monkeypatch.setenv("BLOOMFIELD_SCHEDULER", "1")
    monkeypatch.setenv("BLOOMFIELD_SCHEDULER_FIRST_DELAY_SECONDS", "0")
    monkeypatch.setenv("BLOOMFIELD_SCHEDULER_INTERVAL_HOURS", "100000")
    monkeypatch.setenv("BLOOMFIELD_SCHEDULER_ONLY", "made_up_collector")
    scheduler.ensure_started(str(tmp_path / "x.duckdb"))
    _wait_for_cycle()
    s = scheduler.status()
    scheduler.stop()
    assert s["cycles"] == 1
    assert s["last_ok"] is False and "made_up_collector" in s["last_error"]
