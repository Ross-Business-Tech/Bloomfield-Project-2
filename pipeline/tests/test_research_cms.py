"""Tests for the CMS pool and cost-report lanes. Fake CMS files; no network; all FAKE."""
import csv
import io

import pytest

from collectors.common.files import store_file
from research import cms_pool, cost_reports, gates
from research.schema import create_research_tables


@pytest.fixture
def rcon(con):
    create_research_tables(con)
    return con


def make_csv(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


PROVIDER_HEADER = ["CMS Certification Number (CCN)", "Provider Name", "Number of Fines",
                   "Total Amount of Fines", "Number of Payment Denials", "Overall Rating",
                   "Number of Certified Beds", "Average Number of Residents per Day", "Processing Date"]
PROVIDER = make_csv(PROVIDER_HEADER, [
    ["999001", "FAKE PINES", "2", "50000", "1", "2", "100", "80", "10/01/2026"],
    ["999002", "FAKE OAKS", "0", "0", "0", "5", "50", "49", "10/01/2026"],
])

PENALTY_HEADER = ["CMS Certification Number (CCN)", "Provider Name", "Penalty Date", "Penalty Type",
                  "Fine Amount", "Payment Denial Start Date", "Payment Denial Length in Days",
                  "Processing Date"]
PENALTIES = make_csv(PENALTY_HEADER, [
    ["999001", "FAKE PINES", "01/15/2025", "Fine", "30000", "", "", "10/01/2026"],
    ["999001", "FAKE PINES", "06/01/2025", "Fine", "20000", "", "", "10/01/2026"],
    ["999001", "FAKE PINES", "06/01/2025", "Denial", "", "06/15/2025", "15", "10/01/2026"],
    ["999002", "FAKE OAKS", "03/01/2025", "Fine", "5000", "", "", "10/01/2026"],   # provider says 0: mismatch
])

OWNERSHIP_HEADER = ["CMS Certification Number (CCN)", "Provider Name",
                    "Role played by Owner or Manager in Facility", "Owner Name",
                    "Ownership Percentage", "Association Date", "Processing Date"]
OWNERSHIP = make_csv(OWNERSHIP_HEADER, [
    ["999001", "FAKE PINES", "5% OR GREATER DIRECT OWNERSHIP INTEREST", "FAKE HOLDCO LLC", "100%",
     "09/01/2020", "10/01/2026"],
])

CR_HEADER = ["rpt_rec_num", "Provider CCN", "Facility Name", "Fiscal Year Begin Date",
             "Fiscal Year End Date", "Net Income", "Net income from service to patients",
             "Total fund balances", "Total Current Assets", "Total current liabilities",
             "Total Liabilities", "Net Patient Revenue", "Less Total Operating Expense"]
COST_REPORT_23 = make_csv(CR_HEADER, [
    ["1001", "999001", "FAKE PINES", "01/01/2023", "12/31/2023", "50000", "45000", "100000",
     "200000", "150000", "600000", "900000", "850000"],
])
COST_REPORT_24 = make_csv(CR_HEADER, [
    ["1002", "999001", "FAKE PINES CARE CENTER", "01/01/2024", "11/30/2024", "-30000", "-25000",
     "80000", "90000", "-5000", "590000", "800000", "830000"],
    ["1003", "999001", "FAKE PINES CARE CENTER", "01/01/2024", "11/30/2024", "-30000", "-25000",
     "80000", "90000", "-5000", "590000", "800000", "830000"],   # same facility twice in one file
])


def store(con, body, source_record_id):
    return store_file(con, body.encode(), source="cms", doc_category="other",
                      collector_name="test", ext="csv", source_record_id=source_record_id,
                      notes="test")


def test_pool_reconciliation_match_and_mismatch(rcon):
    store(rcon, PROVIDER, cms_pool.PROVIDER_INFO)
    store(rcon, PENALTIES, cms_pool.PENALTIES)
    cms_pool.collect(rcon)
    # 999001 matches exactly: no open conflict. 999002 disagrees: open conflict.
    rows = dict(rcon.execute("SELECT conflict_id, status FROM research_conflicts").fetchall())
    assert rows.get("cms-pool:999001:provider_vs_penalties") in (None, "resolved")
    assert rows["cms-pool:999002:provider_vs_penalties"] == "open"
    # and the open conflict holds the facility out of full-depth research
    gates.evaluate_all(rcon)
    depth = rcon.execute("SELECT depth FROM research_status "
                         "WHERE property_key = 'identity:ccn:999002'").fetchone()[0]
    assert depth == "held"


def test_fines_never_unpaid_debt_and_denials_never_revenue(rcon):
    store(rcon, PROVIDER, cms_pool.PROVIDER_INFO)
    store(rcon, PENALTIES, cms_pool.PENALTIES)
    cms_pool.collect(rcon)
    caveats = " ".join(r[0] for r in rcon.execute("SELECT caveat FROM research_evidence").fetchall())
    assert "never described as unpaid debt" in caveats
    assert "not lost revenue" in caveats
    assert "not staffed occupancy" in caveats


def test_ownership_is_a_record_entry_only(rcon):
    store(rcon, OWNERSHIP, cms_pool.OWNERSHIP)
    cms_pool.collect(rcon)
    claim_type, caveat, value = rcon.execute(
        "SELECT claim_type, caveat, value_text FROM research_evidence").fetchone()
    assert claim_type == "entity_link"
    assert "not proof of a transfer" in caveat and "not common ownership" in caveat
    assert "FAKE HOLDCO LLC" in value and "since 09/01/2020" in value


def test_cost_report_partial_year_never_annualized(rcon):
    store(rcon, COST_REPORT_24, "cms_snf_cost_report_fy2024")
    cost_reports.collect(rcon)
    months, caveat = rcon.execute(
        "SELECT period_months, caveat FROM research_evidence WHERE field_name = 'net_income'").fetchone()
    assert months == 11.0 and "never annualized" in caveat


def test_cost_report_negative_component_preserved_without_inference(rcon):
    store(rcon, COST_REPORT_24, "cms_snf_cost_report_fy2024")
    cost_reports.collect(rcon)
    value, caveat = rcon.execute(
        "SELECT value_number, caveat FROM research_evidence WHERE field_name = 'current_liabilities'"
    ).fetchone()
    assert value == -5000.0 and "no inference of insolvency" in caveat


def test_one_row_per_ccn_is_a_recorded_check_not_proof(rcon):
    store(rcon, COST_REPORT_24, "cms_snf_cost_report_fy2024")
    cost_reports.collect(rcon)
    caveat, raw_id = rcon.execute(
        "SELECT caveat, raw_record_id FROM research_evidence WHERE field_name = 'net_income'").fetchone()
    assert "not proof" in caveat and "2 rows listed" in caveat and raw_id == "1003"


def test_worksheet_lines_and_units_carried(rcon):
    store(rcon, COST_REPORT_23, "cms_snf_cost_report_fy2023")
    cost_reports.collect(rcon)
    line, units, conf = rcon.execute(
        "SELECT worksheet_ref, units, unit_confidence FROM research_evidence "
        "WHERE field_name = 'net_income'").fetchone()
    assert line == "G3 L31" and units == "usd_unconfirmed" and conf == "unconfirmed"


def test_reruns_replace_instead_of_duplicating(rcon):
    store(rcon, PROVIDER, cms_pool.PROVIDER_INFO)
    store(rcon, COST_REPORT_23, "cms_snf_cost_report_fy2023")
    cms_pool.collect(rcon)
    cost_reports.collect(rcon)
    before = rcon.execute("SELECT count(*) FROM research_evidence").fetchone()[0]
    cms_pool.collect(rcon)
    cost_reports.collect(rcon)
    after = rcon.execute("SELECT count(*) FROM research_evidence").fetchone()[0]
    assert before == after and before > 0


def test_missing_files_fall_back_to_manual_inbox(rcon):
    run = cost_reports.collect(rcon)
    assert any("inbox" in why or "drop" in why for _src, why in run.fallbacks)
