"""Tests for Part D (the Scorer). No internet, no AI: fake data only."""
import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from collectors.common import db
from scorer import group, signals
from scorer.__main__ import main as scorer_main
from scorer.schema import TABLES, create_scorer_tables, open_db
from scorer.score import load_scoring_config, score_all, write_scores
from scout_reader_schema import create_scout_reader_tables, upsert

AS_OF = date(2026, 11, 1)
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def scon(con):
    """In-memory database with Part A, B and D tables."""
    create_scout_reader_tables(con)
    create_scorer_tables(con)
    return con


@pytest.fixture
def cfg():
    return load_scoring_config()


def loan(con, rid, kind, rec_date, *, parcel="P-1", address="10 Test St, Town, MI", amount=10e6,
         lender="Test Bank", related=None, inst=None, state="MI"):
    db.insert_row(con, "recorded_loans", {
        "record_id": rid, "state": state, "county": "Test County", "instrument_number": inst or rid,
        "loan_kind": kind, "recording_date": rec_date, "grantor": "Borrower LLC", "grantee": lender,
        "amount": amount, "parcel_number": parcel, "property_address": address,
        "related_instrument": related})


def filing(con, fid, ftype, **kw):
    row = {"file_id": fid, "filing_type": ftype, "status": "ok", "property_state": "MI",
           "property_address": "10 Test St, Town, MI"}
    for k in ("distress_tags", "quotes"):
        if k in kw:
            kw[k] = json.dumps(kw[k])
    row.update(kw)
    upsert(con, "filing_fields", row)


def scores(con, cfg):
    return {sp.facts["property_address"]: sp for sp in score_all(con, cfg, AS_OF)}


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def test_street_key_normalizes():
    assert group.street_key("1000 Example Campus Way, Ann Arbor, MI") == "1000 example campus way"
    assert group.street_key("12 North Main Street") == group.street_key("12 N. Main St, X")
    assert group.street_key("Ann Arbor, MI") is None
    assert group.street_key(None) is None


def test_records_join_by_parcel_or_address(scon):
    loan(scon, "a", "construction", date(2022, 1, 1), parcel="P-1", address="10 Test St")
    # permit has no parcel but the same street -> same property
    filing(scon, "f1", "building_permit", parcel_number=None, property_address="10 Test Street, Town, MI")
    # same parcel number, different address spelling -> same property
    loan(scon, "b", "modification", date(2023, 1, 1), parcel="p1", address=None)
    # same street in another state -> different property
    loan(scon, "c", "permanent", date(2023, 1, 1), parcel=None, address="10 Test St", state="OH")
    props = group.group_properties(scon)
    assert len(props) == 2
    big = max(props, key=lambda p: len(p.all_rows()))
    assert len(big.recorded_loans) == 2 and len(big.filings) == 1


def test_part_c_links_win_when_present(scon):
    loan(scon, "a", "construction", date(2022, 1, 1), parcel="P-1")
    loan(scon, "b", "construction", date(2022, 1, 1), parcel="P-2", address="99 Other Rd")
    scon.execute("CREATE TABLE property_links (property_id TEXT, record_table TEXT, record_id TEXT)")
    scon.execute("INSERT INTO property_links VALUES ('C-1','recorded_loans','a'), ('C-1','recorded_loans','b')")
    props = group.group_properties(scon)
    assert [p.property_id for p in props] == ["C-1"] and props[0].grouped_by == "part_c"


def test_records_without_location_are_skipped(scon):
    filing(scon, "f1", "lis_pendens", property_address=None, property_state=None)
    assert group.group_properties(scon) == []


# ---------------------------------------------------------------------------
# Individual signals
# ---------------------------------------------------------------------------
def test_old_construction_loan_without_takeout_scores(scon, cfg):
    loan(scon, "c", "construction", AS_OF - timedelta(days=365 * 3))
    loan(scon, "m", "modification", AS_OF - timedelta(days=300))
    sp = scores(scon, cfg)["10 Test St, Town, MI"]
    names = [s.name for s in sp.signals]
    assert "construction_loan_no_takeout" in names and "construction_loan_modified" in names


def test_construction_loan_taken_out_by_permanent_is_fine(scon, cfg):
    loan(scon, "c", "construction", AS_OF - timedelta(days=365 * 4))
    loan(scon, "p", "permanent", AS_OF - timedelta(days=365 * 2))
    assert scores(scon, cfg)["10 Test St, Town, MI"].score == 0


def test_young_construction_loan_is_fine(scon, cfg):
    loan(scon, "c", "construction", AS_OF - timedelta(days=365))
    assert scores(scon, cfg)["10 Test St, Town, MI"].score == 0


