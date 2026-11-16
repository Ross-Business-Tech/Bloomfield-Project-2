"""Adversarial tests for the autonomous loop: the agent bridge, qualified checks and
assessment drafts. Fake data only; no network; every property and page is FAKE."""
from datetime import date, datetime

import pytest

from agent import ingest as agent_ingest
from agent import tools as agent_tools
from agent import weekly as agent_weekly
from collectors.common.files import store_file
from matcher.schema import create_matcher_tables
from research import agent_bridge, drafts, evidence as ev, gates
from research.schema import create_research_tables
from tests.b_fixtures import bcon  # noqa: F401  (pytest fixture: adds projects table)

PAGE = ("FAKE COUNTY treasurer - parcel FAKE-12 - FAKE PINES NURSING HOME - "
        "2025 taxes billed 10,000 paid 10,000 status paid")
LIENS_PAGE = ("FAKE STATE UCC and tax lien index - search: FAKE PINES NURSING HOME, "
              "FAKE COUNTY - No records found")
OTHER_PAGE = "FAKE COUNTY treasurer - parcel OTHER-99 - SOMEWHERE ELSE MANOR - taxes due"


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


def _run(rcon, run_id="run-fake-1", goal="[property_id=prop-fake-1] Research FAKE PINES"):
    rcon.execute("INSERT INTO agent_runs (run_id, goal, status, started_at, steps, llm_calls, "
                 "cost_usd) VALUES (?,?,?,?,0,0,0.0)",
                 [run_id, goal, "done", datetime(2026, 11, 16, 12, 0, 0)])
    return run_id


def _evidence(rcon, run_id, eid, claim, value="100", quote="figure as stated", notes=None,
              file_id="file-fake-1"):
    rcon.execute("INSERT INTO evidence (evidence_id, run_id, subject, claim, value, source_kind, "
                 "file_id, source_url, quote, observed_at, notes, created_at) "
                 "VALUES (?,?,?,?,?,'web_page',?,?,?,?,?,?)",
                 [eid, run_id, "FAKE PINES", claim, value, file_id,
                  "https://county.example/fake", quote, datetime(2026, 11, 16, 12, 1, 0), notes,
                  datetime(2026, 11, 16, 12, 1, 0)])


def _search_step(rcon, run_id, step, query):
    rcon.execute("INSERT INTO agent_steps (run_id, step, kind, name, detail, created_at) "
                 "VALUES (?,?,'tool_call','web_search',?,?)",
                 [run_id, step, query, datetime(2026, 11, 16, 12, 2, 0)])


def _sync(rcon, run_id, page_text=PAGE):
    return agent_bridge.sync_run(rcon, run_id, property_key="prop-fake-1",
                                 page_text_fn=lambda _fid: page_text)


# --- search attempts are not qualified coverage --------------------------------
def test_tax_words_in_query_do_not_clear_counter_evidence_gate(rcon):
    run_id = _run(rcon)
    _search_step(rcon, run_id, 1, "FAKE PINES county tax delinquent lien search")
    _sync(rcon, run_id)
    lanes = {r[0] for r in rcon.execute("SELECT lane FROM research_searches").fetchall()}
    assert lanes == {"attempt:county_tax"}           # recorded, but as an ATTEMPT
    r = gates.readiness_for(rcon, "prop-fake-1")
    assert r["gates"]["counter_evidence_searched"] is False
    assert any("attempts recorded but unqualified" in u for u in r["unmet"])


def test_failed_fetch_is_unknown_and_clears_nothing(rcon):
    run_id = _run(rcon)
    rcon.execute("INSERT INTO agent_steps (run_id, step, kind, name, detail, created_at) "
                 "VALUES (?,1,'tool_result','fetch_page',?,?)",
                 [run_id, '{"ok": false, "error": "blocked"}', datetime(2026, 11, 16, 12, 2, 0)])
    _sync(rcon, run_id)
    row = rcon.execute("SELECT lane, notes FROM research_searches").fetchone()
    assert row[0] == "attempt:fetch" and "unknown never clears a gate" in row[1]
    assert gates.readiness_for(rcon, "prop-fake-1") is None or \
        gates.readiness_for(rcon, "prop-fake-1")["gates"]["counter_evidence_searched"] is False


