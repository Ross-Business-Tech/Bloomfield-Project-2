"""Tests for the source-discovery lane: paid search APIs stay off unless configured,
and every query is recorded. No network: the searcher is injected."""
import pytest

from research import discovery
from research.schema import create_research_tables


@pytest.fixture
def rcon(con):
    create_research_tables(con)
    return con


CFG_OFF = {"search": {"provider": "none"}}
CFG_ON = {"search": {"provider": "tavily", "tavily": {"api_key_env": "TAVILY_API_KEY"}},
          "limits": {"max_pages_per_run": 60, "max_search_queries": 20}}


def fake_search(query, cfg, budget, session=None):
    budget.take_query()
    return [{"title": "Fake County Treasurer", "url": "https://fakecounty.example/tax",
             "snippet": "fake"}]


def test_off_without_a_provider_and_nothing_recorded(rcon):
    out = discovery.discover_sources(rcon, name="FAKE PINES", county="Fake", state="MI",
                                     cfg=CFG_OFF, searcher=fake_search)
    assert out == {"status": "no_search_service", "queries": 0, "candidates": {}}
    assert rcon.execute("SELECT count(*) FROM research_searches").fetchone()[0] == 0


def test_queries_recorded_with_exact_wording_and_counts(rcon):
    out = discovery.discover_sources(rcon, property_key="prop-fake-1", name="FAKE PINES",
                                     address="100 FAKE ST", county="Fake", state="MI",
                                     cfg=CFG_ON, searcher=fake_search)
    assert out["status"] == "ok" and out["queries"] == len(discovery.DISCOVERY_QUERIES)
    rows = rcon.execute("SELECT lane, query, result_count FROM research_searches "
                        "ORDER BY lane").fetchall()
    assert len(rows) == len(discovery.DISCOVERY_QUERIES)
    assert all(r[2] == 1 for r in rows)
    tax = [r for r in rows if r[0] == "county_tax"][0]
    assert "Fake County MI property tax lookup 100 FAKE ST" == tax[1]


def test_budget_caps_queries(rcon):
    from bot.search import Budget
    budget = Budget({"limits": {"max_pages_per_run": 60, "max_search_queries": 2}})

    def counting_search(query, cfg, budget, session=None):
        if not budget.take_query():
            return []
        return [{"title": "t", "url": "https://fake.example/x", "snippet": "s"}]

    out = discovery.discover_sources(rcon, name="FAKE PINES", county="Fake", state="MI",
                                     cfg=CFG_ON, budget=budget, searcher=counting_search)
    got = [u for urls in out["candidates"].values() for u in urls]
    assert len(got) == 2                                    # the budget stopped the rest


def test_ten_x_is_not_a_discovery_lane():
    assert not any("ten-x" in q.lower() or "tenx" in q.lower()
                   for q in discovery.DISCOVERY_QUERIES.values())


# --- the real provider path: paid execution needs an explicit opt-in, then a key -------
def test_real_provider_refuses_without_paid_opt_in_even_with_key(rcon, monkeypatch):
    monkeypatch.delenv("BLOOMFIELD_RESEARCH_PAID", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key-for-test")
    out = discovery.discover_sources(rcon, name="FAKE PINES", county="Fake", state="MI", cfg=CFG_ON)
    assert out["status"] == "paid_disabled" and out["queries"] == 0
    assert rcon.execute("SELECT count(*) FROM research_searches").fetchone()[0] == 0


def test_real_provider_without_key_says_no_service(rcon, monkeypatch):
    monkeypatch.setenv("BLOOMFIELD_RESEARCH_PAID", "1")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = discovery.discover_sources(rcon, name="FAKE PINES", county="Fake", state="MI", cfg=CFG_ON)
    assert out["status"] == "no_search_service" and out["queries"] == 0


def test_candidates_proposed_in_suggested_sources_pending(rcon):
    discovery.discover_sources(rcon, name="FAKE PINES", address="100 FAKE ST", county="Fake",
                               state="MI", cfg=CFG_ON, searcher=fake_search)
    rows = rcon.execute("SELECT domain, status, times_seen FROM suggested_sources").fetchall()
    assert rows and all(r[1] == "pending" for r in rows)          # a person still screens them
    assert any(r[0] == "fakecounty.example" for r in rows)
    discovery.discover_sources(rcon, name="FAKE PINES", address="100 FAKE ST", county="Fake",
                               state="MI", cfg=CFG_ON, searcher=fake_search)
    again = rcon.execute("SELECT count(*), max(times_seen) FROM suggested_sources").fetchone()
    assert again[0] == len(rows) and again[1] == 2                # deduped, just seen again


# --- contract: discovery must call the searcher with bot.search's real signature --------
def test_searcher_call_matches_bot_search_signature(rcon, monkeypatch):
    import inspect

    from bot import search as bot_search
    real_sig = inspect.signature(bot_search.search)
    calls = []

    def spy(*args, **kwargs):
        real_sig.bind(*args, **kwargs)          # raises TypeError if the call shape drifts
        calls.append((args, kwargs))
        return [{"title": "Fake County Treasurer", "url": "https://fakecounty.example/tax"}]

    monkeypatch.setattr(discovery.bot_search, "search", spy)
    monkeypatch.setenv("BLOOMFIELD_RESEARCH_PAID", "1")
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key-for-test")
    out = discovery.discover_sources(rcon, name="FAKE PINES", address="100 FAKE ST",
                                     county="Fake", state="MI", cfg=CFG_ON)
    assert out["status"] == "ok" and calls
    for args, kwargs in calls:                  # (query, cfg, budget, session=...) every time
        assert isinstance(args[0], str) and args[1] is CFG_ON and "session" in kwargs
