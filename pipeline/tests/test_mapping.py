"""The evidence-to-scorer mapping: only verified, exactly-typed facts become scorer inputs."""
from datetime import date

import duckdb
import pytest

from agent import mapping, tools
from matcher.schema import SCHEMA_SQL as MATCHER_SCHEMA
from scorer import equation as eq


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(eq.INPUT_SCHEMA)
    c.execute(MATCHER_SCHEMA)
    tools.create_tables(c)
    c.execute("CREATE TABLE projects (project_id TEXT, project_name TEXT)")
    c.execute("INSERT INTO projects VALUES ('proj-1', 'Maple Court')")
    c.execute("INSERT INTO property_links VALUES ('prop-1', 'projects', 'proj-1', 'address', 100.0, now())")
    return c


def _evidence(con, claim, value=None, notes="", kind="web_page", file_id="f1",
              url="https://example.com/page", quote="see page", db_query=None, subject="prop-1"):
    con.execute(
        "INSERT INTO evidence (evidence_id, run_id, subject, claim, value, source_kind, file_id, "
        "source_url, quote, db_query, observed_at, notes, created_at) "
        "VALUES (?, 'r1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())",
        [f"ev-{claim}-{value}-{url}", subject, claim, value, kind, file_id, url, quote, db_query,
         date(2026, 11, 10), notes])


def test_verified_asking_rent_becomes_a_rent_observation(con):
    _evidence(con, "asking_rent", "1850", "unit_basis=unit; lease_term_months=12",
              quote="Now leasing from $1,850/mo")
    _evidence(con, "asking_rent", "24", "unit_basis=sqft", quote="$24/SF NNN",
              url="https://example.com/sqft")
    assert mapping.apply(con)["mapped"] == 2          # unit and sqft are different metrics: both land
    row = con.execute("SELECT project_id, observed_at, asking_rent, unit_basis, source_url, file_id "
                      "FROM rent_observations WHERE unit_basis = 'unit'").fetchone()
    assert row == ("proj-1", date(2026, 11, 10), 1850.0, "unit", "https://example.com/page", "f1")
    assert mapping.apply(con)["mapped"] == 0          # idempotent: corroboration, not new rows


def test_value_must_appear_in_the_verbatim_quote(con):
    _evidence(con, "asking_rent", "2400", quote="Now leasing from $1,850/mo")   # 2400 is not on the page
    out = mapping.apply(con)
    assert out["mapped"] == 0 and "does not appear in the verbatim quote" in next(iter(out["skipped"]))


def test_units_and_context_are_validated_not_just_the_number(con):
    _evidence(con, "asking_rent", "1850", "", quote="Now leasing from $1,850/mo")          # no unit_basis
    _evidence(con, "asking_rent", "9", "unit_basis=unit", quote="only $9 to apply",       # implausible rent
              url="https://example.com/other")
    out = mapping.apply(con)
    assert out["mapped"] == 0
    reasons = " | ".join(out["skipped"])
    assert "unit_basis" in reasons and "sane range" in reasons
    assert con.execute("SELECT count(*) FROM rent_observations").fetchone()[0] == 0


def test_unverified_or_untyped_evidence_is_never_mapped(con):
    _evidence(con, "asking_rent", "1850", file_id=None, url=None, quote=None)      # no proof
    _evidence(con, "The lobby was renovated in 2024")                              # narrative claim
    _evidence(con, "asking_rent", "1850", subject="prop-unknown", quote="$1,850")  # no project link
    out = mapping.apply(con)
    assert out["mapped"] == 0
    assert con.execute("SELECT count(*) FROM rent_observations").fetchone()[0] == 0


def test_existing_observation_is_not_duplicated_or_overwritten(con):
    con.execute("INSERT INTO rent_observations (project_id, observed_at, asking_rent, source_url) "
                "VALUES ('proj-1', ?, 1900.0, 'https://scout/page')", [date(2026, 11, 10)])
    _evidence(con, "asking_rent", "1850", "unit_basis=unit", quote="$1,850/mo")     # same day: corroborates, does not add
    assert mapping.apply(con)["mapped"] == 0
    rows = con.execute("SELECT asking_rent FROM rent_observations").fetchall()
    assert rows == [(1900.0,)]                                    # the original reading stands


def test_market_rent_index_and_permit_inspection(con):
    _evidence(con, "market_rent_index", "1450", "city=Austin; state=TX; period=2026-11-01",
              kind="database", file_id=None, url=None, quote=None,
              db_query="SELECT rent FROM hud_fmr WHERE ...", subject="market")
    _evidence(con, "permit_inspection", None,
              "permit_number=2026-001; city=Austin; state=TX; inspection_date=2026-10-03; "
              "inspection_type=framing; result=passed", quote="Framing inspection passed 10/03")
    out = mapping.apply(con)
    assert out["mapped"] == 2
    assert con.execute("SELECT city, rent_index FROM market_rents").fetchone() == ("Austin", 1450.0)
    assert con.execute("SELECT permit_number, result FROM permit_inspections").fetchone() == \
        ("2026-001", "passed")


def test_database_evidence_needs_its_query(con):
    _evidence(con, "market_rent_index", "1450", "city=Austin; state=TX; period=2026-11-01",
              kind="database", file_id=None, url=None, quote=None, db_query=None, subject="market")
    assert mapping.apply(con)["mapped"] == 0