# --- identity: the goal marker is routing, not verified identity ----------------
def test_unbound_fact_stays_unattached(rcon):
    run_id = _run(rcon)
    _evidence(rcon, run_id, "ev-a", "operating_noi", value="-50000",
              quote="NOI was negative fifty thousand")
    _sync(rcon, run_id, page_text=OTHER_PAGE)        # page never mentions FAKE PINES
    row = rcon.execute("SELECT property_key, identity_method, coverage_limit FROM "
                       "research_evidence WHERE evidence_id = 'agent:ev-a'").fetchone()
    assert row[0] is None and row[1] == "unmatched"
    assert "routing context" in row[2]
    r = gates.readiness_for(rcon, "prop-fake-1")
    assert r is None or r["gates"]["identity_verified"] is False   # nothing attached, nothing cleared


def test_bound_fact_attaches_with_universe_identity(rcon):
    run_id = _run(rcon)
    _evidence(rcon, run_id, "ev-b", "occupancy_rate", value="0.81", quote=PAGE[:100])
    _sync(rcon, run_id)                              # page corroborates FAKE PINES
    row = rcon.execute("SELECT property_key, identity_method, identity_key, units, direction "
                       "FROM research_evidence WHERE evidence_id = 'agent:ev-b'").fetchone()
    assert row[0] == "prop-fake-1"                     # attached by corroborated content binding
    assert row[1] == "name_only_unverified"            # ...but a web mention is NOT record-key
    assert row[2] is None                              # identity: it never clears identity_verified
    assert row[3] == "ratio" and row[4] == "neutral"   # qualifiers preserved, direction not judged
    assert gates.readiness_for(rcon, "prop-fake-1")["gates"]["identity_verified"] is False


# --- claim mapping: type and qualifiers preserved, no generic financial label ----
def test_agent_web_figure_never_clears_primary_source_gate(rcon):
    run_id = _run(rcon)
    _evidence(rcon, run_id, "ev-c", "operating_noi", value="-50000", quote=PAGE[:100])
    _sync(rcon, run_id)
    row = rcon.execute("SELECT claim_type, source_name, field_name, units, caveat "
                       "FROM research_evidence WHERE evidence_id = 'agent:ev-c'").fetchone()
    assert row[0] == "financial_fact" and row[1] == "agent_research" and row[2] == "operating_noi"
    assert "not a primary financial source" in row[4]
    assert "period unknown" in row[4]                # unstated period is labeled, never invented
    r = gates.readiness_for(rcon, "prop-fake-1")
    assert r["gates"]["primary_financial_source"] is False   # dated + verified identity, still no


def test_stated_period_is_carried_verbatim(rcon):
    run_id = _run(rcon)
    _evidence(rcon, run_id, "ev-d", "operating_revenue", value="1000000", quote=PAGE[:100],
              notes="period_end=2024-12-31, period_months=12")
    _sync(rcon, run_id)
    row = rcon.execute("SELECT period_end, period_months FROM research_evidence "
                       "WHERE evidence_id = 'agent:ev-d'").fetchone()
    assert str(row[0]) == "2024-12-31" and row[1] == 12.0


# --- identity: corroborated binding, not a bare mention ----------------------------
def test_single_generic_mention_does_not_bind(rcon):
    """A page showing ONLY the property's name (chains share names; streets repeat) is not
    identity: the fact stays unattached even though the name appears verbatim."""
    run_id = _run(rcon)
    _evidence(rcon, run_id, "ev-b2", "occupancy_rate", value="0.81",
              quote="FAKE PINES NURSING HOME announced occupancy gains")
    _sync(rcon, run_id, page_text="FAKE PINES NURSING HOME announced occupancy gains this year")
    row = rcon.execute("SELECT property_key, identity_method FROM research_evidence "
                       "WHERE evidence_id = 'agent:ev-b2'").fetchone()
    assert row == (None, "unmatched")