def test_release_of_a_different_loan_does_not_count_as_payoff(scon, cfg):
    loan(scon, "c", "construction", AS_OF - timedelta(days=365 * 4), inst="I-NEW")
    loan(scon, "r", "release", AS_OF - timedelta(days=365), related="I-OLD")
    names = [s.name for s in scores(scon, cfg)["10 Test St, Town, MI"].signals]
    assert "construction_loan_no_takeout" in names


def test_permit_without_co(scon, cfg):
    filing(scon, "p", "building_permit", permit_number="BP-1", permit_status="Active - Extended",
           filing_date=AS_OF - timedelta(days=1000), permit_expiration_date=AS_OF - timedelta(days=10),
           distress_tags=["permit_extended"])
    names = {s.name for s in scores(scon, cfg)["10 Test St, Town, MI"].signals}
    assert names == {"permit_extended", "permit_expired", "permit_without_co"}


def test_certificate_of_occupancy_clears_permit_signals(scon, cfg):
    filing(scon, "p", "building_permit", filing_date=AS_OF - timedelta(days=1000),
           distress_tags=["permit_extended"])
    filing(scon, "co", "certificate_of_occupancy")
    assert scores(scon, cfg)["10 Test St, Town, MI"].score == 0


def test_recent_big_lien_gets_bonus_old_lien_gets_half(scon, cfg):
    loan(scon, "c", "permanent", AS_OF - timedelta(days=100), amount=10e6)
    filing(scon, "l1", "mechanics_lien", lien_amount=500_000, lien_claimant="HVAC Co",
           recording_date=AS_OF - timedelta(days=20))
    filing(scon, "l2", "mechanics_lien", lien_amount=1_000, recording_date=AS_OF - timedelta(days=400),
           property_address="20 Other St, Town, MI")
    by = scores(scon, cfg)
    rule = cfg["signals"]["mechanics_lien"]
    assert by["10 Test St, Town, MI"].score == rule["points"] + rule["big_lien_bonus"]
    assert by["20 Other St, Town, MI"].score == rule["points"] / 2


def test_needs_review_evidence_is_discounted(scon, cfg):
    filing(scon, "l1", "lis_pendens", status="needs_review", filing_date=AS_OF - timedelta(days=5))
    sp = scores(scon, cfg)["10 Test St, Town, MI"]
    expected = cfg["signals"]["legal_event"]["points_by_tag"]["lis_pendens"] * cfg["needs_review_factor"]
    assert sp.score == expected and "not yet checked" in sp.signals[0].detail


def test_failed_reads_are_ignored(scon, cfg):
    filing(scon, "l1", "foreclosure_notice", status="failed")
    assert score_all(scon, cfg, AS_OF) == []


def test_one_document_counts_once_even_with_several_tags(scon, cfg):
    filing(scon, "l1", "lis_pendens", distress_tags=["lender_lawsuit", "lis_pendens"],
           filing_date=AS_OF - timedelta(days=5))
    sp = scores(scon, cfg)["10 Test St, Town, MI"]
    assert len(sp.signals) == 1


def test_old_legal_event_counts_half(scon, cfg):
    filing(scon, "l1", "foreclosure_notice", filing_date=AS_OF - timedelta(days=cfg["stale_after_days"] + 30))
    full = cfg["signals"]["legal_event"]["points_by_tag"]["foreclosure_scheduled"]
    assert scores(scon, cfg)["10 Test St, Town, MI"].score == full / 2


def test_cmbs_signals(scon, cfg):
    db.insert_row(scon, "loans", {
        "loan_key": "x", "deal_name": "D", "property_address": "5 Mall Rd", "property_state": "TX",
        "dscr": 0.8, "occupancy": 65.0, "watchlist_flag": True, "payment_status": "90+ days delinquent",
        "maturity_date": AS_OF - timedelta(days=30), "current_balance": 5e6})
    sp = scores(scon, cfg)["5 Mall Rd"]
    names = {s.name for s in sp.signals}
    assert names == {"cmbs_dscr_below_1", "cmbs_low_occupancy", "cmbs_watchlist",
                     "cmbs_delinquent", "cmbs_past_maturity"}
    assert sp.score == cfg["family_caps"]["cmbs"]           # family cap holds


def test_healthy_cmbs_loan_scores_zero(scon, cfg):
    db.insert_row(scon, "loans", {
        "loan_key": "x", "property_address": "5 Mall Rd", "property_state": "TX", "dscr": 1.6,
        "occupancy": 0.95, "payment_status": "Current", "maturity_date": AS_OF + timedelta(days=900)})
    assert scores(scon, cfg)["5 Mall Rd"].score == 0


def test_lender_matching_is_fuzzy_but_cautious(scon):
    db.insert_row(scon, "bank_stats", {"rssd_id": "1", "bank_name": "First Example Bank, N.A.",
                                       "report_date": date(2026, 6, 30)})
    book = signals.BankBook(scon)
    assert book.find("FIRST EXAMPLE BANK", 90)["rssd_id"] == "1"
    assert book.find("First Example Bancorp Holdings", 90) is None
    assert book.find(None, 90) is None


