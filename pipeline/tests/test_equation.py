"""Tests for the Bloomfield Opportunity Score (scorer/equation.py). Fake data only."""
from datetime import date

import pytest

from scorer import equation as eq
from scorer.group import Property

AS_OF = date(2026, 11, 10)


@pytest.fixture
def cfg():
    return eq.load_equation_config()


def construction(rec_date, kind="construction", amount=38e6):
    return {"record_id": f"r-{rec_date}-{kind}", "loan_kind": kind, "recording_date": rec_date,
            "amount": amount, "grantee": "Sample Bank", "grantor": "Sample Owner LLC",
            "property_address": "1 Test St", "state": "MI"}


def project(**kw):
    base = {"project_id": "p1", "project_name": "Test Hall", "city": "Ann Arbor", "state": "MI",
            "stale_candidate": True, "page_still_unopened": True}
    return {**base, **kw}


def score(p, cfg, rents=None, market=None, insp=None):
    return eq.score_property(p, cfg, AS_OF, rents or {}, market or {}, insp or {})


# ---- L: loan clock ------------------------------------------------------------
def test_loan_clock_is_zero_before_30_months_and_full_at_60(cfg):
    c = cfg["loan_clock"]
    young = Property("a", recorded_loans=[construction(date(2025, 1, 10))])          # ~22 months
    mid = Property("b", recorded_loans=[construction(date(2023, 5, 10))])            # 42 months
    old = Property("c", recorded_loans=[construction(date(2021, 11, 1))])            # just over 60 months
    assert eq.loan_clock(young, c, AS_OF).value == 0
    assert eq.loan_clock(mid, c, AS_OF).value == pytest.approx(0.4, abs=0.02)
    assert eq.loan_clock(old, c, AS_OF).value == 1.0


def test_paid_off_construction_loan_scores_zero(cfg):
    p = Property("a", recorded_loans=[construction(date(2020, 1, 1)), construction(date(2022, 6, 1), "permanent")])
    assert eq.loan_clock(p, cfg["loan_clock"], AS_OF).value == 0


def test_extensions_add_on_top(cfg):
    base = [construction(date(2023, 5, 10))]
    ext = base + [construction(date(2025, 5, 1), "extension"), construction(date(2026, 3, 1), "modification")]
    a = eq.loan_clock(Property("a", recorded_loans=base), cfg["loan_clock"], AS_OF).value
    b = eq.loan_clock(Property("b", recorded_loans=ext), cfg["loan_clock"], AS_OF).value
    assert b == pytest.approx(a + 0.2)


# ---- O: opening delay -----------------------------------------------------------
def test_delays_under_six_months_are_normal(cfg):
    p = Property("a", projects=[project(months_overdue=5)])
    assert eq.opening_delay(p, cfg["opening_delay"], AS_OF).value == 0


def test_opening_delay_grows_from_6_to_18_months(cfg):
    p12 = Property("a", projects=[project(months_overdue=12)])
    p18 = Property("b", projects=[project(months_overdue=18)])
    assert eq.opening_delay(p12, cfg["opening_delay"], AS_OF).value == pytest.approx(0.5)
    assert eq.opening_delay(p18, cfg["opening_delay"], AS_OF).value == 1.0


# ---- R: relative rent decline ----------------------------------------------------
def test_rent_decline_is_measured_against_the_city(cfg):
    p = Property("a", projects=[project()])
    rents = {"p1": [(date(2026, 1, 15), 1250.0), (date(2026, 3, 1), 1240.0), (date(2026, 9, 1), 1100.0),
                    (date(2026, 10, 20), 1095.0)]}
    flat_city = {("ann arbor", "MI"): [(date(2026, 1, 1), 100.0), (date(2026, 10, 1), 100.0)]}
    falling_city = {("ann arbor", "MI"): [(date(2026, 1, 1), 100.0), (date(2026, 10, 1), 88.0)]}
    facts = {"city": "Ann Arbor", "state": "MI"}
    worse = eq.rent_decline(p, facts, cfg["rent_decline"], rents, flat_city, AS_OF)
    same = eq.rent_decline(p, facts, cfg["rent_decline"], rents, falling_city, AS_OF)
    assert worse.value > 0.75         # ~12 points worse than a flat city
    assert same.value == 0            # the whole city fell just as much: not this property's problem


