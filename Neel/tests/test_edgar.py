from datetime import date

import pytest
from sqlalchemy import text

from collectors import edgar
from collectors.common import RunStats, count_rows, load_yaml
from conftest import FIXTURES

E = FIXTURES / "edgar"
IDX = "https://www.sec.gov/Archives/edgar/full-index/2026/QTR4/form.idx"
FIL3 = "https://www.sec.gov/Archives/edgar/data/1000003/000100000326000003/"
FIL4 = "https://www.sec.gov/Archives/edgar/data/1000004/000100000426000004/"


def routes(broken_bank_filing=True):
    r = {
        IDX: (E / "form.idx").read_bytes(),
        FIL3 + "index.json": (E / "index_1000003.json").read_bytes(),
        FIL3 + "exh_102.xml": (E / "exh_102.xml").read_bytes(),
    }
    if not broken_bank_filing:
        r[FIL4 + "index.json"] = b'{"directory": {"item": []}}'
    return r  # FIL4 index.json missing -> 404 -> must not stop the run


def test_form_index_parsing_and_cmbs_selection():
    filings = edgar.parse_form_index((E / "form.idx").read_text())
    assert len(filings) == 5 and filings[2].accession == "0001000003-26-000003"
    cfg = load_yaml(edgar.CONFIG_PATH)
    chosen = edgar.select_filings(filings, cfg, date(2026, 10, 1), date(2026, 10, 31), None)
    assert [f.cik for f in chosen] == ["1000003", "1000004"]  # auto trust, 10-K and September filing excluded
    only = edgar.select_filings(filings, cfg, date(2026, 10, 1), date(2026, 10, 31), {"1000002"})
    assert [f.cik for f in only] == ["1000002"]


def test_quarters():
    assert list(edgar.quarters(date(2026, 8, 1), date(2027, 1, 5))) == [(2026, 3), (2026, 4), (2027, 1)]


def test_end_to_end_into_loans(engine, fake_client):
    client = fake_client(routes())
    stats = edgar.run(client, engine, date(2026, 10, 1), date(2026, 10, 31))
    with engine.connect() as conn:
        loans = {r["loan_number"]: dict(r) for r in conn.execute(text("SELECT * FROM loans")).mappings()}
        raw = conn.execute(text("SELECT * FROM raw_files")).mappings().one()
    assert set(loans) == {"1", "2"}
    a = loans["1"]
    assert a["source"] == "EDGAR" and a["loan_id"] == "EDGAR:FAKE Commercial Mortgage Trust 2019-C1:1"
    assert a["reporting_period"] == "2026-10-16" and a["maturity_date"] == "2027-07-01"
    assert a["current_balance"] == 24_100_000 and a["interest_rate"] == pytest.approx(0.0475)
    assert a["dscr"] == pytest.approx(0.92) and a["occupancy"] == pytest.approx(0.78)
    assert a["city"] == "Springfield" and a["lender_name"] == "Example Bank, N.A."
    assert a["special_servicing"] == 1 and loans["2"]["special_servicing"] is None
    assert loans["2"]["city"] == "Columbus"  # first property only
    assert raw["source"] == "EDGAR" and raw["date"] == "2026-10-14" and raw["needs_reading"] == 0
    # the broken BANK filing was logged and skipped, not fatal
    assert stats.rows_written == 2 and len(stats.errors) == 1 and "BANK 2099-BNK1" in stats.errors[0]


def test_rerun_downloads_nothing_and_adds_no_rows(engine, fake_client):
    edgar.run(fake_client(routes(False)), engine, date(2026, 10, 1), date(2026, 10, 31))
    client = fake_client(routes(False))
    stats = edgar.run(client, engine, date(2026, 10, 1), date(2026, 10, 31))
    assert FIL3 + "exh_102.xml" not in client.session.calls  # URL already loaded: not re-requested
    assert stats.files_skipped_duplicate == 1 and stats.rows_written == 0
    assert count_rows(engine, "loans") == 2 and count_rows(engine, "raw_files") == 1


def test_sec_rate_limit_is_under_10_per_second(fake_client):
    client = fake_client({})
    assert client._limiter("www.sec.gov").min_interval >= 0.1
