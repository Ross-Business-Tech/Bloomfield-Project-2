"""Tests for the EDGAR collector. Uses a pretend sec.gov (Neel's FAKE fixtures); no network."""
import copy
import json
from datetime import date
from pathlib import Path

import pytest

from collectors import edgar
from collectors.common import config, inbox
from collectors.common.log import RunTracker
from collectors.common.parse import load_yaml
from tests.conftest import FakeResponse, FakeSession

E = Path(__file__).parent / "fixtures" / "edgar"
CFG = load_yaml(edgar.CONFIG_PATH)
IDX = "https://www.sec.gov/Archives/edgar/full-index/2026/QTR4/form.idx"
FIL3 = "https://www.sec.gov/Archives/edgar/data/1000003/000100000326000003/"
FIL4 = "https://www.sec.gov/Archives/edgar/data/1000004/000100000426000004/"
TRUST3 = "FAKE Commercial Mortgage Trust 2019-C1"


def pages(bank_filing_works=False):
    p = {
        IDX: FakeResponse((E / "form.idx").read_text(), content_type="text/plain"),
        FIL3 + "index.json": FakeResponse((E / "index_1000003.json").read_text(), content_type="application/json"),
        FIL3 + "exh_102.xml": FakeResponse((E / "exh_102.xml").read_text(), content_type="application/xml"),
    }
    if bank_filing_works:
        p[FIL4 + "index.json"] = FakeResponse('{"directory": {"item": []}}', content_type="application/json")
    return p                  # otherwise FIL4's index.json is "missing" (404): must not stop the run


def collect(con, session, **kw):
    kw.setdefault("since", date(2026, 10, 1))
    kw.setdefault("until", date(2026, 10, 31))
    return edgar.collect(con, session=session, **kw)


def loans(con):
    cur = con.execute("SELECT * FROM loans ORDER BY loan_key")
    cols = [d[0] for d in cur.description]
    return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


# ---- choosing filings -----------------------------------------------------------------------
def test_form_index_parsing_and_cmbs_selection():
    filings = edgar.parse_form_index((E / "form.idx").read_text())
    assert len(filings) == 5 and filings[2].accession == "0001000003-26-000003"
    chosen = edgar.select_filings(filings, CFG, date(2026, 10, 1), date(2026, 10, 31), None)
    assert [f.cik for f in chosen] == ["1000003", "1000004"]        # auto trust, 10-K and a September filing excluded
    only = edgar.select_filings(filings, CFG, date(2026, 10, 1), date(2026, 10, 31), {"1000002"})
    assert [f.cik for f in only] == ["1000002"]


def test_max_trusts_keeps_the_most_recently_filing_trusts():
    filings = edgar.parse_form_index((E / "form.idx").read_text())
    three = edgar.select_filings(filings, CFG, date(2026, 9, 1), date(2026, 10, 31), None)
    assert [f.cik for f in three] == ["1000005", "1000003", "1000004"]
    two = edgar.select_filings(filings, CFG, date(2026, 9, 1), date(2026, 10, 31), None, max_trusts=2)
    assert [f.cik for f in two] == ["1000003", "1000004"]


def test_quarters():
    assert list(edgar.quarters(date(2026, 8, 1), date(2027, 1, 5))) == [(2026, 3), (2026, 4), (2027, 1)]


def test_sec_rate_limit_is_well_under_10_per_second():
    interval = config.domain_settings("www.sec.gov")["seconds_between_requests"]
    assert 1 / float(interval) <= 5                                  # at most 5 requests per second


# ---- end to end ---------------------------------------------------------------------------------
def test_end_to_end_into_loans_and_raw_files(con, env):
    session = FakeSession(pages())
    run = collect(con, session)
    rows = loans(con)
    assert set(rows) == {f"edgar:{TRUST3}:1", f"edgar:{TRUST3}:2"}
    a = rows[f"edgar:{TRUST3}:1"]
    assert a["source"] == "sec_edgar" and a["deal_name"] == TRUST3
    assert a["report_period"] == date(2026, 10, 16) and a["maturity_date"] == date(2027, 7, 1)
    assert a["current_balance"] == 24_100_000 and a["original_balance"] == 25_000_000
    assert a["dscr"] == pytest.approx(0.92) and a["occupancy"] == pytest.approx(0.78)
    assert (a["property_name"], a["property_city"], a["property_state"], a["property_zip"]) == \
        ("Maple Plaza", "Springfield", "IL", "62701")
    assert a["property_type"] == "RT" and a["payment_status"] == "0" and a["servicer_name"] == "Sample Servicing Co"
    assert a["watchlist_flag"] is None and a["servicer_commentary"] is None
    assert rows[f"edgar:{TRUST3}:2"]["property_city"] == "Columbus"          # first property only
    raw = con.execute("SELECT source, doc_category, file_type, url, source_record_id, state, notes FROM raw_files").fetchall()
    assert raw == [("sec_edgar", "other", "xml", FIL3 + "exh_102.xml", "0001000003-26-000003", None,
                    f"deal={TRUST3}; filed=2026-10-14")]
    # the BANK filing's index could not be read: logged, not fatal
    assert run.status == "partial" and len(run.errors) == 1 and "BANK 2099-BNK1" in run.errors[0]
    assert run.files_new == 1


