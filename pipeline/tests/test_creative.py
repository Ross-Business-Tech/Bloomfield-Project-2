"""Tests for the creative signals (scorer/creative.py): each formula, and how they combine."""
from datetime import date

import pytest

from scorer import creative as cr
from scorer import equation as eq
from scorer.group import Property

AS_OF = date(2022, 8, 1)


@pytest.fixture
def c():
    return eq.load_equation_config()["creative"]


def prop():
    return Property("p", projects=[{"project_id": "x", "property_type": "senior housing", "state": "MI"}])


def ops(*rows):
    return {"x": [dict(zip(("period_end", "period_months", "occupancy", "revenue", "expenses", "noi", "debt_service"), r))
                  for r in rows]}


def test_occupancy_slide_is_measured_against_the_market(c):
    o = ops((date(2019, 12, 31), 12, 0.85, None, None, None, None), (date(2022, 3, 31), 12, 0.72, None, None, None, None))
    alone = cr.occupancy_slide(prop(), "Senior housing", "MI", c["occupancy_slide"], o, {}, AS_OF)
    market = {("US", "Senior housing"): [(date(2019, 12, 31), 0.88), (date(2022, 3, 31), 0.80)]}
    vs_mkt = cr.occupancy_slide(prop(), "Senior housing", "MI", c["occupancy_slide"], o, market, AS_OF)
    assert alone.value == pytest.approx(0.13 / 0.15, abs=0.01) and alone.q == pytest.approx(0.9 * 0.7)
    assert vs_mkt.value == pytest.approx((0.13 - 0.08) / 0.15, abs=0.01) and vs_mkt.q == 0.9


def test_income_erosion_uses_noi_only_no_dscr(c):
    o = ops((date(2019, 12, 31), 12, None, None, None, 4_000_000, 3_000_000),
            (date(2022, 3, 31), 12, None, None, None, 3_000_000, 3_000_000))
    v = cr.income_erosion(prop(), c["income_erosion"], o, AS_OF)
    assert v.value == pytest.approx(0.25 / 0.5) and "DSCR" not in v.why   # debt service is ignored


def test_rising_costs_count_in_income_erosion(c):
    # cost squeeze is part of V now: NOI = revenue - expenses when NOI isn't given
    o = ops((date(2019, 12, 31), 12, None, 20_000_000, 16_000_000, None, None),
            (date(2022, 3, 31), 3, None, 18_000_000 / 4, 17_600_000 / 4, None, None))
    v = cr.income_erosion(prop(), c["income_erosion"], o, AS_OF)
    assert v.value == pytest.approx((4.0 - 0.4) / 4.0 / 0.5 if (4.0 - 0.4) / 4.0 < 0.5 else 1.0)


def test_licensing_is_part_of_compliance_trouble(c):
    p = Property("p", projects=[{"project_id": "x", "property_address": "100 FAKE Sample Ave", "state": "MI"}])
    lic = {"100 fake|MI": [{"facility_name": "SNF", "status": "CMS overall 1-star",
                                  "distress_reason": "abuse icon", "distress_flag": True}]}
    s = cr.compliance_trouble(p, c["compliance_trouble"], AS_OF, None, lic)
    assert s.code == "U" and s.value == 1.0 and s.q == 0.6


def test_noisy_or_combination_and_confirmation_cap(c):
    sigs = [cr.Sig("Q", 0.5, True, "", 0.9), cr.Sig("V", 1.0, True, "", 1.0), cr.Sig("U", 1.0, True, "", 0.6)]
    value, showing, _ = cr.combine(sigs, c)
    assert value == pytest.approx(1 - (1 - 0.45) * 0 * (1 - 0.6))
    assert showing == 3 and c["max_confirmations"] == 2


def multi_loan_prop(*loan_keys):
    return Property("p", projects=[{"project_id": "proj-x", "property_type": "retail", "state": "NY"}],
                    cmbs_loans=[{"loan_key": k} for k in loan_keys])


