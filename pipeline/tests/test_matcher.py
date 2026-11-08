"""Tests for Part C (the Matcher). No internet, no AI: fake data only."""
import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from collectors.common import db
from matcher import normalize as nz
from matcher.__main__ import main as matcher_main
from matcher.entities import build_entities, numbers_conflict, request_lookups
from matcher.lenders import match_lenders
from matcher.loan_history import build_histories
from matcher.run import load_matching_config, run_matcher
from matcher.schema import create_matcher_tables, open_db, rows
from scorer.schema import open_db as scorer_open_db
from scorer.score import load_scoring_config, score_all
from scout_reader_schema import create_scout_reader_tables, upsert

AS_OF = date(2026, 11, 1)
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mcon(con):
    create_scout_reader_tables(con)
    create_matcher_tables(con)
    return con


@pytest.fixture
def cfg():
    return load_matching_config()


def rl(con, rid, kind="permanent", *, parcel=None, address=None, state="MI", rec=date(2024, 1, 1),
       grantor="Borrower LLC", grantee="Test Bank", amount=1e6, inst=None, related=None):
    db.insert_row(con, "recorded_loans", {
        "record_id": rid, "state": state, "county": "Test County", "instrument_number": inst or rid,
        "loan_kind": kind, "recording_date": rec, "grantor": grantor, "grantee": grantee, "amount": amount,
        "parcel_number": parcel, "property_address": address, "related_instrument": related})


def ff(con, fid, **kw):
    row = {"file_id": fid, "filing_type": "other", "status": "ok"}
    if "other_parties" in kw:
        kw["other_parties"] = json.dumps(kw["other_parties"])
    row.update(kw)
    upsert(con, "filing_fields", row)


def links(con):
    return {r["record_id"]: r for r in rows(con, "SELECT * FROM property_links")}


# ---------------------------------------------------------------------------
# Normalizing
# ---------------------------------------------------------------------------
def test_entity_names_normalize():
    assert nz.entity_key("Maize Row Holdings, L.L.C.") == nz.entity_key("MAIZE ROW HOLDINGS LLC") == "maize row holdings"
    assert nz.entity_key("The Smith & Jones Co.") == "smith and jones"
    assert nz.entity_key("Fakeville Heating & Cooling Inc. (HVAC contractor)") == "fakeville heating and cooling"
    assert nz.entity_kind("Acme Partners, L.P.") == "partnership"
    assert nz.entity_kind("First Example Bank") == "bank"
    assert nz.entity_kind("Jane Doe") == "other"


def test_bank_names_normalize():
    assert nz.bank_key("First Example Bank, N.A.") == nz.bank_key("FIRST EXAMPLE BANK") == "first example bank"


def test_addresses_normalize():
    assert nz.street_key("10 North Main Street, Suite 200, Town, MI") == "10 n main st"
    assert nz.street_key("10 N. Main St. #4") == "10 n main st"
    assert nz.split_address("1000 Example Way, Ann Arbor, MI 48104") == ("1000 Example Way", "Ann Arbor", "MI", "48104")
    assert nz.zip5("00000") is None and nz.zip5("48104-1234") == "48104"
    assert nz.parcel_key("09-12-345-001") == nz.parcel_key("0912345001")


def test_numbered_names_are_different_entities():
    assert numbers_conflict("example property 01", "example property 02")
    assert numbers_conflict("maize row phase i", "maize row phase ii")
    assert not numbers_conflict("maize row holdings", "maize row holding")


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------
def test_parcel_and_address_link(mcon, cfg):
    rl(mcon, "a", "construction", parcel="P-100", address="10 Main St, Town, MI")
    rl(mcon, "b", "modification", parcel="p100")                       # parcel only
    ff(mcon, "f", property_address="10 Main Street, Town, MI 49999", property_state="MI")  # address only
    run_matcher(mcon, cfg, AS_OF)
    lk = links(mcon)
    assert lk["a"]["property_id"] == lk["b"]["property_id"] == lk["f"]["property_id"]
    assert lk["b"]["match_method"] == "parcel" and lk["f"]["match_method"] == "address"


