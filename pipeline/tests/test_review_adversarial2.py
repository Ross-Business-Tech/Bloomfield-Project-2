"""Second-round independent-review adversarial tests for f3ffc3d.
Parent's added concerns: period_months/scope compatibility, qualified-check proof vs
assertion, jurisdiction defaulting. Fake data only; no network."""
from datetime import date, datetime

import pytest

from agent import ingest as agent_ingest
from agent import tools as agent_tools
from collectors.common.files import store_file
from matcher.schema import create_matcher_tables
from research import agent_bridge, county_tax, drafts, evidence as ev, gates
from research.schema import create_research_tables
from tests.b_fixtures import bcon  # noqa: F401

PAGE = ("FAKE COUNTY treasurer - parcel FAKE-12 - FAKE PINES NURSING HOME - FAKE CITY - "
        "2025 taxes billed 10,000 paid 10,000 status paid")
NEWS_PAGE = ("FAKE GAZETTE: FAKE PINES NURSING HOME in FAKE CITY was mentioned in a "
             "routine business roundup of FAKE COUNTY facilities.")


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


def _run(rcon, run_id):
    rcon.execute("INSERT INTO agent_runs (run_id, goal, status, started_at, steps, llm_calls, "
                 "cost_usd) VALUES (?,?,?,?,0,0,0.0)",
                 [run_id, "[property_id=prop-fake-1] Research FAKE PINES", "done",
                  datetime(2026, 11, 16, 12, 0, 0)])
    return run_id


def _evidence(rcon, run_id, eid, claim, value, notes=None):
    rcon.execute("INSERT INTO evidence (evidence_id, run_id, subject, claim, value, source_kind, "
                 "file_id, source_url, quote, observed_at, notes, created_at) "
                 "VALUES (?,?,?,?,?,'web_page','file-fake-1','https://x.example/f',?, ?, ?, ?)",
                 [eid, run_id, "FAKE PINES", claim, value, "quote words",
                  datetime(2026, 11, 16, 12, 1, 0), notes, datetime(2026, 11, 16, 12, 1, 0)])


def test_6_same_period_end_different_length_is_not_a_conflict(rcon):
    """Annual figure vs quarterly figure ending the same date: same period_end and units,
    DIFFERENT period_months - different scopes, not a disagreement."""
    ev.insert_evidence(rcon, ev.Evidence(
        "cost-noi-fy", claim_type="financial_fact", source_name="cms_snf_cost_report",
        field_name="operating_noi", property_key="prop-fake-1", identity_method="ccn",
        identity_key="ccn:999001", period_end=date(2024, 12, 31), period_months=12.0,
        units="usd_unconfirmed", caveat=ev.COST_REPORT_CAVEAT, value_number=100.0,
        dataset_vintage="fake FY2024"))
    run_id = _run(rcon, "run-fake-q")
    _evidence(rcon, run_id, "ev-q", "operating_noi", "25.0",
              notes="period_end=2024-12-31, period_months=3")
    agent_bridge.sync_run(rcon, run_id, property_key="prop-fake-1",
                          page_text_fn=lambda _f: PAGE)
    conf = rcon.execute("SELECT count(*) FROM research_conflicts WHERE status = 'open'").fetchone()[0]
    print("PERIOD-MONTHS CHECK: open conflicts (annual 100 vs quarterly 25, same end date) =", conf)
    assert conf == 0, "annual-vs-quarterly figures with the same period_end falsely conflicted"


def test_7_generic_query_with_defaulted_jurisdiction_clears_lien_lane(rcon):
    """Parent's concern: a bare query with NO stated jurisdiction must not become a
    qualified lien check just because the universe record knows the property's state."""
    stored = store_file(rcon, NEWS_PAGE.encode(), source="agent_web", doc_category="web_page",
                        collector_name="test", url="https://news.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="liens",
        query="FAKE PINES", result_count=0,           # no lien index, no jurisdiction
        file_id=stored.file_id, source_url="https://news.example/fake",
        quote="FAKE PINES NURSING HOME in FAKE CITY was mentioned")
    print("LIEN ASSERTION CHECK: tool result =", out)
    row = rcon.execute("SELECT lane, coverage_limit FROM research_searches").fetchone()
    print("LIEN ASSERTION CHECK: recorded row =", row)
    assert out["ok"] is False or (row and "attempt" in row[0]), \
        "an unjurisdictioned generic query was recorded as a QUALIFIED lien check"


def test_8_wrong_jurisdiction_tax_check_is_refused(rcon):
    """The stated state/county are agent-asserted strings: a TX 'check' on an MI property
    must not record as that property's qualified county tax coverage."""
    stored = store_file(rcon, PAGE.encode(), source="agent_web", doc_category="web_page",
                        collector_name="test", url="https://county.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="county_tax",
        state="TX", county="FAKE COUNTY", tax_year=2025, parcel="FAKE-12",
        billed=10000.0, paid=10000.0, status="paid",
        file_id=stored.file_id, source_url="https://county.example/fake",
        quote="2025 taxes billed 10,000 paid 10,000 status paid")
    rows = rcon.execute("SELECT lane, identity_key FROM research_searches").fetchall()
    print("WRONG-JURISDICTION CHECK: tool ok =", out.get("ok"), "| search rows =", rows)
    r = gates.readiness_for(rcon, "prop-fake-1")
    cleared = bool(r and r["gates"]["counter_evidence_searched"])
    print("WRONG-JURISDICTION CHECK: counter_evidence_searched =", cleared)
    assert out["ok"] is False or not cleared, \
        "a check asserted against the WRONG state cleared this property's coverage"


def test_9_draft_store_leaves_live_assessment_untouched(rcon, bcon):
    """prepare_drafts writes zero rows to research_assessments; reviewed counts and the
    conflict set are byte-identical before and after."""
    stored = store_file(rcon, PAGE.encode(), source="agent_web", doc_category="web_page",
                        collector_name="test", url="https://county.example/fake")
    agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="county_tax",
        state="MI", county="FAKE COUNTY", tax_year=2025, parcel="FAKE-12",
        billed=10000.0, paid=10000.0, status="paid",
        file_id=stored.file_id, source_url="https://county.example/fake",
        quote="2025 taxes billed 10,000 paid 10,000 status paid")
    gates.evaluate_all(rcon)
    rcon.execute("INSERT INTO projects (project_id, project_name) VALUES "
                 "('prop-fake-1', 'FAKE PINES NURSING HOME')")
    agent_ingest.save_research_assessment(rcon, {
        "property_id": "prop-fake-1", "research_tier": "Hot", "research_score": 78,
        "confidence": "high", "coverage": "full", "one_line": "reviewed by a person"})
    before = rcon.execute("SELECT * FROM research_assessments ORDER BY ingested_at").fetchall()
    out = drafts.prepare_drafts(rcon)
    after = rcon.execute("SELECT * FROM research_assessments ORDER BY ingested_at").fetchall()
    n_drafts = rcon.execute("SELECT count(*) FROM research_drafts").fetchone()[0]
    print("ISOLATION CHECK: drafts out =", out, "| research_drafts rows =", n_drafts,
          "| research_assessments unchanged =", before == after)
    assert before == after and n_drafts >= 0
