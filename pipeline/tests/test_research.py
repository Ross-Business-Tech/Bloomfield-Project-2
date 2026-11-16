"""Tests for the research layer core: evidence shape, identity discipline, gates and holds.
Fake data only; no network; every property and facility is FAKE."""
from datetime import date

import pytest

from research import county_tax, evidence as ev, gates
from research.identity import (IDENTITY_METHODS, VERIFIED_METHODS, key_for_ccn, key_for_name,
                               normalize_ccn)
from research.schema import create_research_tables


@pytest.fixture
def rcon(con):
    create_research_tables(con)
    return con


def both_lanes_searched(con, property_key="prop-fake-1"):
    """Record the county tax check AND the lien search the gates need (fake data)."""
    ev.record_search(con, property_key=property_key, lane="county_tax",
                     query="county tax Fake County MI fake-12 2025", result_count=1)
    ev.record_search(con, property_key=property_key, lane="liens",
                     query="state SOS tax lien search fake entity", result_count=0)


def fin_fact(**kw):
    """A complete financial fact about FAKE PINES (CCN 999001)."""
    base = dict(evidence_id="e1", claim_type="financial_fact", source_name="cms_snf_cost_report",
                field_name="net_income", property_key="prop-fake-1", identity_method="ccn",
                identity_key="ccn:999001", period_begin=date(2024, 1, 1),
                period_end=date(2024, 12, 31), units="usd_unconfirmed",
                caveat=ev.COST_REPORT_CAVEAT, value_number=-100.0,
                source_url="https://data.cms.gov/fake", dataset_vintage="fake FY2024")
    base.update(kw)
    return ev.Evidence(**base)


# --- identity -----------------------------------------------------------------
def test_ccn_normalization():
    assert normalize_ccn("45987") == "045987"
    assert normalize_ccn("145987") == "145987"
    assert normalize_ccn(" 145987 ") == "145987"
    assert normalize_ccn("1234567") is None
    assert normalize_ccn("") is None and normalize_ccn(None) is None


def test_name_only_match_is_not_verified():
    assert "name_only_unverified" in IDENTITY_METHODS
    assert "name_only_unverified" not in VERIFIED_METHODS
    assert key_for_name("FAKE PINES", "FAKE CITY").startswith("name:")


# --- evidence validation --------------------------------------------------------
def test_evidence_requires_provenance():
    e = fin_fact(source_url=None, dataset_vintage=None, file_id=None)
    assert any("provenance" in p for p in e.validate())
    assert fin_fact().validate() == []


def test_financial_fact_requires_units_and_caveat():
    assert any("units" in p for p in fin_fact(units=None).validate())
    assert any("units" in p for p in fin_fact(caveat=None).validate())


def test_period_months_computed_never_annualized():
    e = fin_fact(period_begin=date(2024, 1, 1), period_end=date(2024, 11, 30))
    assert e.period_months == 11.0          # stated 11-month period kept as 11 months


# --- gates ----------------------------------------------------------------------
def test_ready_for_review_when_every_gate_passes(rcon):
    ev.insert_evidence(rcon, fin_fact())
    county_tax.record_tax_year(rcon, state="MI", county="FAKE COUNTY", tax_year=2025,
                               parcel="FAKE-12", property_key="prop-fake-1",
                               billed=1000.0, paid=1000.0, source_url="https://county.example/fake")
    ev.record_search(rcon, property_key="prop-fake-1", lane="liens",
                     query="state SOS tax lien search fake entity", result_count=0)
    gates.evaluate_all(rcon)
    row = rcon.execute("SELECT depth, identity_verified, primary_financial_source, "
                       "counter_evidence_searched, caveats_stored, open_conflicts "
                       "FROM research_status WHERE property_key = 'prop-fake-1'").fetchone()
    assert row == ("ready_for_review", True, True, True, True, 0)


def test_paid_current_taxes_are_counter_evidence(rcon):
    county_tax.record_tax_year(rcon, state="MI", county="FAKE COUNTY", tax_year=2025,
                               parcel="FAKE-12", property_key="prop-fake-1",
                               billed=1000.0, paid=1000.0, source_url="https://county.example/fake")
    direction, lane, result_count = rcon.execute(
        "SELECT e.direction, s.lane, s.result_count FROM research_evidence e, research_searches s "
        "WHERE s.lane = 'county_tax'").fetchone()
    assert direction == "counter" and result_count == 1