def test_occupancy_slide_never_compares_across_loans(c):
    # BMO 2023-5C1 Gateway Center South: controlling note 1A-1 reports 0.9793 while pari passu
    # companions 1A-3/1A-5 file a literal 0.0 for the SAME period; each note is trended on its
    # own history, so no cross-note slide is fabricated.
    p = multi_loan_prop("edgar:BMO 2023-5C1 Mortgage Trust:1A-1",
                        "edgar:BMO 2023-5C1 Mortgage Trust:1A-3",
                        "edgar:BMO 2023-5C1 Mortgage Trust:1A-5")
    o = {"edgar:BMO 2023-5C1 Mortgage Trust:1A-1": [{"period_end": date(2022, 6, 30), "occupancy": 0.9793}],
         "edgar:BMO 2023-5C1 Mortgage Trust:1A-3": [{"period_end": date(2022, 6, 30), "occupancy": 0.0}],
         "edgar:BMO 2023-5C1 Mortgage Trust:1A-5": [{"period_end": date(2022, 6, 30), "occupancy": 0.0}]}
    q = cr.occupancy_slide(p, "Retail", "NY", c["occupancy_slide"], o, {}, AS_OF)
    assert not q.known or q.value == 0.0


def test_occupancy_slide_real_trend_survives_companion_zero_notes(c):
    # A genuine slide on the controlling note still fires when companion notes file zeros.
    p = multi_loan_prop("loan-1A-1", "loan-1A-3")
    o = {"loan-1A-1": [{"period_end": date(2021, 12, 31), "occupancy": 0.85},
                       {"period_end": date(2022, 6, 30), "occupancy": 0.75}],
         "loan-1A-3": [{"period_end": date(2021, 12, 31), "occupancy": 0.0},
                       {"period_end": date(2022, 6, 30), "occupancy": 0.0}]}
    q = cr.occupancy_slide(p, "Retail", "NY", c["occupancy_slide"], o, {}, AS_OF)
    assert q.known and q.value == pytest.approx(0.10 / 0.15, abs=0.01)


def test_occupancy_slide_preserves_literal_zero_readings(c):
    # Whether a literal 0.0 is a non-report or true vacancy is unresolved source semantics
    # (BMO 2024-5C8 loan 16 went 0.0 -> 1.0 in one month with payments current; BMO 2022-C1
    # loan 3 went 0.8786 -> 0.0 with DSCR also 0.0). Zeros stay in the series as observed and
    # a same-loan slide onto one scores as filed; flagged for team review, not silently dropped.
    p = multi_loan_prop("loan-1")
    o = {"loan-1": [{"period_end": date(2021, 12, 31), "occupancy": 0.8786},
                    {"period_end": date(2022, 6, 30), "occupancy": 0.0}]}
    q = cr.occupancy_slide(p, "Retail", "NY", c["occupancy_slide"], o, {}, AS_OF)
    assert q.known and q.value == 1.0 and q.q == pytest.approx(0.9 * 0.7)


def test_duplicate_period_keeps_later_file_deterministically(c):
    # Same loan + same period_end filed twice: the row from the later file_id wins,
    # so repeated runs never flip the series.
    p = multi_loan_prop("loan-1")
    o = {"loan-1": [{"period_end": date(2021, 12, 31), "occupancy": 0.90, "file_id": "f1"},
                    {"period_end": date(2021, 12, 31), "occupancy": 0.80, "file_id": "f2"},
                    {"period_end": date(2022, 6, 30), "occupancy": 0.70, "file_id": "f3"}]}
    q = cr.occupancy_slide(p, "Retail", "NY", c["occupancy_slide"], o, {}, AS_OF)
    assert q.known and q.value == pytest.approx(0.10 / 0.15, abs=0.01)