def test_typo_in_street_auto_links(mcon, cfg):
    rl(mcon, "a", address="1200 Washtenaw Avenue, Ann Arbor, MI")
    ff(mcon, "f", property_address="1200 Washtenw Ave, Ann Arbor, MI", property_state="MI")
    run_matcher(mcon, cfg, AS_OF)
    lk = links(mcon)
    assert lk["a"]["property_id"] == lk["f"]["property_id"]
    assert lk["f"]["match_method"] == "fuzzy_address"


def test_different_street_type_or_direction_never_auto_links(mcon, cfg):
    rl(mcon, "a", address="100 Main St, Town, MI")
    rl(mcon, "b", address="100 Main Ave, Town, MI")
    rl(mcon, "c", address="100 N Elm St, Town, MI")
    rl(mcon, "d", address="100 S Elm St, Town, MI")
    run_matcher(mcon, cfg, AS_OF)
    assert len({r["property_id"] for r in links(mcon).values()}) == 4
    reasons = [r["reason"] for r in rows(mcon, "SELECT * FROM match_review")]
    assert "direction differs" in reasons


def test_same_street_other_state_or_zip_stays_apart(mcon, cfg):
    rl(mcon, "a", address="5 Oak St, Town, MI 48104")
    rl(mcon, "b", address="5 Oak St, Town, OH 43001", state="OH")
    ff(mcon, "c", property_address="5 Oak Stt, Elsewhere, MI 49001", property_state="MI")
    run_matcher(mcon, cfg, AS_OF)
    assert len({r["property_id"] for r in links(mcon).values()}) == 3


def test_name_only_record_links_by_property_name(mcon, cfg):
    ff(mcon, "site", project_name="Maize Row Student Lofts", property_address="1000 Example Way, Ann Arbor, MI",
       property_state="MI", property_city="Ann Arbor")
    ff(mcon, "news", project_name="The Maize Row Student Lofts", property_state="MI", property_city="Ann Arbor")
    ff(mcon, "other", project_name="Blue Lake Apartments", property_state="MI", property_city="Ann Arbor")
    run_matcher(mcon, cfg, AS_OF)
    lk = links(mcon)
    assert lk["news"]["property_id"] == lk["site"]["property_id"]
    assert lk["news"]["match_method"] == "property_name"
    assert lk["other"]["property_id"] != lk["site"]["property_id"]


def test_records_with_nothing_to_go_on_are_not_linked(mcon, cfg):
    ff(mcon, "x", property_state="MI")
    run_matcher(mcon, cfg, AS_OF)
    assert links(mcon) == {}


def test_a_persons_answer_is_obeyed(mcon, cfg):
    rl(mcon, "a", address="100 N Elm St, Town, MI")
    rl(mcon, "b", address="100 S Elm St, Town, MI")
    run_matcher(mcon, cfg, AS_OF)
    (rid,) = mcon.execute("SELECT review_id FROM match_review").fetchone()
    mcon.execute("UPDATE match_review SET decision = 'yes' WHERE review_id = ?", [rid])
    run_matcher(mcon, cfg, AS_OF)
    lk = links(mcon)
    assert lk["a"]["property_id"] == lk["b"]["property_id"] and lk["a"]["match_method"] == "manual"
    mcon.execute("UPDATE match_review SET decision = 'no' WHERE review_id = ?", [rid])
    run_matcher(mcon, cfg, AS_OF)
    lk = links(mcon)
    assert lk["a"]["property_id"] != lk["b"]["property_id"]
    assert mcon.execute("SELECT count(*) FROM match_review").fetchone()[0] == 1   # not asked again


