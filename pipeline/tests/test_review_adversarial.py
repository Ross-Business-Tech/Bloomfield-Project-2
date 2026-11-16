"""Independent-review adversarial tests for the 04f51b8 autonomous loop.
Each test proves one suspected failure mode. Fake data only; no network."""
from datetime import date, datetime

import pytest

from agent import ingest as agent_ingest
from agent import tools as agent_tools
from collectors.common.files import store_file
from matcher.schema import create_matcher_tables
from research import agent_bridge, county_tax, drafts, evidence as ev, gates
from research.schema import create_research_tables
from tests.b_fixtures import bcon  # noqa: F401

PAGE = ("FAKE COUNTY treasurer - parcel FAKE-12 - FAKE PINES NURSING HOME - "
        "2025 taxes billed 10,000 paid 10,000 status paid")
NAME_ONLY_PAGE = "FAKE GAZETTE article: FAKE PINES NURSING HOME reported figures last year."
LIENS_PAGE = ("FAKE STATE UCC and tax lien index - search: FAKE PINES NURSING HOME, "
              "FAKE COUNTY - No records found")


@pytest.fixture
def rcon(con):
    create_research_tables(con)
    create_matcher_tables(con)
    agent_tools.create_tables(con)
    agent_ingest.create_tables(con)
    con.execute("INSERT INTO properties (property_id, property_name, street_address, city, state, "
                "county, parcel_number) VALUES ('prop-fake-1', 'FAKE PINES NURSING HOME', "
                "'12 FAKE LANE', 'FAKE CITY', 'MI', 'FAKE COUNTY', 'FAKE-12')")
    return con


def _run(rcon, run_id, goal="[property_id=prop-fake-1] Research FAKE PINES"):
    rcon.execute("INSERT INTO agent_runs (run_id, goal, status, started_at, steps, llm_calls, "
                 "cost_usd) VALUES (?,?,?,?,0,0,0.0)",
                 [run_id, goal, "done", datetime(2026, 11, 16, 12, 0, 0)])
    return run_id


def _evidence(rcon, run_id, eid, claim, value, notes=None):
    rcon.execute("INSERT INTO evidence (evidence_id, run_id, subject, claim, value, source_kind, "
                 "file_id, source_url, quote, observed_at, notes, created_at) "
                 "VALUES (?,?,?,?,?,'web_page','file-fake-1','https://x.example/f',?, ?, ?, ?)",
                 [eid, run_id, "FAKE PINES", claim, value, "quote words",
                  datetime(2026, 11, 16, 12, 1, 0), notes, datetime(2026, 11, 16, 12, 1, 0)])


def _make_ready(rcon):
    stored = store_file(rcon, PAGE.encode(), source="agent_web", doc_category="web_page",
                        collector_name="test", url="https://county.example/fake")
    agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="county_tax",
        state="MI", county="FAKE COUNTY", tax_year=2025, parcel="FAKE-12",
        billed=10000.0, paid=10000.0, status="paid",
        file_id=stored.file_id, source_url="https://county.example/fake",
        quote="2025 taxes billed 10,000 paid 10,000 status paid")
    liens_page = store_file(rcon, LIENS_PAGE.encode(), source="agent_web",
                            doc_category="web_page", collector_name="test",
                            url="https://sos.example/fake")
    agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="liens",
        state="MI", query="FAKE PINES liens", result_count=0,
        file_id=liens_page.file_id, source_url="https://sos.example/fake",
        quote="search: FAKE PINES NURSING HOME, FAKE COUNTY - No records found")
    gates.evaluate_all(rcon)


# The dashboard's own latest-assessment queries, verbatim from dashboard/app.py.
def _dashboard_reviewed(con):
    rows = con.execute(
        "SELECT property_id, research_score, research_tier, one_line FROM research_assessments "
        "QUALIFY row_number() OVER (PARTITION BY property_id ORDER BY ingested_at DESC) = 1").fetchall()
    return {r[0]: (r[1], (r[2] or "").split(" ")[0], r[3] or "") for r in rows if r[1] is not None}


def _dashboard_conflicts(con):
    rows = con.execute(
        "SELECT property_id FROM research_assessments "
        "QUALIFY row_number() OVER (PARTITION BY property_id ORDER BY ingested_at DESC) = 1 "
        "AND research_score IS NULL").fetchall()
    return {r[0] for r in rows}


def test_1_auto_draft_demotes_a_reviewed_assessment(rcon, bcon):
    """A property with a reviewed Hot assessment gets an auto-draft written on top;
    the dashboard then drops its score and labels it a CONFLICT."""
    _make_ready(rcon)
    con = rcon
    con.execute("INSERT INTO projects (project_id, project_name) VALUES "
                "('prop-fake-1', 'FAKE PINES NURSING HOME')")
    # Pre-existing REVIEWED assessment (what a person's artifact ingest wrote).
    agent_ingest.save_research_assessment(con, {
        "property_id": "prop-fake-1", "research_tier": "Hot", "research_score": 78,
        "confidence": "high", "coverage": "full", "one_line": "reviewed by a person"})
    assert _dashboard_reviewed(con)["prop-fake-1"][0] == 78.0
    assert "prop-fake-1" not in _dashboard_conflicts(con)

    out = drafts.prepare_drafts(con)   # weekly hook writes the auto-draft
    assert out["drafts"] == 1

    reviewed = _dashboard_reviewed(con)
    conflicts = _dashboard_conflicts(con)
    print("DEMOTION CHECK: reviewed =", reviewed, "| conflicts =", conflicts)
    assert "prop-fake-1" in reviewed, "reviewed score survived (no demotion)"
    assert "prop-fake-1" not in conflicts, "property not mislabeled as conflict"