def test_nursing_benchmark_never_serves_assisted_living(c):
    o = ops((date(2022, 1, 1), 12, 0.95, 0, 0, 0, 0), (date(2022, 7, 1), 12, 0.70, 0, 0, 0, 0))
    nursing_mkt = {("MI", "Senior housing (nursing)"): [(date(2022, 1, 1), 0.90, None), (date(2022, 7, 1), 0.88, None)]}
    assisted = Property("p", projects=[{"project_id": "x", "property_type": "Assisted living", "state": "MI"}])
    nursing = Property("p", projects=[{"project_id": "x", "property_type": "Skilled nursing facility", "state": "MI"}])
    a = cr.occupancy_slide(assisted, "Senior housing", "MI", c["occupancy_slide"], o, nursing_mkt, AS_OF)
    n = cr.occupancy_slide(nursing, "Senior housing", "MI", c["occupancy_slide"], o, nursing_mkt, AS_OF)
    assert "no market data" in a.why            # honest miss, not a nursing-facility comparison
    assert "nursing" in n.why and "no market data" not in n.why


# ---------------------------------------------------------------------------
# Nov 2026 rework: general signals that work for any loan and property type, from public records
# ---------------------------------------------------------------------------
def bank_loan_office():
    """An office building on a BANK loan: no CMBS data at all, only public county / city / listing records."""
    return Property("p", filings=[{"file_id": "f1", "property_address": "200 Sample Pkwy", "property_state": "NV",
                                   "property_type": "office"}])


def test_the_six_general_signals():
    assert set(cr.CREATIVE_LABELS) == {"R", "Q", "V", "W", "T", "U"}


def test_unpaid_tax_ramps_from_half_to_full_over_a_year(c):
    t = c["unpaid_tax"]
    rows = {"200 sample|NV": [{"address": "200 Sample Pkwy", "state": "NV", "amount_due": 41_000.0,
                               "due_date": date(2022, 2, 1), "status": "delinquent", "as_of_date": date(2022, 7, 1)}]}
    s = cr.unpaid_tax(bank_loan_office(), t, AS_OF, rows)
    months = (AS_OF - date(2022, 2, 1)).days / 30.4
    assert s.known and s.value == pytest.approx(0.5 + 0.5 * months / 12, abs=0.01) and s.q == 0.9
    year_late = cr.unpaid_tax(bank_loan_office(), t, date(2023, 3, 1), rows)
    assert year_late.value == 1.0


def test_paid_taxes_are_a_known_zero_and_no_record_is_dont_have(c):
    paid = {"200 sample|NV": [{"address": "200 Sample Pkwy", "state": "NV", "amount_due": 0.0, "status": "paid",
                               "due_date": date(2022, 2, 1), "as_of_date": date(2022, 7, 1)}]}
    s = cr.unpaid_tax(bank_loan_office(), c["unpaid_tax"], AS_OF, paid)
    assert s.known and s.value == 0.0
    none = cr.unpaid_tax(bank_loan_office(), c["unpaid_tax"], AS_OF, {})
    assert not none.known and "don't have" in none.why


def test_tax_lien_moves_to_the_late_score(c):
    rows = {"200 sample|NV": [{"address": "200 Sample Pkwy", "state": "NV", "status": "tax certificate sold",
                               "amount_due": 90_000.0, "due_date": date(2021, 2, 1), "as_of_date": date(2022, 6, 1)}]}
    cfg = eq.load_equation_config()
    liens = cr.tax_lien_rows(bank_loan_office(), rows, AS_OF)
    pts, why = eq.late_points(bank_loan_office(), {}, cfg["late"], AS_OF, liens)
    assert pts == cfg["late"]["court_points"] and "tax certificate sold" in why[0]


def test_open_code_violations_and_unsafe_orders(c):
    u = c["compliance_trouble"]
    two = {"200 sample|NV": [{"address": "200 Sample Pkwy", "state": "NV", "record_date": date(2022, 3, 1),
                              "kind": "code", "status": "open", "description": "broken elevator"},
                             {"address": "200 Sample Pkwy", "state": "NV", "record_date": date(2022, 4, 1),
                              "kind": "fire", "status": "OPEN", "description": "sprinkler impairment"},
                             {"address": "200 Sample Pkwy", "state": "NV", "record_date": date(2022, 1, 1),
                              "kind": "code", "status": "closed - complied", "description": "trash"}]}
    s = cr.compliance_trouble(bank_loan_office(), u, AS_OF, two)
    assert s.value == pytest.approx(0.5) and s.q == 0.6
    two["200 sample|NV"].append({"address": "200 Sample Pkwy", "state": "NV", "record_date": date(2022, 6, 1),
                                 "kind": "building", "status": "open", "description": "Unsafe structure - order to vacate"})
    assert cr.compliance_trouble(bank_loan_office(), u, AS_OF, two).value == 1.0