def test_property_ids_are_stable_and_match_the_scorer_fallback(mcon, cfg):
    rl(mcon, "a", "construction", parcel="P-1", address="10 Main St, Town, MI", rec=date(2022, 1, 1))
    from scorer.group import group_properties
    fallback_id = group_properties(mcon)[0].property_id
    run_matcher(mcon, cfg, AS_OF)
    first = links(mcon)["a"]["property_id"]
    run_matcher(mcon, cfg, AS_OF)
    assert links(mcon)["a"]["property_id"] == first == fallback_id


# ---------------------------------------------------------------------------
# Loan histories
# ---------------------------------------------------------------------------
def _h(recs, as_of=AS_OF):
    prop = {f"recorded_loans:{r['record_id']}": "P" for r in recs}
    return {h["history_id"]: h for h in build_histories(recs, prop, {"Bank A": "123"}, as_of)}


def _r(rid, kind, d, inst=None, related=None, grantee="Bank A"):
    return {"record_id": rid, "loan_kind": kind, "recording_date": d, "instrument_number": inst or rid,
            "related_instrument": related, "grantor": "B LLC", "grantee": grantee, "amount": 1e6}


def test_history_construction_modified_then_refinanced_then_released():
    h = _h([_r("c", "construction", date(2020, 1, 1)), _r("m", "modification", date(2021, 6, 1)),
            _r("p", "permanent", date(2022, 1, 1)), _r("x", "release", date(2022, 2, 1), related="c")])
    assert h["c"]["modification_count"] == 1 and h["c"]["takeout_record_id"] == "p"
    assert h["c"]["status"] == "released" and h["c"]["release_record_id"] == "x"
    assert h["c"]["lender_rssd_id"] == "123"
    assert h["p"]["status"] == "open" and h["p"]["modification_count"] == 0
    assert h["c"]["record_ids"] == "c, m, x"


def test_history_open_construction_loan():
    h = _h([_r("c", "construction", date(2023, 5, 1)), _r("e", "extension", date(2025, 5, 1))])
    assert h["c"]["status"] == "open" and h["c"]["modification_count"] == 1
    assert 41 < h["c"]["months_open"] < 43


def test_history_release_without_reference_closes_oldest_open_loan():
    h = _h([_r("old", "permanent", date(2015, 1, 1)), _r("new", "permanent", date(2020, 1, 1)),
            _r("x", "release", date(2020, 2, 1)), _r("a", "assignment", date(2021, 1, 1))])
    assert h["old"]["status"] == "released" and h["new"]["status"] == "open"
    assert h["new"]["assignment_count"] == 1


# ---------------------------------------------------------------------------
# Lenders
# ---------------------------------------------------------------------------
def test_lender_links(mcon, cfg):
    for rssd, name in (("1", "First Example Bank"), ("2", "Lakeshore Savings Bank")):
        db.insert_row(mcon, "bank_stats", {"rssd_id": rssd, "bank_name": name, "report_date": date(2026, 6, 30)})
    rl(mcon, "a", grantee="FIRST EXAMPLE BANK, N.A.", address="1 A St")
    rl(mcon, "b", grantee="Lakeshore Saving Bank", address="2 B St")     # typo -> fuzzy
    rl(mcon, "c", grantee="Lakeshore Capital Partners", address="3 C St")
    ff(mcon, "f", lender_name="Some Private Lender LLC")
    out, _ = match_lenders(mcon, cfg)
    got = {r["lender_name"]: (r["rssd_id"], r["match_method"]) for r in out}
    assert got["FIRST EXAMPLE BANK, N.A."] == ("1", "exact")
    assert got["Lakeshore Saving Bank"] == ("2", "fuzzy")
    assert got["Lakeshore Capital Partners"][0] is None
    assert got["Some Private Lender LLC"] == (None, "none")


