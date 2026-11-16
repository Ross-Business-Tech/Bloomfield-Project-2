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
    quiet = eq.inspection_gap(p, cfg["inspection_gap"], {"BP-1": [date(2026, 5, 14)]}, AS_OF,
                              state="TX", coverage={"TX"})   # 180 days, coverage exists
    busy = eq.inspection_gap(p, cfg["inspection_gap"], {"BP-1": [date(2026, 10, 30)]}, AS_OF,
                             state="TX", coverage={"TX"})
    assert quiet.value == 1.0 and busy.value == 0
    done = Property("b", filings=[permit, {"file_id": "f2", "filing_type": "certificate_of_occupancy"}])
    assert eq.inspection_gap(done, cfg["inspection_gap"], {}, AS_OF).value == 0


# ---- Anti-fluke and late signals ------------------------------------------------------
def test_a_lone_signal_is_no_longer_discounted(cfg):
    """Nov 2026: much data isn't public, so one signal counts in full (no anti-fluke halving)."""
    lone = score(Property("a", recorded_loans=[construction(date(2023, 5, 10))]), cfg)   # L ~0.4: below the boost
    assert lone.showing == 1 and not lone.standouts and lone.corroboration == 1.0
    assert lone.score == pytest.approx(50 * lone.L.value, abs=0.1)


def test_extreme_upside_is_a_standout(cfg):
    """A 5-year-old unpaid construction loan ranks on its own, flagged STANDOUT."""
    lone = score(Property("a", recorded_loans=[construction(date(2021, 11, 1))]), cfg)
    assert lone.standouts and eq.flag_text(lone) == "STANDOUT"
    assert lone.score == pytest.approx(50 * 1.0 * 1.5) and lone.tier == "hot"       # strength boost x1.5 at 100%


def test_extreme_downside_is_a_counter_signal(cfg):
    p = Property("a", recorded_loans=[construction(date(2021, 11, 1))],
                 filings=[{"file_id": "bp", "filing_type": "building_permit", "permit_number": "BP-1",
                           "filing_date": date(2024, 1, 1)}])
    busy = score(p, cfg, insp={"BP-1": [date(2026, 11, 5)]})        # inspected 5 days ago
    assert busy.counters and "COUNTER-SIGNAL" in eq.flag_text(busy)
    assert busy.score == pytest.approx(75.0 * 0.85, abs=0.1)


def test_two_weak_signals_add_up(cfg):
    p = Property("a", recorded_loans=[construction(date(2023, 5, 10))],      # L ~0.4
                 projects=[project(months_overdue=12)])                      # O 0.5
    r = score(p, cfg)
    assert r.showing == 2 and r.corroboration == 1.0
    assert r.score == pytest.approx(50 * r.L.value + 20 * 0.5, abs=0.1)        # weak signals: no boost, no discount


def test_late_signals_add_at_most_ten(cfg):
    filings = [
        {"file_id": "f1", "filing_type": "ucc_sale_notice", "filing_date": date(2026, 11, 1)},
        {"file_id": "f2", "filing_type": "foreclosure_notice", "filing_date": date(2026, 11, 1)},
    ] + [{"file_id": f"l{i}", "filing_type": "mechanics_lien", "filing_date": date(2026, 10, 1),
          "lien_amount": 50_000} for i in range(5)]
    r = score(Property("a", filings=filings), cfg)
    assert r.early == 0 and r.late == 10 and r.score == 10 and r.stage == "ready_to_act"


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


def test_opened_but_construction_loan_still_outstanding_is_boosted(cfg):
    """Bloomfield: a project that opened with its construction loan still outstanding is distress."""
    loans = [construction(date(2022, 8, 1))]                                    # ~51 months
    still_building = eq.loan_clock(Property("a", recorded_loans=loans), cfg["loan_clock"], AS_OF)
    opened = eq.loan_clock(Property("b", recorded_loans=loans,
                                    filings=[{"file_id": "co", "filing_type": "certificate_of_occupancy"}]),
                           cfg["loan_clock"], AS_OF)
    assert opened.value == pytest.approx(min(1.0, still_building.value + 0.25))
    assert "OPENED" in opened.why


def test_filings_after_the_scoring_date_are_ignored(cfg):
    p = Property("a", filings=[{"file_id": "f1", "filing_type": "foreclosure_notice", "filing_date": date(2027, 1, 1)}])
    assert score(p, cfg).late == 0


def test_terms_that_cannot_apply_are_rescaled_at_most_2x(cfg):
    """A long-open building with no construction financing: L and O can't apply, so C is scaled up (max 2x)."""
    filings = [{"file_id": "co", "filing_type": "certificate_of_occupancy", "filing_date": date(2015, 1, 1)}]
    old = Property("a", filings=filings, projects=[project(stale_candidate=False, page_still_unopened=False,
                                                           months_overdue=0)])
    assert eq.applicable_terms(old, cfg, AS_OF) == {"loan_clock": False, "opening_delay": False, "creative": True}
    building = Property("b", recorded_loans=[construction(date(2024, 1, 1))])
    assert all(eq.applicable_terms(building, cfg, AS_OF).values())      # no website yet = unknown, not inapplicable
    assert min(cfg["applicability"]["max_rescale"], 100 / 30) == 2.0


