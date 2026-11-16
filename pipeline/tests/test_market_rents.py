"""Tests for the market_rents collector. Fake CSVs; no network; every place name is FAKE."""
import csv
import io

from collectors import market_rents
from collectors.common import db
from scorer.equation import create_equation_tables
from tests.conftest import FakeResponse, FakeSession

URL = "https://files.example.com/zori.csv"

HEADER = ["RegionID", "SizeRank", "RegionName", "RegionType", "StateName", "State", "Metro",
          "CountyName", "2026-08-31", "2026-09-30", "2026-10-31"]


def make_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    w.writerows(rows)
    return buf.getvalue()


def fake_cfg(tmp_path, verified=False, months=2):
    (tmp_path / "sources.yaml").write_text(
        "user_agent_template: test {email}\ncontact_email_default: t@example.org\n"
        "defaults: {seconds_between_requests: 0, timeout_seconds: 5, max_retries: 0}\n"
        "lookback_days: {first_run: 365, weekly_run: 14}\n"
        f"market_rents: {{months: {months}, sources: [{{name: Fake ZORI, url: {URL!r}, "
        f"verified_terms: {'true' if verified else 'false'}}}]}}\n")
    return tmp_path


def test_zori_rows_land_and_refresh_is_idempotent(con, env, tmp_path, monkeypatch):
    create_equation_tables(con)
    fake_cfg(tmp_path, verified=True)
    body = make_csv([
        ["1", "1", "Fake City", "city", "Michigan", "MI", "Fake Metro", "Fake County", "2000", "2100", "2200"],
        ["2", "2", "Fake Town", "city", "Texas", "TX", "Fake Metro", "Fake County", "1500", "", "1600"],
    ])
    session = FakeSession({URL: FakeResponse(body, content_type="text/csv")})
    cfg = market_rents.config.load_sources_config(tmp_path / "sources.yaml")
    monkeypatch.setattr(market_rents.config, "load_sources_config", lambda *a, **k: cfg)
    market_rents.collect(con, session=session)
    rows = con.execute("SELECT city, state, period, rent_index, source FROM market_rents ORDER BY 1, 3").fetchall()
    # 2 months kept (months: 2); Fake Town's blank September cell is skipped.
    assert [(r[0], r[1], str(r[2]), r[3], r[4]) for r in rows] == [
        ("Fake City", "MI", "2026-09-30", 2100.0, "zillow_zori"),
        ("Fake City", "MI", "2026-10-31", 2200.0, "zillow_zori"),
        ("Fake Town", "TX", "2026-10-31", 1600.0, "zillow_zori")]
    market_rents.collect(con, session=session)             # a second run replaces, never duplicates
    assert con.execute("SELECT count(*) FROM market_rents").fetchone()[0] == 3


def test_unverified_terms_stay_on_the_manual_inbox(con, env, tmp_path, monkeypatch):
    create_equation_tables(con)
    fake_cfg(tmp_path, verified=False)
    cfg = market_rents.config.load_sources_config(tmp_path / "sources.yaml")
    monkeypatch.setattr(market_rents.config, "load_sources_config", lambda *a, **k: cfg)
    session = FakeSession({URL: FakeResponse(make_csv([]))})
    run = market_rents.collect(con, session=session)
    assert con.execute("SELECT count(*) FROM market_rents").fetchone()[0] == 0
    assert URL not in session.requested                    # nothing was downloaded
    assert any("terms of use" in reason for _, reason in run.fallbacks)


def test_a_csv_with_the_wrong_columns_is_an_error_not_a_crash(con, env, tmp_path, monkeypatch):
    create_equation_tables(con)
    fake_cfg(tmp_path)
    cfg = market_rents.config.load_sources_config(tmp_path / "sources.yaml")
    monkeypatch.setattr(market_rents.config, "load_sources_config", lambda *a, **k: cfg)
    session = FakeSession({URL: FakeResponse("a,b,c\n1,2,3\n")})
    market_rents.collect(con, session=session)
    assert con.execute("SELECT count(*) FROM market_rents").fetchone()[0] == 0