def test_open_conflict_holds_property(rcon):
    ev.insert_evidence(rcon, fin_fact())
    county_tax.record_tax_year(rcon, state="MI", county="FAKE COUNTY", tax_year=2025,
                               parcel="FAKE-12", property_key="prop-fake-1",
                               billed=1000.0, paid=1000.0, source_url="https://county.example/fake")
    ev.record_search(rcon, property_key="prop-fake-1", lane="liens",
                     query="state SOS tax lien search fake entity", result_count=0)
    ev.set_conflict(rcon, conflict_id="c1", property_key="prop-fake-1", topic="value",
                    detail="provider info says 2 fines; the penalty rows sum to 3")
    gates.evaluate_all(rcon)
    depth, reason = rcon.execute("SELECT depth, hold_reason FROM research_status "
                                 "WHERE property_key = 'prop-fake-1'").fetchone()
    assert depth == "held" and "value" in reason


def test_no_counter_evidence_search_stays_partial(rcon):
    ev.insert_evidence(rcon, fin_fact())
    r = gates.evaluate_property(rcon, "prop-fake-1")
    assert r["depth"] == "partial"
    assert any("counter-evidence" in u for u in r["unmet"])


def test_financial_fact_without_caveat_blocks_review_ready(rcon):
    ev.insert_evidence(rcon, fin_fact())
    rcon.execute("UPDATE research_evidence SET caveat = NULL")          # a legacy row, pre-discipline
    county_tax.record_tax_year(rcon, state="MI", county="FAKE COUNTY", tax_year=2025,
                               parcel="FAKE-12", property_key="prop-fake-1",
                               billed=1000.0, paid=1000.0, source_url="https://county.example/fake")
    ev.record_search(rcon, property_key="prop-fake-1", lane="liens",
                     query="state SOS tax lien search fake entity", result_count=0)
    r = gates.evaluate_property(rcon, "prop-fake-1")
    assert r["depth"] == "partial" and not r["gates"]["caveats_stored"]


def test_missing_data_is_not_counter_evidence(rcon):
    county_tax.record_tax_year(rcon, state="MI", county="FAKE COUNTY", tax_year=2025,
                               parcel="FAKE-12", property_key="prop-fake-1",
                               source_url="https://county.example/fake")   # nothing found
    direction = rcon.execute("SELECT direction FROM research_evidence").fetchone()[0]
    count = rcon.execute("SELECT result_count FROM research_searches").fetchone()[0]
    assert direction == "neutral" and count == 0        # searched, found nothing, not "healthy"


def test_name_only_identity_never_review_ready(rcon):
    e = fin_fact(identity_method="name_only_unverified", identity_key=key_for_name("FAKE PINES"))
    ev.insert_evidence(rcon, e)
    both_lanes_searched(rcon)
    r = gates.evaluate_property(rcon, "prop-fake-1")
    assert r["depth"] == "partial" and not r["gates"]["identity_verified"]


def test_unmatched_source_rows_group_by_identity_not_property(rcon):
    ev.insert_evidence(rcon, fin_fact(property_key=None))               # a CMS row with no universe mapping
    gates.evaluate_all(rcon)
    rows = rcon.execute("SELECT property_key, depth FROM research_status").fetchall()
    assert rows == [("identity:ccn:999001", "partial")]                 # a source match, not a property


def test_docket_is_allegation_until_read(rcon):
    county_tax.record_docket(rcon, case_number="1:2026cv99999", court="Fake District",
                             title="FAKE FUND v. FAKE OPERATOR", url="https://dockets.example/fake")
    claim_type, coverage = rcon.execute(
        "SELECT claim_type, coverage_limit FROM research_evidence").fetchone()
    assert claim_type == "allegation_unread" and "not been read" in coverage


def test_redemption_stays_neutral_and_grounded(rcon):
    county_tax.record_tax_event(rcon, state="IL", county="FAKE", event_kind="redemption",
                                event_date=date(2024, 3, 1), parcel="FAKE-9",
                                source_url="https://county.example/fake")
    direction, caveat = rcon.execute("SELECT direction, caveat FROM research_evidence").fetchone()
    assert direction == "neutral" and "temporal entity grounding" in caveat