# --- contradictions: only compatible entity/period/unit/claim can conflict ----------
def _stored_cost_report_noi(rcon):
    ev.insert_evidence(rcon, ev.Evidence(
        "cost-noi", claim_type="financial_fact", source_name="cms_snf_cost_report",
        field_name="operating_noi", property_key="prop-fake-1", identity_method="ccn",
        identity_key="ccn:999001", period_begin=date(2024, 1, 1), period_end=date(2024, 12, 31),
        units="usd_unconfirmed", caveat=ev.COST_REPORT_CAVEAT, value_number=100.0,
        dataset_vintage="fake FY2024"))


def test_contradictory_figure_opens_conflict_and_holds(rcon):
    _stored_cost_report_noi(rcon)
    run_id = _run(rcon)
    _evidence(rcon, run_id, "ev-e", "operating_noi", value="200.0", quote=PAGE[:100],
              notes="period_end=2024-12-31, period_months=12")   # SAME period as the stored row
    _sync(rcon, run_id)
    conf = rcon.execute("SELECT status, detail FROM research_conflicts "
                        "WHERE conflict_id = 'bridge:prop-fake-1:operating_noi'").fetchone()
    assert conf[0] == "open" and "100" in conf[1] and "200" in conf[1]
    assert gates.readiness_for(rcon, "prop-fake-1")["depth"] == "held"


def test_different_period_is_not_a_contradiction(rcon):
    """A 2025 figure and a 2024 figure are two periods, not a disagreement: no conflict,
    no hold. An unstated period can contradict nothing either."""
    _stored_cost_report_noi(rcon)
    run_id = _run(rcon)
    _evidence(rcon, run_id, "ev-e2", "operating_noi", value="200.0", quote=PAGE[:100],
              notes="period_end=2025-12-31, period_months=12")
    _sync(rcon, run_id)
    assert rcon.execute("SELECT count(*) FROM research_conflicts").fetchone()[0] == 0
    assert gates.readiness_for(rcon, "prop-fake-1")["depth"] != "held"


def test_unstated_period_is_not_a_contradiction(rcon):
    _stored_cost_report_noi(rcon)
    run_id = _run(rcon)
    _evidence(rcon, run_id, "ev-e3", "operating_noi", value="200.0", quote=PAGE[:100])
    _sync(rcon, run_id)
    assert rcon.execute("SELECT count(*) FROM research_conflicts").fetchone()[0] == 0


def test_resolved_conflict_stays_resolved_on_unchanged_evidence(rcon):
    """A person's resolution stands across re-syncs of the same run (weekly retries)."""
    _stored_cost_report_noi(rcon)
    run_id = _run(rcon)
    _evidence(rcon, run_id, "ev-e4", "operating_noi", value="200.0", quote=PAGE[:100],
              notes="period_end=2024-12-31, period_months=12")
    _sync(rcon, run_id)
    ev.resolve_conflict(rcon, "bridge:prop-fake-1:operating_noi", "person reconciled: restatement")
    _sync(rcon, run_id)                              # same evidence re-synced
    row = rcon.execute("SELECT status FROM research_conflicts "
                       "WHERE conflict_id = 'bridge:prop-fake-1:operating_noi'").fetchone()
    assert row[0] == "resolved"