def test_assessor_value_cut_is_measured_against_the_county(c):
    w = c["value_cut"]
    assessed = {"200 sample|NV": [{"address": "200 Sample Pkwy", "state": "NV", "county": "Clark",
                                   "tax_year": y, "value": v} for y, v in ((2019, 10e6), (2020, 10.5e6), (2022, 7e6))]}
    alone = cr.value_cut(bank_loan_office(), w, AS_OF, assessed, {})
    drop = (10.5 - 7) / 10.5
    assert alone.value == pytest.approx(drop / 0.25 if drop < 0.25 else 1.0) and alone.q == pytest.approx(0.6 * 0.7)
    county = {("NV", "clark"): {2020: 100e9, 2022: 90e9}}           # the whole county fell 10%
    vs = cr.value_cut(bank_loan_office(), w, AS_OF, assessed, county)
    assert vs.value == pytest.approx(min(1.0, (drop - 0.10) / 0.25)) and vs.q == 0.6


def test_cmbs_appraisal_beats_the_assessor(c):
    p = bank_loan_office()
    p.cmbs_loans.append({"loan_key": "L1", "value_securitization": 20e6, "value_recent": 15e6,
                         "value_recent_date": date(2022, 1, 1)})
    assessed = {"200 sample|NV": [{"address": "200 Sample Pkwy", "state": "NV", "tax_year": y, "value": v}
                                  for y, v in ((2020, 10e6), (2022, 2e6))]}
    s = cr.value_cut(p, c["value_cut"], AS_OF, assessed, {})
    assert s.value == pytest.approx(0.25 / 0.5) and s.q == 1.0 and "appraisal" in s.why


def test_occupancy_from_listings_when_no_loan_reports_it(c):
    listings = {"200 sample|NV": [{"address": "200 Sample Pkwy", "state": "NV", "observed_at": date(2021, 3, 1),
                                   "available": 8_000, "total": 80_000},
                                  {"address": "200 Sample Pkwy", "state": "NV", "observed_at": date(2022, 6, 1),
                                   "available": 20_000, "total": 80_000}]}
    q = cr.occupancy_slide(bank_loan_office(), "Office", "NV", c["occupancy_slide"], {}, {}, AS_OF, listings)
    assert q.value == pytest.approx(0.15 / 0.15) and q.q == pytest.approx(0.9 * 0.7 * 0.7) and "listed" in q.why


def test_concessions_lower_effective_rent_in_r(con=None):
    import duckdb
    con = duckdb.connect()
    eq.create_equation_tables(con)
    con.execute("INSERT INTO rent_observations (project_id, observed_at, asking_rent, concession_months, "
                "lease_term_months) VALUES ('x', DATE '2022-01-01', 1200, 0, 12), ('x', DATE '2022-06-01', 1200, 2, 12)")
    rents, *_ = eq.load_inputs(con)
    assert [r for _, r in sorted(rents["x"])] == [1200, pytest.approx(1000)]


def test_a_bank_loan_office_with_only_public_records_scores():
    cfg = eq.load_equation_config()
    tax = {"200 sample|NV": [{"address": "200 Sample Pkwy", "state": "NV", "amount_due": 41_000.0,
                              "due_date": date(2021, 8, 1), "status": "delinquent", "as_of_date": date(2022, 7, 1)}]}
    r = eq.score_property(bank_loan_office(), cfg, AS_OF, {}, {}, {}, {"tax": tax})
    by = {s.code: s for s in r.creative}
    assert by["T"].value == 1.0 and r.score > 0
    assert not by["Q"].known and "don't have" in by["Q"].why
