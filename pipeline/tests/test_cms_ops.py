"""Tests for the CMS occupancy bridge. Fake CMS files; no network; every facility is FAKE."""
import csv
import io

from collectors import cms_ops
from collectors.common import config
from scorer.equation import create_equation_tables
from scout_reader_schema import create_scout_reader_tables

HEADER = ["CMS Certification Number (CCN)", "Provider Name", "Provider Address", "City/Town", "State",
          "ZIP Code", "Number of Certified Beds", "Average Number of Residents per Day",
          "Special Focus Status", "Processing Date"]


def make_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    w.writerows(rows)
    return buf.getvalue()


FAKE = make_csv([
    ["990001", "FAKE PINES NURSING", "100 FAKE ST", "Fake City", "MI", "48104", "100", "82", "", "2026-11-01"],
    ["990002", "FAKE OAKS CARE", "200 FAKE AVE", "Fake Town", "MI", "48105", "50", "40", "", "2026-11-01"],
    ["990003", "FAKE EMPTY HALL", "300 FAKE RD", "Fake City", "MI", "48104", "60", "0", "", "2026-11-01"],
    ["990004", "FAKE NO DATA PLACE", "400 FAKE BLVD", "Fakeville", "TX", "78701", "", "30", "", "2026-11-01"],
])


def store_fake(con, tmp_path, body=FAKE):
    from collectors.common.files import store_file
    store_file(con, body.encode(), source="cms", doc_category="other", collector_name="test",
               ext="csv", source_record_id=cms_ops.PROVIDER_INFO, notes="test")
    create_equation_tables(con)
    create_scout_reader_tables(con)


def test_occupancy_and_state_averages_land(con, env, tmp_path):
    store_fake(con, tmp_path)
    cms_ops.collect(con)
    ops = con.execute("SELECT link_id, period_end, occupancy, source, disclosed_on FROM operating_metrics "
                      "ORDER BY occupancy DESC").fetchall()
    # the facility with no beds number is skipped, not guessed
    assert len(ops) == 3 and all(r[3] == "cms_nursing" for r in ops)
    assert str(ops[0][1]) == "2026-11-01" and str(ops[0][4]) == "2026-11-01"
    assert round(ops[0][2], 3) == 0.82 and round(ops[1][2], 3) == 0.8 and ops[2][2] == 0.0
    mkt = con.execute("SELECT state, property_type, period, occupancy, source FROM market_occupancy").fetchall()
    assert len(mkt) == 1 and mkt[0][0] == "MI" and mkt[0][1] == "Nursing facility"
    assert round(mkt[0][3], 4) == round((0.82 + 0.8 + 0.0) / 3, 4)
    fil = con.execute("SELECT filing_type, project_name, property_city, property_state, filing_date "
                      "FROM filing_fields ORDER BY project_name").fetchall()
    assert len(fil) == 3 and all(f[0] == "cms_provider_info" for f in fil)


def test_rerun_replaces_instead_of_duplicating(con, env, tmp_path):
    store_fake(con, tmp_path)
    cms_ops.collect(con)
    cms_ops.collect(con)
    assert con.execute("SELECT count(*) FROM operating_metrics").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM market_occupancy").fetchone()[0] == 1


def test_missing_beds_column_is_an_error_not_a_guess(con, env, tmp_path):
    bad = make_csv([]).replace("Number of Certified Beds", "Some Other Column")
    store_fake(con, tmp_path, bad)
    run = cms_ops.collect(con)
    assert con.execute("SELECT count(*) FROM operating_metrics").fetchone()[0] == 0
    assert run.errors


def test_market_rows_labeled_nursing_facility(con, env, tmp_path):
    """Market averages are keyed by their true subtype so they can't leak to other senior housing."""
    cms_ops.collect(con)
    pts = {r[0] for r in con.execute("SELECT DISTINCT property_type FROM market_occupancy").fetchall()}
    assert pts == {"Nursing facility"}
