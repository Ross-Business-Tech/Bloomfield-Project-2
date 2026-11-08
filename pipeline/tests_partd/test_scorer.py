"""Part D tests: run with `cd pipeline && python -m pytest tests_partd -q`."""
from scorer import demo_data, rank
from scorer.signals import clean_address
from scorer.store import connect


def build():
    con = connect(":memory:")
    con.execute(demo_data.INPUT_SCHEMA)
    s = rank.load_settings()
    demo_data.week1_data(con); rank.run(con, s, demo_data.WEEK1)
    demo_data.week2_data(con); rank.run(con, s, demo_data.WEEK2)
    return con, s


def flags(con):
    return {r["property_name"]: r for r in con.execute(
        "SELECT * FROM flags WHERE run_id='run-2026-11-09'").df().to_dict("records")}


def test_address_cleaning_groups_variants():
    assert clean_address("88 River Street", "MI") == clean_address("88 River St.", "mi")


def test_ann_arbor_example_ranks_first_with_stacked_signals():
    con, _ = build()
    f = flags(con)
    kh = f["Kestrel Hall"]
    assert kh["rank"] == 1
    for word in ("stale_opening", "stuck_construction_loan", "mechanics_lien", "ucc_sale", "bank_stress"):
        assert word in kh["signals_json"]


def test_filters_hide_office_and_oversized_loans():
    con, _ = build()
    f = flags(con)
    assert not f["Midtown Office Tower"]["shown"]
    assert not f["Gateway Retail Plaza"]["shown"]
    assert f["Riverside Flats"]["shown"]  # unknown loan size is kept


def test_certificate_of_occupancy_clears_stale_flag_and_changes_report():
    con, s = build()
    assert "Maple Commons" not in flags(con)
    ch = rank.changes(con, "run-2026-11-09", s)
    assert [r["property_name"] for r in ch["dropped"]] == ["Maple Commons"]
    assert [r["property_name"] for r in ch["new"]] == ["Summit Ridge Apartments"]
    assert {r["property_name"] for r in ch["worse"]} == {"Kestrel Hall", "Prairie Logistics Center"}


def test_scorer_survives_missing_tables():
    con = connect(":memory:")
    rank.run(con, rank.load_settings())  # nothing loaded yet: no crash, no flags
    assert con.execute("SELECT count(*) FROM flags").fetchone()[0] == 0
