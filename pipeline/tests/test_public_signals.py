"""Public-data inputs (Nov 2026): SEC Schedule AL operating/valuation fields, and public building permits,
feed the occupancy slide, income erosion, cost squeeze, value cut, loan clock and opening delay.
Made-up data only."""
from datetime import date

import pytest

from collectors import edgar
from collectors.common.log import RunTracker
from collectors.common.parse import load_yaml
from scorer import equation as eq
from scorer.group import group_properties

CFG = load_yaml(edgar.CONFIG_PATH)
AS_OF = date(2026, 11, 10)

XML = """<?xml version="1.0" encoding="UTF-8"?>
<assetData xmlns="http://www.sec.gov/edgar/document/absee/cmbs/assetdata">
  <assets>
    <assetNumber>9</assetNumber>
    <reportingPeriodEndDate>10-16-2026</reportingPeriodEndDate>
    <originationDate>12-01-2023</originationDate>
    <originalLoanAmount>15000000</originalLoanAmount>
    <maturityDate>01-01-2029</maturityDate>
    <reportPeriodEndActualBalanceAmount>15000000</reportPeriodEndActualBalanceAmount>
    <modifiedIndicator>N</modifiedIndicator>
    <primaryServicerName>FAKE Servicing</primaryServicerName>
    <property>
      <propertyName>FAKE Lakeview Apartments</propertyName>
      <propertyAddress>99 FAKE Lake Rd</propertyAddress><propertyCity>Fakeville</propertyCity><propertyState>OH</propertyState>
      <propertyTypeCode>MF</propertyTypeCode>
      <physicalOccupancySecuritizationPercentage>0.954</physicalOccupancySecuritizationPercentage>
      <mostRecentPhysicalOccupancyPercentage>0.328</mostRecentPhysicalOccupancyPercentage>
      <revenueSecuritizationAmount>9000000</revenueSecuritizationAmount>
      <operatingExpensesSecuritizationAmount>4000000</operatingExpensesSecuritizationAmount>
      <netOperatingIncomeSecuritizationAmount>5000000</netOperatingIncomeSecuritizationAmount>
      <mostRecentFinancialsStartDate>01-01-2026</mostRecentFinancialsStartDate>
      <mostRecentFinancialsEndDate>06-30-2026</mostRecentFinancialsEndDate>
      <mostRecentRevenueAmount>1500000</mostRecentRevenueAmount>
      <operatingExpensesAmount>3450000</operatingExpensesAmount>
      <mostRecentNetOperatingIncomeAmount>-1950000</mostRecentNetOperatingIncomeAmount>
      <valuationSecuritizationAmount>77000000</valuationSecuritizationAmount>
      <mostRecentValuationAmount>18100000</mostRecentValuationAmount>
      <mostRecentValuationDate>04-01-2026</mostRecentValuationDate>
    </property>
  </assets>
</assetData>
"""


def load(con, tmp_path, xml=XML):
    from scorer.schema import open_db  # noqa: F401  (tables come from the con fixture)
    path = tmp_path / "ex102.xml"
    path.write_text(xml)
    with RunTracker(con, "test") as run:
        edgar.load_file(con, run, "fake-ex102", path, "FAKE 2024-C1", CFG)


def test_schedule_al_fields_feed_q_v_x_and_w(con, tmp_path):
    from scorer.schema import create_scorer_tables
    create_scorer_tables(con)
    load(con, tmp_path)
    rows = con.execute("SELECT period_end, period_months, occupancy, revenue, expenses, noi, source FROM operating_metrics "
                       "ORDER BY period_end").fetchall()
    assert rows[0][0] == date(2023, 12, 1) and rows[0][3:6] == (9e6, 4e6, 5e6)        # securitization baseline
    assert rows[1][0] == date(2026, 6, 30) and rows[1][1] == 6 and rows[1][5] == -1.95e6  # most recent (6 months)
    cfg = eq.load_equation_config()
    r = eq.score_all(con, cfg, AS_OF)[0]
    by = {s.code: s for s in r.creative}
    assert by["Q"].value == 1.0                     # 95.4% -> 32.8%
    assert by["V"].value == 1.0                     # NOI $5.0M -> -$3.9M a year
    assert by["X"].value == 1.0                     # expenses up, revenue down
    assert by["W"].value == 1.0 and "77.0M" in by["W"].why     # value $77.0M -> $18.1M
    assert r.tier in ("hot", "watch") and r.score >= 50


def test_past_maturity_and_modified_public_loans_start_the_loan_clock(con, tmp_path):
    from scorer.schema import create_scorer_tables
    create_scorer_tables(con)
    past = XML.replace("<maturityDate>01-01-2029</maturityDate>", "<maturityDate>01-01-2026</maturityDate>")
    load(con, tmp_path, past)
    cfg = eq.load_equation_config()
    p = group_properties(con)[0]
    lc = eq.loan_clock(p, cfg["loan_clock"], AS_OF)
    assert lc.value == 1.0 and "past its maturity" in lc.why

    con.execute("DELETE FROM loans")
    modified = XML.replace("<originationDate>12-01-2023</originationDate>", "<originationDate>12-01-2020</originationDate>") \
                  .replace("<modifiedIndicator>N</modifiedIndicator>", "<modifiedIndicator>Y</modifiedIndicator>")
    load(con, tmp_path, modified)
    p = group_properties(con)[0]
    lc = eq.loan_clock(p, cfg["loan_clock"], AS_OF)
    assert lc.value > 0.9 and "short-term loan (extended)" in lc.why


def test_new_construction_permit_with_no_co_is_a_public_opening_delay(cfg=None):
    from scorer.group import Property
    cfg = eq.load_equation_config()
    permit = {"file_id": "bp", "filing_type": "building_permit", "permit_number": "BP-1",
              "permit_status": "Issued · Commercial · New Building", "filing_date": date(2023, 5, 10)}
    late = eq.opening_delay(Property("a", filings=[permit]), cfg["opening_delay"], AS_OF)
    assert late.known and late.value == pytest.approx(1.0) and "no certificate of occupancy" in late.why  # 42 - 24 = 18
    reno = {**permit, "permit_status": "Issued · Commercial · Interior Renovation"}
    assert eq.opening_delay(Property("b", filings=[reno]), cfg["opening_delay"], AS_OF).value == 0
    done = Property("c", filings=[permit, {"file_id": "co", "filing_type": "certificate_of_occupancy",
                                           "filing_date": date(2025, 1, 1)}])
    assert eq.opening_delay(done, cfg["opening_delay"], AS_OF).value == 0
