"""Tests for the sourcing agent: the loop, the tools, and the safety nets. No network, no API key."""
import json

import pytest

from agent import loop, tools
from collectors.common import config
from collectors.common.files import store_file
from llm import client
from tests.b_fixtures import bcon, make_cfg  # noqa: F401  (bcon is a pytest fixture)

PAGE_TEXT = "Willow Creek Apartments. Now leasing: 240 units, rents from $1,450. Opening Fall 2026."


def tool_reply(name, args, call_id="call_1"):
    """A fake API answer that asks for one tool call."""
    return 200, {"choices": [{"message": {"role": "assistant", "content": None,
                                          "tool_calls": [{"id": call_id, "type": "function",
                                                          "function": {"name": name, "arguments": json.dumps(args)}}]},
                              "finish_reason": "tool_calls"}],
                 "usage": {"prompt_tokens": 50, "completion_tokens": 10}}, {}


def text_reply(text):
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 60, "completion_tokens": 15}}, {}


class FakeAPI:
    def __init__(self, replies):
        self.replies = list(replies)
        self.payloads = []

    def __call__(self, url, headers, payload, timeout):
        self.payloads.append(payload)
        return self.replies.pop(0)


@pytest.fixture
def agent_on(monkeypatch):
    monkeypatch.setenv("BLOOMFIELD_AGENT", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")


@pytest.fixture
def saved_page(bcon):
    """A page the project actually fetched and saved, as fetch_page would have done."""
    stored = store_file(bcon, PAGE_TEXT.encode(), source="agent_web", doc_category="web_page",
                        collector_name="test", url="https://city.example/willow-creek")
    tools.create_tables(bcon)
    return stored.file_id


def test_agent_records_verified_evidence_and_finishes(bcon, agent_on, saved_page, monkeypatch):
    api = FakeAPI([
        tool_reply("record_evidence", {"subject": "Willow Creek", "claim": "The project page lists 240 units.",
                                       "value": "240 units", "file_id": saved_page,
                                       "source_url": "https://city.example/willow-creek",
                                       "quote": "Now leasing: 240 units"}),
        text_reply("Found one sourced fact; nothing else verified."),
    ])
    monkeypatch.setattr(client, "_post_json", api)
    out = loop.run(bcon, "Check the public record for Willow Creek", cfg={"enabled": True})
    assert out["status"] == "done" and out["evidence_rows"] == 1
    row = bcon.execute("SELECT subject, source_kind, file_id, quote FROM evidence").fetchone()
    assert row == ("Willow Creek", "web_page", saved_page, "Now leasing: 240 units")
    # The verified page also became a needs-review lead in the existing filings workflow
    lead = bcon.execute("SELECT status, confidence, quotes FROM filing_fields WHERE file_id = ?",
                        [saved_page]).fetchone()
    assert lead is not None and lead[0] == "needs_review" and "240 units" in lead[2]
    run = bcon.execute("SELECT status, llm_calls FROM agent_runs WHERE run_id = ?", [out["run_id"]]).fetchone()
    assert run == ("done", 2)


def test_agent_step_cap_is_honest(bcon, agent_on, monkeypatch):
    api = FakeAPI([tool_reply("query_db", {"sql": "SELECT 1"}, call_id=f"c{i}") for i in range(6)])
    monkeypatch.setattr(client, "_post_json", api)
    out = loop.run(bcon, "Loop forever", cfg={"enabled": True, "max_steps": 3})
    assert out["status"] == "step_cap"
    assert "ran out of steps" in out["final_answer"]


def test_agent_refuses_when_off(bcon, monkeypatch):
    monkeypatch.delenv("BLOOMFIELD_AGENT", raising=False)
    with pytest.raises(loop.AgentDisabled):
        loop.run(bcon, "anything", cfg={"enabled": False})


# --- query_db: genuinely read-only -------------------------------------------------------------

def test_query_db_allows_select_only(bcon):
    ok = tools.query_db(bcon, "SELECT count(*) AS n FROM loans")
    assert ok["ok"] is True and ok["columns"] == ["n"]
    for bad in ["DELETE FROM loans", "SELECT * FROM loans; DROP TABLE loans",
                "INSERT INTO loans SELECT * FROM loans", "WITH x AS (SELECT 1) DELETE FROM loans"]:
        out = tools.query_db(bcon, bad)
        assert out["ok"] is False, bad


def test_query_db_cannot_leave_the_local_tables(bcon):
    """File, network and table-function reads are refused even inside a syntactically fine SELECT."""
    for bad in ["SELECT * FROM read_csv('/etc/passwd')", "SELECT * FROM read_parquet('s3://bucket/x.parquet')",
                "SELECT * FROM '/tmp/secret.csv'", "SELECT * FROM \"https://evil.example/data.csv\"",
                "SELECT * FROM parquet_scan('x.parquet')", "SELECT * FROM glob('/home/*')"]:
        out = tools.query_db(bcon, bad)
        assert out["ok"] is False, bad
    # ordinary table reads still work
    assert tools.query_db(bcon, "SELECT file_id FROM raw_files LIMIT 1")["ok"] is True


def test_loop_initializes_a_fresh_database(con, agent_on, monkeypatch):
    """No preloaded schema: the run itself must create the cost/evidence tables (fresh-install bug)."""
    api = FakeAPI([text_reply("Nothing to verify yet.")])
    monkeypatch.setattr(client, "_post_json", api)
    out = loop.run(con, "Say hello", cfg={"enabled": True, "max_steps": 2})
    assert out["status"] == "done"
    assert con.execute("SELECT count(*) FROM llm_usage").fetchone()[0] == 1


# --- fetch_page: public addresses only -----------------------------------------------------------

def test_fetch_page_refuses_private_addresses(bcon, monkeypatch):
    called = []
    monkeypatch.setattr(tools.fetch, "polite_get", lambda *a, **k: called.append(a) or None)
    for url in ["http://169.254.169.254/latest/meta-data", "http://127.0.0.1:8080/admin",
                "http://192.168.1.1/", "ftp://example.com/x"]:
        out = tools.fetch_page(bcon, url)
        assert out["ok"] is False, url
    assert called == []                       # nothing was ever requested


def test_fetch_page_refuses_redirect_to_private(bcon, monkeypatch):
    class Resp:
        url = "http://10.0.0.9/internal"     # the public page redirected inside a network
        content = b"x"
        text = "x"
    monkeypatch.setattr(tools.fetch, "polite_get", lambda *a, **k: Resp())
    out = tools.fetch_page(bcon, "https://93.184.216.34/page")   # IP literal: resolves offline, is public
    assert out["ok"] is False and "redirected" in out["error"]


# --- record_evidence: proof is verified, not trusted ---------------------------------------------

def test_evidence_web_proof_must_match_the_saved_page(bcon, saved_page):
    good = tools.record_evidence(bcon, "run-x", subject="Willow Creek", claim="240 units listed.",
                                 file_id=saved_page, source_url="https://city.example/willow-creek",
                                 quote="Now leasing: 240 units")
    assert good["ok"] is True and good["verified_against"] == "web_page"
    # fabricated file_id
    assert tools.record_evidence(bcon, "run-x", subject="s", claim="c", file_id="nope",
                                 source_url="https://city.example/willow-creek",
                                 quote="Now leasing: 240 units")["ok"] is False
    # wrong address for the saved page
    assert tools.record_evidence(bcon, "run-x", subject="s", claim="c", file_id=saved_page,
                                 source_url="https://other.example/page",
                                 quote="Now leasing: 240 units")["ok"] is False
    # quote not on the page (paraphrase / invention)
    assert tools.record_evidence(bcon, "run-x", subject="s", claim="c", file_id=saved_page,
                                 source_url="https://city.example/willow-creek",
                                 quote="400 luxury units available")["ok"] is False
    # no proof at all
    assert tools.record_evidence(bcon, "run-x", subject="s", claim="c")["ok"] is False


def test_evidence_database_proof_is_rerun(bcon):
    ok = tools.record_evidence(bcon, "run-x", subject="loans", claim="no loans loaded yet", value="0",
                               db_query="SELECT count(*) AS n FROM loans")
    assert ok["ok"] is True and ok["verified_against"] == "database"
    wrong = tools.record_evidence(bcon, "run-x", subject="loans", claim="99 loans", value="99",
                                  db_query="SELECT count(*) AS n FROM loans")
    assert wrong["ok"] is False
    empty = tools.record_evidence(bcon, "run-x", subject="x", claim="c",
                                  db_query="SELECT 1 WHERE 1=0")
    assert empty["ok"] is False
    write = tools.record_evidence(bcon, "run-x", subject="x", claim="c", db_query="DELETE FROM loans")
    assert write["ok"] is False


def test_web_search_is_honest_without_a_service(bcon):
    from bot.search import Budget
    out = tools.web_search("anything", bot_cfg={"search": {"provider": "none"}}, budget=Budget({}))
    assert out["ok"] is False and "No search service" in out["error"]


def test_web_search_fails_loudly_when_the_key_is_missing(bcon, monkeypatch):
    # A configured provider without its key is NOT an empty result: the agent and the weekly
    # pass must see "not searched" (ok: false), never confuse it with "searched, found nothing".
    from bot.search import Budget
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    cfg = {"search": {"provider": "tavily", "tavily": {"api_key_env": "TAVILY_API_KEY"}}}
    out = tools.web_search("anything", bot_cfg=cfg, budget=Budget({}))
    assert out["ok"] is False
    assert "TAVILY_API_KEY" in out["error"] and "not set" in out["error"]


def test_tool_schemas_match_dispatch(bcon):
    """Every schema the AI sees has a working branch in run_tool, and vice versa."""
    assert set(tools.TOOL_NAMES) == {"web_search", "fetch_page", "run_collector", "query_db", "record_evidence"}