def test_report_is_honest_about_depth(rcon):
    ev.insert_evidence(rcon, fin_fact())
    text = gates.report(rcon)
    assert "partial" in text and "not completed research" in text


# --- gate strictness: the financial record must hang on the verified identity -----------
def test_financial_fact_on_unverified_identity_is_not_a_primary_source(rcon):
    e = fin_fact(identity_method="name_only_unverified", identity_key=key_for_name("FAKE PINES"))
    ev.insert_evidence(rcon, e)
    # a verified identity exists for the property, but NOT on the financial row
    ev.insert_evidence(rcon, fin_fact(evidence_id="e2", claim_type="descriptive_signal",
                                      field_name="overall_rating", value_number=2.0,
                                      units="stars", caveat=ev.STARS_CAVEAT))
    both_lanes_searched(rcon)
    r = gates.evaluate_property(rcon, "prop-fake-1")
    assert r["gates"]["identity_verified"] is True
    assert r["gates"]["primary_financial_source"] is False     # the dated money row is name-only
    assert r["depth"] == "partial"


def test_one_tax_row_alone_does_not_clear_counter_evidence(rcon):
    """A paid-taxrow is one lane; the lien search must also be run and recorded."""
    ev.insert_evidence(rcon, fin_fact())
    county_tax.record_tax_year(rcon, state="MI", county="FAKE COUNTY", tax_year=2025,
                               parcel="FAKE-12", property_key="prop-fake-1",
                               billed=1000.0, paid=1000.0, source_url="https://county.example/fake")
    r = gates.evaluate_property(rcon, "prop-fake-1")
    assert r["gates"]["counter_evidence_searched"] is False
    assert any("liens" in u for u in r["unmet"])
    assert r["depth"] == "partial"


# --- universe property linking ----------------------------------------------------------
def test_identity_keys_resolve_to_one_universe_property(rcon):
    """CMS (ccn) rows and county (parcel) rows for the same building share one property group."""
    from matcher.schema import create_matcher_tables
    create_matcher_tables(rcon)
    rcon.execute("INSERT INTO properties (property_id, property_name, street_address, city, "
                 "state, zip, county, parcel_number, record_count, sources, matched_at) "
                 "VALUES ('prop-fake-uni', 'FAKE PINES', '100 Fake St', 'Fake City', 'MI', "
                 "'48000', 'FAKE COUNTY', 'FAKE-12', 1, 'projects', now())")
    rcon.execute("INSERT INTO licensing_records (record_id, as_of_date, source, file_id, "
                 "facility_name, legal_name, license_number, facility_type, address, city, "
                 "state, zip, status, distress_flag, distress_reason) VALUES "
                 "('lr-1', '2026-01-01', 'cms', 'f1', 'FAKE PINES', 'FAKE PINES LLC', '999001', "
                 "'snf', '100 Fake St', 'Fake City', 'MI', '48000', 'active', false, NULL)")
    ev.insert_evidence(rcon, fin_fact(property_key=None))           # CMS row: keyed ccn:999001 only
    county_tax.record_tax_year(rcon, state="MI", county="FAKE COUNTY", tax_year=2025,
                               parcel="FAKE-12", billed=1000.0, paid=1000.0,
                               source_url="https://county.example/fake")   # keyed parcel only
    gates.evaluate_all(rcon)
    keys = {r[0] for r in rcon.execute("SELECT property_key FROM research_evidence").fetchall()}
    assert keys == {"prop-fake-uni"}                                # both lanes merged into one group
    row = rcon.execute("SELECT depth FROM research_status WHERE property_key = 'prop-fake-uni'").fetchone()
    assert row is not None
    assert not rcon.execute("SELECT 1 FROM research_status WHERE property_key LIKE 'identity:%'").fetchone()


