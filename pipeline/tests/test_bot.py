"""Tests for the web bot. Pretend websites and feeds; no network; every name is FAKE."""
from datetime import date

import pytest

from bot import news, opening, run as botrun, search
from bot.search import Budget
from scout_reader_schema import create_scout_reader_tables
from tests.conftest import FakeResponse, FakeSession

TODAY = date(2026, 11, 1)
ROBOTS_OK = FakeResponse("User-agent: *\nAllow: /", content_type="text/plain")


@pytest.fixture
def bcon(con):
    create_scout_reader_tables(con)
    return con


def cfg(**over):
    c = {"limits": {"max_pages_per_run": 10, "max_search_queries": 3},
         "search": {"provider": "none", "skip_domains": ["zillow.com"]},
         "opening_date": {"watchlist": [], "queries": [],
                          "opened_markers": ["now open", "move in today"],
                          "unopened_markers": ["coming soon", "pre-leasing", "opening soon"]},
         "distress_news": {"rss_feeds": [], "queries": [], "cities": [],
                           "commercial_words": ["apartment", "hotel", "student housing", "lofts"],
                           "residential_words": ["single-family"],
                           "rules": [{"words": ["lis pendens"], "filing_type": "lis_pendens", "tag": "lis_pendens"},
                                     {"words": ["receiver"], "filing_type": "receivership", "tag": "receiver_appointed"},
                                     {"words": ["foreclosure"], "filing_type": "foreclosure_notice", "tag": "foreclosure_scheduled"}]}}
    c.update(over)
    return c


def site(url, html, status=200):
    host = url.split("/")[2]
    return {f"https://{host}/robots.txt": ROBOTS_OK, url: FakeResponse(html, status, url=url)}


WATCH = {"project_name": "FAKE Maize Lofts", "url": "https://maizelofts.example.org/", "city": "Ann Arbor", "state": "MI",
         "property_type": "student_housing"}


def watched(**kw):
    c = cfg()
    c["opening_date"]["watchlist"] = [{**WATCH, **kw}]
    return c


def test_a_late_project_that_still_says_coming_soon_is_flagged(bcon):
    pages = site(WATCH["url"], "<html><body><h1>FAKE Maize Lofts</h1><p>Opening Fall 2025.</p><p>Coming soon! Join the waitlist.</p></body></html>")
    out = opening.run(bcon, watched(), Budget(cfg()), TODAY, session=FakeSession(pages))
    assert out["checked"] == 1 and out["ok"] == 1
    p = bcon.execute("SELECT stated_opening_phrase, opening_deadline, stale_candidate, months_overdue, missed_lease_up, "
                     "page_still_unopened FROM projects").fetchone()
    assert "Fall 2025" in p[0] and str(p[1]) == "2025-12-31" and p[2] and p[3] >= 10 and p[4] and p[5]
    assert bcon.execute("SELECT count(*) FROM project_snapshots").fetchone()[0] == 1
    assert bcon.execute("SELECT count(*) FROM raw_files WHERE source = 'project_site'").fetchone()[0] == 1


def test_an_open_project_is_not_flagged(bcon):
    pages = site(WATCH["url"], "<html><body><p>Opening Fall 2025.</p><p>We are now open - move in today!</p></body></html>")
    opening.run(bcon, watched(), Budget(cfg()), TODAY, session=FakeSession(pages))
    p = bcon.execute("SELECT stale_candidate, page_still_unopened FROM projects").fetchone()
    assert p == (False, False)


def test_a_date_that_moves_is_counted(bcon):
    first = site(WATCH["url"], "<html><body><p>Opening Fall 2026.</p><p>Coming soon.</p></body></html>")
    opening.run(bcon, watched(), Budget(cfg()), TODAY, session=FakeSession(first))
    second = site(WATCH["url"], "<html><body><p>Opening Winter 2027.</p><p>Coming soon.</p></body></html>")
    from collectors.common import fetch
    fetch.reset_state()
    opening.run(bcon, watched(), Budget(cfg()), TODAY, session=FakeSession(second))
    p = bcon.execute("SELECT date_change_count, first_stated_phrase FROM projects").fetchone()
    assert p[0] == 1 and "Fall 2026" in p[1]


def test_a_site_that_blocks_us_is_left_alone(bcon):
    pages = {f"https://maizelofts.example.org/robots.txt": ROBOTS_OK,
             WATCH["url"]: FakeResponse("<html>Please verify you are human (CAPTCHA)</html>")}
    out = opening.run(bcon, watched(), Budget(cfg()), TODAY, session=FakeSession(pages))
    assert out["unreachable"] == 1 and bcon.execute("SELECT count(*) FROM projects").fetchone()[0] == 0


def test_robots_txt_is_obeyed(bcon):
    pages = {"https://maizelofts.example.org/robots.txt": FakeResponse("User-agent: *\nDisallow: /", content_type="text/plain"),
             WATCH["url"]: FakeResponse("<html>Opening Fall 2025. Coming soon.</html>")}
    sess = FakeSession(pages)
    out = opening.run(bcon, watched(), Budget(cfg()), TODAY, session=sess)
    assert out["unreachable"] == 1 and WATCH["url"] not in sess.requested


