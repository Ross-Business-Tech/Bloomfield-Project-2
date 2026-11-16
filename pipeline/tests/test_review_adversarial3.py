"""Third-round independent-review adversarial tests for 5c86cf7.
Parent's asks: wrong year/parcel/value on the page must not clear tax; an incidental
'no records' phrase or a bare positive result_count on a non-index page must not clear
liens. Fake data only; no network."""
from datetime import datetime

import pytest

from agent import ingest as agent_ingest
from agent import tools as agent_tools
from collectors.common.files import store_file
from matcher.schema import create_matcher_tables
from research import gates
from research.schema import create_research_tables
from tests.b_fixtures import bcon  # noqa: F401

TAX_PAGE = ("FAKE COUNTY treasurer - parcel FAKE-12 - FAKE PINES NURSING HOME - FAKE CITY - "
            "2025 taxes billed 10,000 paid 10,000 status paid")
# A news page that corroborates the property AND happens to contain a no-results phrase.
NEWS_PAGE = ("FAKE GAZETTE: FAKE PINES NURSING HOME in FAKE CITY profiled. Officials said "
             "no records were kept of the 1987 parade. The home employs 40 people.")
NEWS_PAGE_PLAIN = ("FAKE GAZETTE: FAKE PINES NURSING HOME in FAKE CITY was mentioned in a "
                   "routine business roundup of FAKE COUNTY facilities.")


@pytest.fixture
def rcon(con):
    create_research_tables(con)
    create_matcher_tables(con)
    agent_tools.create_tables(con)
    agent_ingest.create_tables(con)
    con.execute("INSERT INTO properties (property_id, property_name, street_address, city, state, "
                "county, parcel_number) VALUES ('prop-fake-1', 'FAKE PINES NURSING HOME', "
                "'12 FAKE LANE', 'FAKE CITY', 'MI', 'FAKE COUNTY', 'FAKE-12')")
    return con


def _stored(con, text, url="https://county.example/fake"):
    return store_file(con, text.encode(), source="agent_web", doc_category="web_page",
                      collector_name="test", url=url)


def _tax_call(rcon, stored, **over):
    args = dict(property_id="prop-fake-1", check="county_tax",
                state="MI", county="FAKE COUNTY", tax_year=2025, parcel="FAKE-12",
                billed=10000.0, paid=10000.0, status="paid",
                file_id=stored.file_id, source_url="https://county.example/fake",
                quote="2025 taxes billed 10,000 paid 10,000 status paid")
    args.update(over)
    return agent_tools.record_research_check(rcon, "run-1", **args)


def test_10_tax_year_must_be_on_the_page(rcon):
    """The claimed tax year is never verified: a 2019 'check' against a page that only
    shows 2025 records fake counter-evidence for a year nobody looked at."""
    stored = _stored(rcon, TAX_PAGE)
    out = _tax_call(rcon, stored, tax_year=2019)
    rows = rcon.execute("SELECT as_of, direction FROM research_evidence").fetchall()
    print("YEAR CHECK: tool ok =", out.get("ok"), "| rows =", rows)
    assert out["ok"] is False, "a tax year the page never shows was recorded as verified coverage"


def test_11_digit_substring_misbind_creates_false_delinquency(rcon):
    """Digit-substring figure proof: page shows billed 10,000 paid 10,000; claiming
    paid=1,000 passes ('1000' is a substring of the concatenated digits) and records a
    FALSE delinquency on a paid-current property."""
    stored = _stored(rcon, TAX_PAGE)
    out = _tax_call(rcon, stored, billed=10000.0, paid=1000.0)
    rows = rcon.execute("SELECT direction, value_number FROM research_evidence").fetchall()
    print("DIGIT MISBIND CHECK: tool ok =", out.get("ok"), "| rows =", rows)
    assert out["ok"] is False or (rows and rows[0][0] != "distress"), \
        "a figure the page never states passed as a substring and recorded false distress"


def test_12_incidental_no_records_phrase_is_not_lien_proof(rcon):
    """A news page that merely CONTAINS 'no records' (about something else entirely)
    must not stand as the index's no-results statement."""
    stored = _stored(rcon, NEWS_PAGE, url="https://news.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-1", property_id="prop-fake-1", check="liens",
        query="FAKE PINES", result_count=0, state="MI",
        file_id=stored.file_id, source_url="https://news.example/fake",
        quote="FAKE PINES NURSING HOME in FAKE CITY profiled")
    rows = rcon.execute("SELECT lane FROM research_searches").fetchall()
    print("INCIDENTAL-PHRASE CHECK: tool ok =", out.get("ok"), "| lanes =", rows)
    assert out["ok"] is False, "an incidental 'no records' phrase on a non-index page cleared liens"


def test_13_positive_result_count_on_non_index_page_is_not_lien_proof(rcon):
    """A claimed result_count > 0 has NO proof bar at all today: any corroborating page
    plus a say-so number records a qualified lien check."""
    stored = _stored(rcon, NEWS_PAGE_PLAIN, url="https://news.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-1", property_id="prop-fake-1", check="liens",
        query="FAKE PINES", result_count=5, state="MI",
        file_id=stored.file_id, source_url="https://news.example/fake",
        quote="FAKE PINES NURSING HOME in FAKE CITY was mentioned")
    rows = rcon.execute("SELECT lane FROM research_searches").fetchall()
    print("POSITIVE-RC CHECK: tool ok =", out.get("ok"), "| lanes =", rows)
    assert out["ok"] is False, "a bare positive result_count on a non-index page cleared liens"


def test_14_page_parcel_must_match_the_claimed_parcel(rcon):
    """The claimed parcel is checked against the universe but never against the PAGE:
    a page showing a different parcel can still anchor this parcel's 'verified' check."""
    other_parcel_page = ("FAKE COUNTY treasurer - parcel OTHER-99 - FAKE PINES NURSING HOME - "
                         "FAKE CITY - 2025 taxes billed 10,000 paid 10,000 status paid")
    stored = _stored(rcon, other_parcel_page)
    out = _tax_call(rcon, stored, parcel="FAKE-12")
    rows = rcon.execute("SELECT identity_key FROM research_evidence").fetchall()
    print("PAGE-PARCEL CHECK: tool ok =", out.get("ok"), "| identity keys =", rows)
    assert out["ok"] is False, "a page showing a DIFFERENT parcel anchored this parcel's check"