def test_lender_signal_uses_latest_quarter(scon, cfg):
    loan(scon, "c", "permanent", AS_OF - timedelta(days=100), lender="Test Bank")
    for q, rising in ((date(2026, 3, 31), True), (date(2026, 6, 30), False)):
        db.insert_row(scon, "bank_stats", {"rssd_id": "7", "bank_name": "Test Bank", "report_date": q,
                                           "rising_two_quarters": rising, "cre_nonaccrual_ratio": 0.01})
    assert scores(scon, cfg)["10 Test St, Town, MI"].score == 0


def test_scout_opening_signals(scon, cfg):
    upsert(scon, "projects", {"project_id": "p1", "project_name": "Lofts", "state": "MI",
                              "property_address": "10 Test St", "stale_candidate": True,
                              "months_overdue": 30.0, "missed_lease_up": True, "date_change_count": 1})
    sp = scores(scon, cfg)["10 Test St"]
    assert sp.score == cfg["family_caps"]["opening"]


def test_tiers(scon, cfg):
    filing(scon, "a", "foreclosure_notice", filing_date=AS_OF, property_address="1 A St")
    filing(scon, "b", "receivership", filing_date=AS_OF, property_address="1 A St")
    filing(scon, "c", "tax_delinquency", filing_date=AS_OF, property_address="2 B St")
    filing(scon, "d", "lis_pendens", filing_date=AS_OF, property_address="3 C St")
    tiers = {sp.facts["property_address"]: sp.tier for sp in score_all(scon, cfg, AS_OF)}
    assert tiers == {"1 A St": "watch", "2 B St": "low", "3 C St": "watch"}


# ---------------------------------------------------------------------------
# The whole thing, on the team's shared fake data set
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_db(env):
    path = env / "fake.duckdb"
    con = open_db(path)
    fake = _load_script("make_fake_data")
    fake.make_troubled_project(con)
    fake.make_healthy_properties(con)
    readings = _load_script("make_fake_readings")
    readings.fake_readings(con)
    readings.fake_scout_project(con)
    readings.fake_middle_properties(con)
    con.close()
    return path


def test_troubled_ann_arbor_project_ranks_first(fake_db, cfg):
    con = open_db(fake_db)
    ranked = score_all(con, cfg)
    top = ranked[0]
    assert "Maize Row" in top.facts["property_name"]
    assert top.tier == "hot"
    assert top.facts["lender_name"] == "First Fakeland Bank"
    families = {s.family for s in top.signals}
    assert {"construction_loan", "permits", "liens", "lender", "opening"} <= families
    # the two middle properties are on the watch list, the 20 healthy ones are low
    assert [sp.tier for sp in ranked[1:3]] == ["watch", "watch"]
    assert all(sp.tier == "low" for sp in ranked[3:])
    assert len(ranked) == 23


def test_fake_data_without_scout_or_extras_still_flags_ann_arbor(env, cfg):
    con = open_db(env / "plain.duckdb")
    fake = _load_script("make_fake_data")
    fake.make_troubled_project(con)
    fake.make_healthy_properties(con)
    _load_script("make_fake_readings").fake_readings(con)
    top = score_all(con, cfg)[0]
    assert "Maize Row" in (top.facts["property_name"] or "") and top.tier == "hot"


def test_write_scores_replaces_previous_run(fake_db, cfg):
    con = open_db(fake_db)
    for _ in range(2):
        write_scores(con, score_all(con, cfg, AS_OF), cfg, AS_OF)
    assert con.execute("SELECT count(*) FROM property_scores").fetchone()[0] == 23
    top = con.execute("SELECT property_name, tier, scoring_version FROM property_scores "
                      "ORDER BY score DESC LIMIT 1").fetchone()
    assert "Maize Row" in top[0] and top[1] == "hot" and top[2] == cfg["version"]
    n_sig = con.execute("SELECT count(*) FROM score_signals WHERE evidence_id IS NULL").fetchone()[0]
    assert n_sig == 0                     # every reason points at its evidence


def test_cli_runs_and_explains(fake_db, capsys):
    assert scorer_main(["--db", str(fake_db), "--csv", "--top", "3"]) == 0
    out = capsys.readouterr().out
    assert "1 hot, 2 watch, 20 low" in out and "Maize Row" in out
    assert scorer_main(["--db", str(fake_db), "--explain", "maize"]) == 0
    assert "evidence: recorded_loans/fake-rl-annarbor-1" in capsys.readouterr().out
    assert scorer_main(["--db", str(fake_db), "--explain", "no such place"]) == 1


def test_scorer_tables_listed():
    assert TABLES == ["property_scores", "score_signals"]