def test_the_page_limit_stops_the_bot(bcon):
    c = watched()
    c["limits"]["max_pages_per_run"] = 0
    out = opening.run(bcon, c, Budget(c), TODAY, session=FakeSession({}))
    assert out["checked"] == 0


def test_third_party_listing_sites_are_never_opened(bcon):
    c = watched(url="https://www.zillow.com/lofts")
    sess = FakeSession(site("https://www.zillow.com/lofts", "<html>Opening Fall 2025. Coming soon.</html>"))
    out = opening.run(bcon, c, Budget(c), TODAY, session=sess)
    assert out["checked"] == 0 and sess.requested == []


FEED = """<?xml version="1.0"?><rss><channel>
<item><title>Receiver named for FAKE Riverview Apartments</title><link>https://news.example.org/a1</link><description>A judge appointed a receiver</description></item>
<item><title>Foreclosure on a single-family home</title><link>https://news.example.org/a2</link><description>homeowner</description></item>
<item><title>Chef opens new restaurant</title><link>https://news.example.org/a3</link><description>tasty</description></item>
</channel></rss>"""
ARTICLE = ("<html><body><p>A judge appointed a receiver on Monday for the FAKE Riverview Apartments at 410 Example Mill Ave. "
           "The lender sought the receiver after missed payments.</p></body></html>")


def test_news_items_become_needs_review_filings_with_a_real_quote(bcon):
    c = cfg()
    c["distress_news"]["rss_feeds"] = [{"name": "Fake Journal", "url": "https://news.example.org/feed.xml"}]
    pages = {**site("https://news.example.org/feed.xml", FEED), "https://news.example.org/a1": FakeResponse(ARTICLE)}
    out = news.run(bcon, c, Budget(c), TODAY, session=FakeSession(pages))
    assert out["saved"] == 1 and out["skipped"] == 2                # residential and unrelated items are skipped
    r = bcon.execute("SELECT filing_type, status, model_name, property_address, distress_tags, quotes FROM filing_fields").fetchone()
    assert r[0] == "receivership" and r[1] == "needs_review" and r[2] == "bot-keywords"
    assert "410 Example Mill Ave" in r[3] and "receiver_appointed" in r[4]
    import json
    quote = json.loads(r[5])["filing_type"]
    assert quote in news.page_text(ARTICLE)                          # the saved sentence really appears in the article


def test_no_search_key_means_no_open_web_search(bcon, monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    c = cfg()
    c["search"]["provider"] = "brave"
    assert search.search("anything", c, Budget(c), session=FakeSession({})) == []


def test_search_results_skip_listing_sites_and_respect_the_query_limit(bcon, monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    c = cfg()
    c["search"]["provider"] = "brave"
    body = {"web": {"results": [{"url": "https://www.zillow.com/x", "title": "z", "description": ""},
                                {"url": "https://lofts.example.org/", "title": "Lofts", "description": "d"}]}}
    class S:
        def get(self, url, params=None, headers=None, timeout=None):
            assert headers["X-Subscription-Token"] == "test-key"
            return FakeResponse(__import__("json").dumps(body), content_type="application/json")
    b = Budget({"limits": {"max_pages_per_run": 5, "max_search_queries": 1}})
    assert [r["url"] for r in search.search("q", c, b, session=S())] == ["https://lofts.example.org/"]
    assert search.search("q2", c, b, session=S()) == []            # only one query allowed


def test_tavily_provider_searches_and_respects_limits(bcon, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    c = cfg()
    c["search"]["provider"] = "tavily"
    body = {"results": [{"url": "https://www.zillow.com/x", "title": "z", "content": ""},
                        {"url": "https://lofts.example.org/", "title": "Lofts", "content": "d"}]}

    class S:
        def post(self, url, json=None, headers=None, timeout=None):
            assert url == search.TAVILY_URL
            assert headers["Authorization"] == "Bearer test-tavily-key"
            assert json["query"] == "q"
            return FakeResponse(__import__("json").dumps(body), content_type="application/json")

    b = Budget({"limits": {"max_pages_per_run": 5, "max_search_queries": 1}})
    hits = search.search("q", c, b, session=S())
    assert [r["url"] for r in hits] == ["https://lofts.example.org/"]      # skipped domain filtered
    assert hits[0]["snippet"] == "d"
    assert search.search("q2", c, b, session=S()) == []                     # only one query allowed


def test_tavily_without_key_is_quiet(bcon, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    c = cfg()
    c["search"]["provider"] = "tavily"
    assert search.search("anything", c, Budget(c), session=FakeSession({})) == []


def test_one_failing_signal_does_not_stop_the_others(bcon, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("feed exploded")
    monkeypatch.setattr(botrun.news, "run", boom)
    out = botrun.run(bcon, cfg(), signals=["distress_news", "opening_date"], today=TODAY, session=FakeSession({}))
    assert out["distress_news"] == {"error": "feed exploded"} and out["opening_date"]["checked"] == 0


def test_the_signal_list_matches_the_config():
    real = botrun.load_config()
    assert set(real["enabled_signals"]) == set(botrun.DESCRIPTIONS)
    # The user approved the free Tavily account on 2026-11-15; the key still comes only from
    # the environment (TAVILY_API_KEY) and is never committed.
    assert real["search"]["provider"] == "tavily"
    assert real["search"]["tavily"]["api_key_env"] == "TAVILY_API_KEY"