def test_2_billed_only_tax_row_is_not_delinquency(rcon):
    """A tax row with billed but paid UNKNOWN must stay neutral (missing is unknown),
    never a distress/delinquent fact."""
    ev_id = county_tax.record_tax_year(rcon, state="MI", county="FAKE COUNTY", tax_year=2025,
                                       parcel="FAKE-12", property_key="prop-fake-1",
                                       billed=10000.0, paid=None, status=None)
    row = rcon.execute("SELECT direction, caveat FROM research_evidence WHERE evidence_id = ?",
                       [ev_id]).fetchone()
    print("BILLED-ONLY CHECK:", row)
    assert row[0] != "distress", f"billed-only row marked {row[0]}: false delinquency"


def test_3_different_periods_are_not_a_conflict(rcon):
    """Two operating_noi figures for DIFFERENT stated periods are separate facts,
    not a 'sources disagree' conflict."""
    ev.insert_evidence(rcon, ev.Evidence(
        "cost-noi-2024", claim_type="financial_fact", source_name="cms_snf_cost_report",
        field_name="operating_noi", property_key="prop-fake-1", identity_method="ccn",
        identity_key="ccn:999001", period_end=date(2024, 12, 31),
        units="usd_unconfirmed", caveat=ev.COST_REPORT_CAVEAT, value_number=100.0,
        dataset_vintage="fake FY2024"))
    run_id = _run(rcon, "run-fake-2")
    _evidence(rcon, run_id, "ev-p", "operating_noi", "200.0",
              notes="period_end=2025-12-31, period_months=12")
    agent_bridge.sync_run(rcon, run_id, property_key="prop-fake-1",
                          page_text_fn=lambda _f: PAGE)
    conf = rcon.execute("SELECT count(*) FROM research_conflicts WHERE status = 'open'").fetchone()[0]
    print("PERIOD CHECK: open conflicts after different-period figure =", conf)
    assert conf == 0, "different-period figures falsely opened a conflict"


def test_4_resolved_conflict_stays_resolved(rcon):
    """Once a person resolves a bridge conflict, re-running the bridge must not reopen it."""
    ev.insert_evidence(rcon, ev.Evidence(
        "cost-noi", claim_type="financial_fact", source_name="cms_snf_cost_report",
        field_name="operating_noi", property_key="prop-fake-1", identity_method="ccn",
        identity_key="ccn:999001", period_begin=date(2024, 1, 1), period_end=date(2024, 12, 31),
        units="usd_unconfirmed", caveat=ev.COST_REPORT_CAVEAT, value_number=100.0,
        dataset_vintage="fake FY2024"))
    run_id = _run(rcon, "run-fake-3")
    _evidence(rcon, run_id, "ev-r", "operating_noi", "200.0",
              notes="period_end=2024-12-31, period_months=12")
    agent_bridge.sync_run(rcon, run_id, property_key="prop-fake-1",
                          page_text_fn=lambda _f: PAGE)
    ev.resolve_conflict(rcon, "bridge:prop-fake-1:operating_noi", "person reconciled: restatement")
    agent_bridge.sync_run(rcon, run_id, property_key="prop-fake-1",
                          page_text_fn=lambda _f: PAGE)   # same run re-synced (weekly retry)
    row = rcon.execute("SELECT status FROM research_conflicts "
                       "WHERE conflict_id = 'bridge:prop-fake-1:operating_noi'").fetchone()
    print("RESURRECTION CHECK: status after re-sync =", row[0])
    assert row[0] == "resolved", "a person's resolution was overwritten back to open"


def test_5_name_mention_is_not_verified_identity(rcon):
    """A page that merely MENTIONS the property name must not yield a parcel-verified
    identity (the method says a name-only match never verifies identity)."""
    run_id = _run(rcon, "run-fake-4")
    _evidence(rcon, run_id, "ev-n", "occupancy_rate", "0.81")
    agent_bridge.sync_run(rcon, run_id, property_key="prop-fake-1",
                          page_text_fn=lambda _f: NAME_ONLY_PAGE)  # name only, no parcel
    row = rcon.execute("SELECT identity_method, property_key FROM research_evidence "
                       "WHERE evidence_id = 'agent:ev-n'").fetchone()
    r = gates.readiness_for(rcon, "prop-fake-1")
    print("IDENTITY CHECK: stored method =", row[0],
          "| gate identity_verified =", r["gates"]["identity_verified"])
    assert row[0] not in ("ccn", "parcel"), "name-mention fact labeled as verified identity"
    assert r["gates"]["identity_verified"] is False
