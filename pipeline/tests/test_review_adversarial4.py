"""Fourth-round probes for 16d98ba: label binding inside the quoted record region, and
whether positive lien counts require an actual index result statement. Fake data only."""
import pytest

from agent import ingest as agent_ingest
from agent import tools as agent_tools
from collectors.common.files import store_file
from matcher.schema import create_matcher_tables
from research.schema import create_research_tables
from tests.b_fixtures import bcon  # noqa: F401

# A genuinely delinquent record: billed 12,000, only 10,000 paid.
DELINQUENT_PAGE = ("FAKE COUNTY treasurer - parcel FAKE-12 - FAKE PINES NURSING HOME - FAKE CITY - "
                   "2025 taxes billed 12,000 paid 10,000 status past due")
DELINQUENT_QUOTE = "2025 taxes billed 12,000 paid 10,000 status past due"
# A non-index page whose text happens to contain a count and lien vocabulary.
NEWS_WITH_COUNT = ("FAKE GAZETTE: FAKE PINES NURSING HOME in FAKE CITY hosted a legal-aid "
                   "clinic where 5 questions about lien filings were answered.")


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


def test_15_swapped_billed_paid_flips_delinquency_to_counter(rcon):
    """Number tokens are matched ANYWHERE in the quote, not to their labels: claiming the
    page's paid figure as billed and vice versa passes the token check and turns a real
    delinquency into paid-current COUNTER-evidence."""
    stored = store_file(rcon, DELINQUENT_PAGE.encode(), source="agent_web",
                        doc_category="web_page", collector_name="test",
                        url="https://county.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-1", property_id="prop-fake-1", check="county_tax",
        state="MI", county="FAKE COUNTY", tax_year=2025, parcel="FAKE-12",
        billed=10000.0, paid=12000.0, status="past due",      # SWAPPED vs the record
        file_id=stored.file_id, source_url="https://county.example/fake",
        quote=DELINQUENT_QUOTE)
    rows = rcon.execute("SELECT direction, value_number, value_text FROM research_evidence").fetchall()
    print("SWAP CHECK: tool ok =", out.get("ok"), "| rows =", rows)
    assert out["ok"] is False or not rows or rows[0][0] != "counter", \
        "label-swapped figures turned a delinquent record into paid-current counter-evidence"


def test_16_positive_lien_count_needs_more_than_vocab(rcon):
    """rc>0 bar is 'count token + result vocabulary in quote': a non-index page with an
    incidental count and the word 'lien' passes - still assertion, not an index result."""
    stored = store_file(rcon, NEWS_WITH_COUNT.encode(), source="agent_web",
                        doc_category="web_page", collector_name="test",
                        url="https://news.example/fake")
    out = agent_tools.record_research_check(
        rcon, "run-1", property_id="prop-fake-1", check="liens",
        query="FAKE PINES liens", result_count=5, state="MI",
        file_id=stored.file_id, source_url="https://news.example/fake",
        quote="5 questions about lien filings were answered")
    rows = rcon.execute("SELECT lane FROM research_searches").fetchall()
    print("VOCAB CHECK: tool ok =", out.get("ok"), "| lanes =", rows)
    assert out["ok"] is False, "an incidental count + the word 'lien' qualified the liens lane"