def test_readiness_for_is_a_live_read_only_assessment_input(rcon):
    assert gates.readiness_for(rcon, "prop-fake-1") is None          # nothing tracked yet
    ev.insert_evidence(rcon, fin_fact())
    county_tax.record_tax_year(rcon, state="MI", county="FAKE COUNTY", tax_year=2025,
                               parcel="FAKE-12", property_key="prop-fake-1",
                               billed=1000.0, paid=1000.0, source_url="https://county.example/fake")
    ev.record_search(rcon, property_key="prop-fake-1", lane="liens",
                     query="state SOS tax lien search fake entity", result_count=0)
    r = gates.readiness_for(rcon, "prop-fake-1")
    assert r["depth"] == "ready_for_review" and r["gates"]["no_open_conflicts"]
    # reading readiness writes nothing: it is an input, not an assessment
    assert rcon.execute("SELECT count(*) FROM research_status").fetchone()[0] == 0


# --- linking corroboration: a bare street/parcel string is not identity -------------------
def _universe(rcon, **over):
    from matcher.schema import create_matcher_tables
    create_matcher_tables(rcon)
    row = dict(property_id="prop-fake-uni", property_name="FAKE PINES",
               street_address="100 Fake St", city="Fake City", state="MI", zip="48000",
               county="FAKE COUNTY", parcel_number="FAKE-12")
    row.update(over)
    rcon.execute("INSERT INTO properties (property_id, property_name, street_address, city, "
                 "state, zip, county, parcel_number, record_count, sources, matched_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'projects', now())",
                 [row["property_id"], row["property_name"], row["street_address"], row["city"],
                  row["state"], row["zip"], row["county"], row["parcel_number"]])


def _license(rcon, **over):
    row = dict(record_id="lr-1", license_number="999001", address="100 Fake St",
               city="Fake City", state="MI", zip="48000")
    row.update(over)
    rcon.execute("INSERT INTO licensing_records (record_id, as_of_date, source, file_id, "
                 "facility_name, legal_name, license_number, facility_type, address, city, "
                 "state, zip, status, distress_flag, distress_reason) VALUES "
                 "(?, '2026-01-01', 'cms', 'f1', 'FAKE PINES', 'FAKE PINES LLC', ?, 'snf', "
                 "?, ?, ?, ?, 'active', false, NULL)",
                 [row["record_id"], row["license_number"], row["address"], row["city"],
                  row["state"], row["zip"]])


def test_same_street_different_city_does_not_resolve(rcon):
    """The licensing file says the facility is in Other City; the only universe property
    with that street is in Fake City. A single wrong-city candidate stays unresolved."""
    _universe(rcon)                                              # property in Fake City
    _license(rcon, city="Other City", zip="49001")               # facility in Other City
    ev.insert_evidence(rcon, fin_fact(property_key=None))        # ccn:999001
    gates.evaluate_all(rcon)
    assert rcon.execute("SELECT property_key FROM research_evidence").fetchone()[0] is None
    assert rcon.execute("SELECT property_key FROM research_status").fetchone()[0] == "identity:ccn:999001"


def test_parcel_without_county_agreement_does_not_resolve(rcon):
    _universe(rcon, county="OTHER COUNTY")
    county_tax.record_tax_year(rcon, state="MI", county="FAKE COUNTY", tax_year=2025,
                               parcel="FAKE-12", billed=1000.0, paid=1000.0,
                               source_url="https://county.example/fake")
    gates.evaluate_all(rcon)
    assert rcon.execute("SELECT property_key FROM research_evidence").fetchone()[0] is None


def test_preassigned_conflicting_property_key_is_preserved(rcon):
    _universe(rcon)
    _license(rcon)
    # a row pre-assigned to a DIFFERENT property by whoever recorded it
    ev.insert_evidence(rcon, fin_fact(evidence_id="e-pre", property_key="prop-other",
                                      source_url="https://data.cms.gov/fake2"))
    ev.insert_evidence(rcon, fin_fact(property_key=None))
    gates.evaluate_all(rcon)
    assigned = {r[0]: r[1] for r in rcon.execute(
        "SELECT evidence_id, property_key FROM research_evidence").fetchall()}
    assert assigned["e-pre"] == "prop-other"                     # preserved, never overwritten
    assert assigned["e1"] == "prop-fake-uni"                     # the unassigned row linked
    conflict = rcon.execute("SELECT topic, status FROM research_conflicts "
                            "WHERE conflict_id LIKE 'link:%'").fetchone()
    assert conflict == ("identity", "open")