def test_extended_short_term_loan_is_on_the_clock(cfg):
    """Any mortgage that needed an extension counts (bridge / value-add), not only construction loans."""
    plain = Property("a", recorded_loans=[construction(date(2021, 1, 1), "unknown")])
    assert eq.loan_clock(plain, cfg["loan_clock"], AS_OF).value == 0          # never extended: not on the clock
    extended = Property("b", recorded_loans=[construction(date(2021, 1, 1), "unknown"),
                                             construction(date(2024, 1, 1), "extension")])
    r = eq.loan_clock(extended, cfg["loan_clock"], AS_OF)
    assert r.value == 1.0 and "short-term loan" in r.why
    assert eq.applicable_terms(extended, cfg, AS_OF)["loan_clock"] is True


def test_loan_records_after_the_scoring_date_are_ignored(cfg):
    p = Property("a", recorded_loans=[construction(date(2021, 1, 1), "unknown"),
                                      construction(date(2027, 1, 1), "extension")])
    assert eq.loan_clock(p, cfg["loan_clock"], AS_OF).value == 0       # the extension hasn't happened yet


def test_standout_boost_and_rescale_do_not_stack(cfg):
    """A standout gets max(1.2 boost, rescale), never both multiplied."""
    filings = [{"file_id": "co", "filing_type": "certificate_of_occupancy", "filing_date": date(2015, 1, 1)}]
    p = Property("a", recorded_loans=[construction(date(2019, 1, 1), "unknown"),
                                      construction(date(2023, 1, 1), "extension")], filings=filings)
    r = score(p, cfg)
    assert r.rescale == pytest.approx(100 / 80) and r.standouts            # L applies, O doesn't; L is a standout
    assert r.early == pytest.approx(50 * 1.0 * max(1.5, 100 / 80))          # 75: boost 1.5 beats rescale 1.25; never 1.5 x 1.25


def test_loans_read_by_the_ai_reader_feed_the_loan_clock(cfg):
    """Real runs: Part A's recorded_loans collector isn't built, so recorder documents arrive as Reader rows
    (filing_type 'recorded_loan'). They must still start the loan clock (they used to be ignored -> L = 0)."""
    filings = [
        {"file_id": "r1", "filing_type": "recorded_loan", "loan_kind": "construction", "recording_date": date(2021, 3, 1),
         "loan_amount": 24_000_000, "lender_name": "Sample Bank", "borrower_name": "Sample Owner LLC",
         "property_address": "1 Test St", "property_state": "MI"},
        {"file_id": "r2", "filing_type": "recorded_loan", "loan_kind": "modification", "recording_date": date(2024, 3, 1),
         "property_address": "1 Test St", "property_state": "MI"},
    ]
    p = Property("a", filings=filings)                    # recorded_loans table empty, as in a real run
    assert p.recorded_loans == []
    r = eq.loan_clock(p, cfg["loan_clock"], AS_OF)
    assert r.value == 1.0 and "construction loan" in r.why and "1 extension" in r.why
    assert eq.applicable_terms(p, cfg, AS_OF)["loan_clock"] is True


def test_strength_boost_is_graduated(cfg):
    so = cfg["standout"]
    assert eq.strength_boost(0.5, so) == 1.0 and eq.strength_boost(0.6, so) == 1.0
    assert eq.strength_boost(0.8, so) == pytest.approx(1.25) and eq.strength_boost(1.0, so) == pytest.approx(1.5)


def test_one_strong_creative_signal_is_boosted_even_with_low_reliability(cfg):
    """e.g. occupancy 95% -> 33% with no market data: s = 1.0 but reliability 0.63. The strongest signal sets the boost."""
    from scorer import creative as cr
    p = Property("a", projects=[project(stale_candidate=False, page_still_unopened=False, months_overdue=0)])
    ops = {"p1": [{"period_end": date(2025, 1, 1), "period_months": 12, "occupancy": 0.954},
                  {"period_end": date(2026, 6, 30), "period_months": 12, "occupancy": 0.328}]}
    r = eq.score_property(p, cfg, AS_OF, {}, {}, {}, {"ops": ops, "mkt_occ": {}, "concessions": {}, "licenses": {}})
    q = next(s for s in r.creative if s.code == "Q")
    assert q.value == 1.0 and r.C.value == pytest.approx(0.63)
    assert r.boosts["creative"] == pytest.approx(1.5) and "STANDOUT" in eq.flag_text(r)
    assert r.early == pytest.approx(30 * 0.63 * max(1.5, r.rescale), abs=0.1)    # was 30 x 0.63 x 0.5 x 2 = 18.9


def test_inspection_gap_unknown_without_coverage(cfg):
    # Permits exist but the market publishes no readable inspection records: permit age alone
    # cannot establish inactivity, so I must report unknown, not a ramped score.
    permit = {"file_id": "f1", "filing_type": "building_permit", "permit_number": "BP-1", "filing_date": date(2024, 1, 5)}
    p = Property("a", filings=[permit])
    none_anywhere = eq.inspection_gap(p, cfg["inspection_gap"], {}, AS_OF, state="TX", coverage=set())
    wrong_state = eq.inspection_gap(p, cfg["inspection_gap"], {"OTHER-9": [date(2026, 1, 1)]}, AS_OF,
                                    state="TX", coverage={"MI"})
    assert not none_anywhere.known and none_anywhere.value == 0
    assert not wrong_state.known
    # Real inspection dates on THIS permit are evidence even without a coverage flag:
    dated = eq.inspection_gap(p, cfg["inspection_gap"], {"BP-1": [date(2026, 5, 14)]}, AS_OF, state="TX")
    assert dated.known and dated.value == 1.0