def test_one_rent_reading_is_not_enough(cfg):
    p = Property("a", projects=[project()])
    r = eq.rent_decline(p, {"city": "Ann Arbor", "state": "MI"}, cfg["rent_decline"],
                        {"p1": [(date(2026, 10, 1), 900.0)]}, {}, AS_OF)
    assert not r.known and r.value == 0


# ---- I: inspection gap -------------------------------------------------------------
def test_inspection_gap(cfg):
    permit = {"file_id": "f1", "filing_type": "building_permit", "permit_number": "BP-1", "filing_date": date(2024, 1, 5)}
    p = Property("a", filings=[permit])
    quiet = eq.inspection_gap(p, cfg["inspection_gap"], {"BP-1": [date(2026, 5, 14)]}, AS_OF)   # 180 days
    busy = eq.inspection_gap(p, cfg["inspection_gap"], {"BP-1": [date(2026, 10, 30)]}, AS_OF)
    assert quiet.value == 1.0 and busy.value == 0
    done = Property("b", filings=[permit, {"file_id": "f2", "filing_type": "certificate_of_occupancy"}])
    assert eq.inspection_gap(done, cfg["inspection_gap"], {}, AS_OF).value == 0


# ---- Anti-fluke and late signals ------------------------------------------------------
def test_a_lone_signal_keeps_only_half(cfg):
    lone = score(Property("a", recorded_loans=[construction(date(2021, 11, 1))]), cfg)
    assert lone.showing == 1 and lone.score == pytest.approx(25.0)


def test_signals_that_agree_get_full_credit(cfg):
    p = Property("a", recorded_loans=[construction(date(2021, 11, 10))],
                 projects=[project(months_overdue=18)])
    r = score(p, cfg)
    assert r.showing == 2 and r.score == pytest.approx((50 + 20) * 0.8)


def test_late_signals_add_at_most_ten(cfg):
    filings = [
        {"file_id": "f1", "filing_type": "ucc_sale_notice", "filing_date": date(2026, 11, 1)},
        {"file_id": "f2", "filing_type": "foreclosure_notice", "filing_date": date(2026, 11, 1)},
    ] + [{"file_id": f"l{i}", "filing_type": "mechanics_lien", "filing_date": date(2026, 10, 1),
          "lien_amount": 50_000} for i in range(5)]
    r = score(Property("a", filings=filings), cfg)
    assert r.early == 0 and r.late == 10 and r.score == 10 and r.stage == "actionable"


def test_ann_arbor_example_is_hot(cfg):
    """Bloomfield's example: late opening + loan on the clock + rent sliding + quiet site + a lien."""
    p = Property(
        "kestrel",
        recorded_loans=[construction(date(2022, 3, 14)), construction(date(2025, 2, 20), "extension")],
        projects=[project(months_overdue=8, missed_lease_up=True, date_change_count=2)],
        filings=[{"file_id": "bp", "filing_type": "building_permit", "permit_number": "BP-9",
                  "filing_date": date(2023, 4, 1)},
                 {"file_id": "ln", "filing_type": "mechanics_lien", "filing_date": date(2026, 10, 2),
                  "lien_amount": 184_500}],
    )
    rents = {"p1": [(date(2026, 1, 15), 1250.0), (date(2026, 10, 20), 1095.0)]}
    market = {("ann arbor", "MI"): [(date(2026, 1, 1), 100.0), (date(2026, 10, 1), 101.0)]}
    r = score(p, cfg, rents, market, {"BP-9": [date(2026, 6, 1)]})
    assert r.showing == 4 and r.corroboration == 1.0
    assert r.tier == "hot" and r.score > 70