def test_rerun_downloads_nothing_and_adds_no_rows(con, env):
    collect(con, FakeSession(pages(True)))
    session = FakeSession(pages(True))
    run = collect(con, session)
    assert FIL3 + "exh_102.xml" not in session.requested             # known URL: not requested again
    assert run.files_skipped == 1 and run.files_new == 0
    assert len(loans(con)) == 2 and con.execute("SELECT count(*) FROM raw_files").fetchone()[0] == 1


def test_loans_keep_only_the_most_recent_report(con, env):
    path = E / "exh_102.xml"
    new = edgar.parse_asset_xml(path, CFG, deal_name=TRUST3, file_id="f-new")
    old_text = path.read_text().replace("10-16-2026", "09-16-2026").replace("24100000.00", "24500000.00")
    old_path = config.data_dir() / "old.xml"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_text(old_text)
    old = edgar.parse_asset_xml(old_path, CFG, deal_name=TRUST3, file_id="f-old")
    edgar.write_loans(con, new)
    assert edgar.write_loans(con, old) == 0                          # an older report is ignored
    assert loans(con)[f"edgar:{TRUST3}:1"]["current_balance"] == 24_100_000
    # and a newer one replaces it
    newer_path = config.data_dir() / "newer.xml"
    newer_path.write_text(path.read_text().replace("10-16-2026", "11-16-2026").replace("24100000.00", "23900000.00"))
    assert edgar.write_loans(con, edgar.parse_asset_xml(newer_path, CFG, deal_name=TRUST3, file_id="f-newer")) == 2
    row = loans(con)[f"edgar:{TRUST3}:1"]
    assert row["current_balance"] == 23_900_000 and row["report_period"] == date(2026, 11, 16)
    assert con.execute("SELECT count(*) FROM loans").fetchone()[0] == 2


def test_every_downloaded_xml_is_kept_in_raw_files(con, env):
    collect(con, FakeSession(pages(True)))
    (r,) = con.execute("SELECT file_path FROM raw_files").fetchall()
    assert config.from_stored_path(r[0]).read_bytes() == (E / "exh_102.xml").read_bytes()


# ---- the tag audit ------------------------------------------------------------------------------
def test_audit_names_fields_whose_tags_are_wrong():
    cfg = copy.deepcopy(CFG)
    cfg["fields"]["dscr"] = ["someWrongTag"]
    res = edgar.parse_asset_xml(E / "exh_102.xml", cfg, deal_name=TRUST3, file_id="f")
    warnings = edgar.audit_tags(res, cfg)
    assert len(warnings) == 1 and "dscr" in warnings[0] and "assetNumber" in warnings[0]
    assert edgar.audit_tags(edgar.parse_asset_xml(E / "exh_102.xml", CFG, deal_name=TRUST3, file_id="f"), CFG) == []


def test_wrong_loan_tag_gives_a_clear_error_not_a_crash(con, env):
    cfg = copy.deepcopy(CFG)
    cfg["fields"]["loan_number"] = ["nope"]
    with RunTracker(con, "edgar.py") as run:
        edgar.load_file(con, run, "f", E / "exh_102.xml", TRUST3, cfg)
    assert "no loan rows parsed" in run.errors[0] and loans(con) == {}


# ---- manual fallback ---------------------------------------------------------------------------------
def test_manual_xml_in_the_inbox_is_parsed_using_the_deal_name_in_its_notes(con, env):
    folder = config.inbox_dir() / "sec_edgar"
    folder.mkdir(parents=True)
    (folder / "by_hand.xml").write_bytes((E / "exh_102.xml").read_bytes())
    (folder / "by_hand.xml.json").write_text(json.dumps({"notes": "deal=BANK 2099-BNK1"}))
    inbox.load_inbox(con)                                            # generic inbox saves it to raw_files...
    assert con.execute("SELECT count(*) FROM loans").fetchone()[0] == 0
    with RunTracker(con, "edgar.py") as run:
        assert edgar.load_unparsed_manual_files(con, run, CFG) == 1  # ...and edgar.py parses it
    assert set(loans(con)) == {"edgar:BANK 2099-BNK1:1", "edgar:BANK 2099-BNK1:2"}


def test_blocked_sec_falls_back_to_manual_inbox_and_says_so(con, env):
    session = FakeSession({IDX: FakeResponse("denied", 403)})
    session.pages["https://www.sec.gov/robots.txt"] = FakeResponse("", 404)
    run = collect(con, session)
    assert run.status == "failed" and any("sec_edgar" in s for s, _ in run.fallbacks)