# --- the qualified path: record_research_check -----------------------------------
def _qualified_checks(rcon):
    """Both qualified counter-evidence checks, honestly proven."""
    stored = store_file(rcon, PAGE.encode(), source="agent_web", doc_category="web_page",
                        collector_name="test", url="https://county.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="county_tax",
        state="MI", county="FAKE COUNTY", tax_year=2025, parcel="FAKE-12",
        billed=10000.0, paid=10000.0, status="paid",
        file_id=stored.file_id, source_url="https://county.example/fake",
        quote="2025 taxes billed 10,000 paid 10,000 status paid")
    assert out["ok"], out
    liens_page = store_file(rcon, LIENS_PAGE.encode(), source="agent_web",
                            doc_category="web_page", collector_name="test",
                            url="https://sos.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="liens",
        state="MI", query="FAKE PINES NURSING HOME site:sos.example liens", result_count=0,
        file_id=liens_page.file_id, source_url="https://sos.example/fake",
        quote="search: FAKE PINES NURSING HOME, FAKE COUNTY - No records found")
    assert out["ok"], out


def test_qualified_tax_check_clears_its_lane_and_can_complete_the_loop(rcon):
    _qualified_checks(rcon)
    r = gates.readiness_for(rcon, "prop-fake-1")
    assert r["gates"]["counter_evidence_searched"] is True
    assert r["gates"]["primary_financial_source"] is True     # county_tax is a primary source
    assert r["gates"]["identity_verified"] is True
    assert r["depth"] == "ready_for_review"


def test_wrong_jurisdiction_tax_check_is_refused(rcon):
    stored = store_file(rcon, PAGE.encode(), source="agent_web", doc_category="web_page",
                        collector_name="test", url="https://county.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="county_tax",
        state="TX", county="FAKE COUNTY", tax_year=2025, parcel="FAKE-12",  # WRONG state
        billed=10000.0, paid=10000.0, status="paid",
        file_id=stored.file_id, source_url="https://county.example/fake",
        quote="2025 taxes billed 10,000 paid 10,000 status paid")
    assert out["ok"] is False and "MI" in out["error"]
    # recorded as an unqualified attempt, never as qualified coverage
    lanes = {r[0] for r in rcon.execute("SELECT lane FROM research_searches").fetchall()}
    assert lanes == {"attempt:county_tax"}


def test_figures_not_on_the_page_are_refused(rcon):
    stored = store_file(rcon, PAGE.encode(), source="agent_web", doc_category="web_page",
                        collector_name="test", url="https://county.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="county_tax",
        state="MI", county="FAKE COUNTY", tax_year=2025, parcel="FAKE-12",
        billed=10000.0, paid=9999.0, status="paid",                    # page says 10,000 paid
        file_id=stored.file_id, source_url="https://county.example/fake",
        quote="2025 taxes billed 10,000 paid 10,000 status paid")
    assert out["ok"] is False and "does not show paid" in out["error"]
    # digit-soup misbind variant: '1,000' is a substring of the page digits but no token
    out = agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="county_tax",
        state="MI", county="FAKE COUNTY", tax_year=2025, parcel="FAKE-12",
        billed=10000.0, paid=1000.0, status="paid",
        file_id=stored.file_id, source_url="https://county.example/fake",
        quote="2025 taxes billed 10,000 paid 10,000 status paid")
    assert out["ok"] is False


def test_lien_check_needs_explicit_jurisdiction_and_result_proof(rcon):
    liens_page = store_file(rcon, LIENS_PAGE.encode(), source="agent_web",
                            doc_category="web_page", collector_name="test",
                            url="https://sos.example/fake")
    # no state: the property's universe state is routing, not proof of index coverage
    out = agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="liens",
        query="FAKE PINES liens", result_count=0,
        file_id=liens_page.file_id, source_url="https://sos.example/fake",
        quote="No records found")
    assert out["ok"] is False and "explicit state" in out["error"]
    # state but a page with no result statement: a find-nothing needs the index's own words
    news = store_file(rcon, OTHER_PAGE.encode(), source="agent_web",
                      doc_category="web_page", collector_name="test",
                      url="https://news.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="liens",
        state="MI", query="FAKE PINES liens", result_count=0,
        file_id=news.file_id, source_url="https://news.example/fake",
        quote="parcel OTHER-99 - SOMEWHERE ELSE MANOR")
    assert out["ok"] is False
    lanes = {r[0] for r in rcon.execute("SELECT lane FROM research_searches").fetchall()}
    assert "liens" not in lanes                            # the lane never cleared