def test_scorer_trusts_part_c_lender_links(mcon, cfg):
    db.insert_row(mcon, "bank_stats", {"rssd_id": "1", "bank_name": "Test Bank", "report_date": date(2026, 6, 30),
                                       "rising_two_quarters": True, "cre_nonaccrual_ratio": 0.01})
    rl(mcon, "a", address="1 A St", grantee="Test Bank")
    run_matcher(mcon, cfg, AS_OF)
    con = mcon
    from scorer.schema import create_scorer_tables
    create_scorer_tables(con)
    sc = load_scoring_config()
    assert [s.name for s in score_all(con, sc, AS_OF)[0].signals] == ["lender_nonaccruals_rising"]
    con.execute("UPDATE lender_links SET rssd_id = NULL")     # e.g. a person said "not that bank"
    assert score_all(con, sc, AS_OF)[0].signals == []


# ---------------------------------------------------------------------------
# Entities and state lookups
# ---------------------------------------------------------------------------
def test_entities_merge_variants_but_not_numbered_siblings(mcon, cfg):
    rl(mcon, "a", grantor="Maize Row Holdings, L.L.C.", address="1 A St")
    ff(mcon, "f", borrower_name="MAIZE ROW HOLDINGS LLC", developer_name="Maize Row Holding LLC",
       property_state="MI", property_address="1 A St",
       other_parties=[{"name": "Fakeville HVAC Inc.", "role": "Lien_Claimant"}])
    rl(mcon, "b", grantor="Example Property 01 LLC", address="2 B St")
    rl(mcon, "c", grantor="Example Property 02 LLC", address="3 C St")
    run_matcher(mcon, cfg, AS_OF)
    ents = {e["canonical_name"]: e for e in rows(mcon, "SELECT * FROM entities")}
    maize = [e for n, e in ents.items() if n.lower().startswith("maize")]
    assert len(maize) == 1
    assert set(maize[0]["roles"].split(", ")) == {"borrower", "developer"}
    assert len(json.loads(maize[0]["name_variants"])) == 3
    assert "Example Property 01 LLC" in ents and "Example Property 02 LLC" in ents
    assert ents["Fakeville HVAC Inc."]["roles"] == "lien_claimant"
    assert ents["Test Bank"]["entity_kind"] == "bank"


def test_lookup_requests_for_borrower_companies_only_once(mcon, cfg):
    rl(mcon, "a", grantor="Maize Row Holdings LLC", grantee="Test Bank", address="1 A St")
    rl(mcon, "b", grantor="Jane Doe", grantee="Test Bank", address="2 B St")
    run_matcher(mcon, cfg, AS_OF)
    run_matcher(mcon, cfg, AS_OF)
    req = rows(mcon, "SELECT * FROM entity_lookup_requests")
    assert [(r["entity_name"], r["state"], r["status"], r["requested_by"]) for r in req] == \
        [("Maize Row Holdings LLC", "MI", "pending", "part_c_matcher")]


def test_request_lookups_skips_names_already_asked(mcon, cfg):
    db.insert_row(mcon, "entity_lookup_requests", {"entity_name": "ACME LLC", "state": "MI", "status": "done"})
    ents, _ = build_entities(mcon, {}, cfg)
    assert request_lookups(mcon, [{"canonical_name": "Acme LLC", "entity_kind": "llc", "roles": "borrower",
                                   "states": "MI"}], cfg) == 0
    assert ents == []


# ---------------------------------------------------------------------------
# CMBS loans across sources
# ---------------------------------------------------------------------------
def _cmbs(con, key, source, deal, bal, addr="9 Plaza Dr"):
    db.insert_row(con, "loans", {"loan_key": key, "source": source, "deal_name": deal, "property_address": addr,
                                 "property_city": "Phoenix", "property_state": "AZ", "original_balance": bal})