def test_qualified_check_refuses_a_page_about_a_different_property(rcon):
    stored = store_file(rcon, OTHER_PAGE.encode(), source="agent_web", doc_category="web_page",
                        collector_name="test", url="https://county.example/other")
    out = agent_tools.record_research_check(
        rcon, "run-fake-1", property_id="prop-fake-1", check="county_tax",
        state="MI", county="FAKE COUNTY", tax_year=2025, parcel="FAKE-12",
        billed=1.0, paid=1.0, status="paid",
        file_id=stored.file_id, source_url="https://county.example/other",
        quote="parcel OTHER-99 - SOMEWHERE ELSE MANOR")
    assert out["ok"] is False and "does not corroborate" in out["error"]
    assert rcon.execute("SELECT count(*) FROM research_searches").fetchone()[0] == 0


# --- assessment drafts: through the verified lane, always Unscored ----------------
def _make_ready(rcon):
    _qualified_checks(rcon)
    gates.evaluate_all(rcon)


def _reviewed_assessment(rcon):
    """A person's reviewed assessment already in the LIVE store (must stay untouched)."""
    rcon.execute("INSERT INTO research_assessments (id, property_id, research_score, "
                 "research_tier, confidence, coverage, score_breakdown, score_reasons, one_line, "
                 "artifact, generated_at, ingested_at) VALUES ('rev-1', 'prop-fake-1', 82.0, "
                 "'Hot', 'high', 'reviewed by a person', '{}', '[]', 'reviewed: strong', "
                 "'', '2026-11-15', '2026-11-15')")


def test_ready_property_gets_unscored_draft_not_a_score(rcon, bcon):
    _make_ready(rcon)
    rcon.execute("INSERT INTO projects (project_id, project_name) VALUES "
                 "('prop-fake-1', 'FAKE PINES NURSING HOME')")
    _reviewed_assessment(rcon)
    out = drafts.prepare_drafts(rcon)
    assert out == {"drafts": 1, "skipped_unchanged": 0, "invalid": 0}
    row = rcon.execute("SELECT tier_label, draft_kind, one_line, coverage FROM research_drafts "
                       "WHERE property_key = 'prop-fake-1'").fetchone()
    assert row[0].startswith("Unscored") and row[1] == "ready"
    assert "ready for review" in row[2]
    assert "rubric" in row[3] and "not codified" in row[3]       # the unknown is stated
    # The live assessment store is NEVER touched: the reviewed row stands alone, undemoted.
    live = rcon.execute("SELECT id, research_tier, research_score FROM research_assessments "
                        "WHERE property_id = 'prop-fake-1'").fetchall()
    assert live == [("rev-1", "Hot", 82.0)]
    out = drafts.prepare_drafts(rcon)                            # reruns do not pile up
    assert out == {"drafts": 0, "skipped_unchanged": 1, "invalid": 0}


def test_held_property_gets_held_unscored_draft(rcon, bcon):
    test_contradictory_figure_opens_conflict_and_holds(rcon)
    gates.evaluate_all(rcon)
    rcon.execute("INSERT INTO projects (project_id, project_name) VALUES "
                 "('prop-fake-1', 'FAKE PINES NURSING HOME')")
    out = drafts.prepare_drafts(rcon)
    assert out["drafts"] == 1
    row = rcon.execute("SELECT tier_label, draft_kind, one_line FROM research_drafts "
                       "WHERE property_key = 'prop-fake-1'").fetchone()
    assert "held" in row[0] and row[1] == "held" and "conflict" in row[2]
    assert rcon.execute("SELECT count(*) FROM research_assessments").fetchone()[0] == 0


# --- the chain is off by default; steering needs the paid opt-in -------------------
def test_goal_steering_requires_paid_opt_in(monkeypatch):
    candidate = {"property_id": "prop-fake-1", "property_name": "FAKE PINES",
                 "address": "12 FAKE LANE", "city": "FAKE CITY", "state": "MI"}
    readiness = {"depth": "partial", "unmet": ["counter-evidence: run the checks"]}
    monkeypatch.delenv("BLOOMFIELD_RESEARCH_PAID", raising=False)
    goal = agent_weekly._goal_for(candidate, readiness)
    assert "unmet lanes" not in goal                 # no research-initiated spend without opt-in
    monkeypatch.setenv("BLOOMFIELD_RESEARCH_PAID", "1")
    goal = agent_weekly._goal_for(candidate, readiness)
    assert "unmet lanes" in goal and "record_research_check" in goal


def test_research_hook_off_by_default():
    assert agent_weekly._readiness_fn(None, {"weekly_loop": {}})("prop-fake-1") is None


# --- label-bound figures, status precedence and positive lien statements (round 4) ---
def test_explicit_delinquent_status_wins_over_paid_figures(rcon):
    # The figures look paid-in-full, but the record's own status says past due: an explicit
    # delinquent status ALWAYS wins - the row is distress, never counter-evidence.
    from research import county_tax
    eid = county_tax.record_tax_year(rcon, state="MI", county="FAKE COUNTY", tax_year=2025,
                                     parcel="FAKE-12", property_key="prop-fake-1",
                                     billed=12000.0, paid=12000.0, status="past due")
    row = rcon.execute("SELECT direction FROM research_evidence WHERE evidence_id=?",
                       [eid]).fetchone()
    assert row[0] == "distress"


def test_contradictory_status_and_figures_stay_unknown(rcon):
    # Status says paid but the figures are short: a contradiction is recorded as unknown
    # (neutral + contradiction caveat), never flipped to either direction.
    from research import county_tax
    eid = county_tax.record_tax_year(rcon, state="MI", county="FAKE COUNTY", tax_year=2025,
                                     parcel="FAKE-12", property_key="prop-fake-1",
                                     billed=12000.0, paid=10000.0, status="paid")
    row = rcon.execute("SELECT direction, caveat FROM research_evidence WHERE evidence_id=?",
                       [eid]).fetchone()
    assert row[0] == "neutral" and "contradict" in row[1]


def test_label_bound_figures_qualify(rcon):
    # Each figure next to its OWN label qualifies; the page is the paid-current record.
    stored = store_file(rcon, PAGE.encode(), source="agent_web", doc_category="web_page",
                        collector_name="test", url="https://county.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-1", property_id="prop-fake-1", check="county_tax", state="MI",
        county="FAKE COUNTY", tax_year=2025, parcel="FAKE-12",
        billed=10000.0, paid=10000.0, status="paid",
        file_id=stored.file_id, source_url="https://county.example/fake",
        quote="2025 taxes billed 10,000 paid 10,000 status paid")
    assert out["ok"] is True
    row = rcon.execute("SELECT direction FROM research_evidence").fetchone()
    assert row[0] == "counter"


def test_positive_lien_count_with_index_statement_qualifies(rcon):
    page = ("FAKE STATE UCC and tax lien index - search: FAKE PINES NURSING HOME, "
            "FAKE COUNTY - 5 results")
    stored = store_file(rcon, page.encode(), source="agent_web", doc_category="web_page",
                        collector_name="test", url="https://sos.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-1", property_id="prop-fake-1", check="liens",
        query="FAKE PINES NURSING HOME site:sos.example liens", result_count=5, state="MI",
        file_id=stored.file_id, source_url="https://sos.example/fake",
        quote="search: FAKE PINES NURSING HOME, FAKE COUNTY - 5 results")
    assert out["ok"] is True
    lanes = {r[0] for r in rcon.execute("SELECT lane FROM research_searches").fetchall()}
    assert "liens" in lanes