def test_same_loan_from_edgar_and_trepp_is_linked(mcon, cfg):
    _cmbs(mcon, "edgar:A:1", "sec_edgar", "BANK 2019-BNK20", 10e6)
    _cmbs(mcon, "trepp:A:1", "trepp", "Bank 2019-BNK20", 10.1e6)
    _cmbs(mcon, "edgar:B:9", "sec_edgar", "OTHER 2020-C1", 3e6)            # same property, other loan
    _cmbs(mcon, "trepp:C:5", "trepp", "THIRD 2021-X", 3.03e6)              # matches B by balance only
    run_matcher(mcon, cfg, AS_OF)
    g = {r["loan_key"]: r for r in rows(mcon, "SELECT * FROM loan_links")}
    assert g["edgar:A:1"]["loan_group_id"] == g["trepp:A:1"]["loan_group_id"]
    assert g["edgar:A:1"]["match_method"] == "same_deal_and_property"
    assert g["edgar:B:9"]["loan_group_id"] == g["trepp:C:5"]["loan_group_id"]
    assert g["edgar:B:9"]["match_method"] == "same_property_balance"
    assert g["edgar:A:1"]["loan_group_id"] != g["edgar:B:9"]["loan_group_id"]


def test_two_loans_from_one_source_stay_separate(mcon, cfg):
    _cmbs(mcon, "edgar:A:1", "sec_edgar", "DEAL", 10e6)
    _cmbs(mcon, "edgar:A:2", "sec_edgar", "DEAL", 10e6)
    run_matcher(mcon, cfg, AS_OF)
    assert mcon.execute("SELECT count(DISTINCT loan_group_id) FROM loan_links").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# The whole thing on the team's fake data, then the scorer on top
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


def test_fake_data_end_to_end(fake_db, cfg):
    con = open_db(fake_db)
    s = run_matcher(con, cfg)
    assert s["properties"] == 24 and s["open_construction_loans"] == 1
    assert s["cmbs_cross_source_groups"] == 1 and s["lenders_linked"] == s["lender_names"]
    assert s["pending_reviews"] == 1                       # the "Exmaple Drive" typo

    maize = rows(con, "SELECT * FROM properties WHERE property_name LIKE '%Maize Row%'")
    assert len(maize) == 1 and maize[0]["record_count"] == 5
    assert maize[0]["parcel_number"] == "FAKE-09-12-345-001"
    hist = rows(con, "SELECT * FROM loan_histories WHERE property_id = ?", [maize[0]["property_id"]])
    assert len(hist) == 1 and hist[0]["status"] == "open" and hist[0]["modification_count"] == 1
    assert hist[0]["lender_rssd_id"] == "9990001"

    # Scorer on top: uses Part C's grouping, still ranks Ann Arbor first, no double-counted CMBS loan.
    sc = load_scoring_config()
    ranked = score_all(scorer_open_db(fake_db), sc)
    assert all(sp.prop.grouped_by == "part_c" for sp in ranked)
    assert "Maize Row" in ranked[0].facts["property_name"] and ranked[0].tier == "hot"
    camel = next(sp for sp in ranked if "Camelback" in (sp.facts["property_name"] or ""))
    names = [s.name for s in camel.signals]
    assert len(names) == len(set(names))


def test_cli(fake_db, capsys):
    assert matcher_main(["--db", str(fake_db)]) == 0
    assert "into 24 properties" in capsys.readouterr().out
    assert matcher_main(["--db", str(fake_db), "--review"]) == 0
    out = capsys.readouterr().out
    rid = next(w for w in out.split() if w.startswith("rev-"))
    assert "Exmaple" in out
    assert matcher_main(["--db", str(fake_db), "--decide", rid, "maybe"]) == 1
    assert matcher_main(["--db", str(fake_db), "--decide", rid, "yes"]) == 0
    assert matcher_main(["--db", str(fake_db)]) == 0
    assert "into 23 properties" in capsys.readouterr().out
    assert matcher_main(["--db", str(fake_db), "--show", "maize"]) == 0
    out = capsys.readouterr().out
    assert "OPEN after" in out and "lien_claimant" in out
    assert matcher_main(["--db", str(fake_db), "--show", "nowhere"]) == 1
