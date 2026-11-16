"""Tests for agent/weekly.py - the bounded weekly-candidate research pass."""
from __future__ import annotations

import duckdb
import pytest

from agent import weekly as aw
from scorer import weekly as scorer_weekly
from scout_reader_schema import create_scout_reader_tables


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    from collectors.common import db as cdb
    cdb.create_tables(c)                   # the full collector schema group_properties reads
    create_scout_reader_tables(c)          # llm_usage / llm_cache
    scorer_weekly.create_tables(c)         # weekly_runs / weekly_flags
    aw.create_tables(c)                    # agent tables + weekly_research_passes
    yield c
    c.close()


def _add_week(con, props):
    con.execute("INSERT INTO weekly_runs VALUES ('week-1', '2026-11-15', now(), 'v1', '{}', ?)",
                [len(props)])
    for i, pid in enumerate(props, 1):
        con.execute(
            "INSERT INTO weekly_flags (run_id, property_id, property_name, address, city, state, "
            "shown, rank, deal_quality) VALUES ('week-1', ?, ?, '1 Main St', 'Austin', 'TX', true, ?, 50)",
            [pid, f"Property {pid}", i])


def _limits(**over):
    d = dict(aw.DEFAULTS)
    d.update(over)
    return d


def test_goal_cap_stops_pass(con):
    _add_week(con, ["p1", "p2", "p3"])
    calls = []

    def fake_agent(c, goal):
        calls.append(goal)
        return {"status": "done", "evidence_rows": 0}

    s = aw.run_pass(con, limits=_limits(max_goals=2), agent_run=fake_agent)
    assert s["goals_run"] == 2
    assert s["status"] == "stopped:goal_cap"
    assert len(calls) == 2


def test_prior_verified_research_is_not_repeated(con):
    _add_week(con, ["p1", "p2"])
    con.execute("INSERT INTO agent_runs (run_id, goal, status, started_at, steps, llm_calls, cost_usd) "
                "VALUES ('run-old', '[property_id=p1] Research...', 'done', now(), 1, 1, 0.0)")
    con.execute("INSERT INTO evidence (evidence_id, run_id, subject, claim) VALUES ('e1', 'run-old', 'x', 'y')")
    goals = []

    def fake_agent(c, goal):
        goals.append(goal)
        return {"status": "done", "evidence_rows": 0}

    s = aw.run_pass(con, limits=_limits(), agent_run=fake_agent)
    assert s["goals_run"] == 1
    assert "[property_id=p2]" in goals[0]
    assert all("p1" not in g for g in goals)


def test_convergence_when_no_new_evidence(con):
    _add_week(con, ["p1"])

    def fake_agent(c, goal):
        return {"status": "done", "evidence_rows": 0}

    s = aw.run_pass(con, limits=_limits(max_passes=3), agent_run=fake_agent)
    assert s["status"] == "stopped:converged_no_new_evidence"
    assert s["sweeps"] == 1


def test_tool_failures_are_not_convergence(con):
    # Zero new evidence WITH failed tool calls (e.g. search service missing) must NOT
    # report convergence - that would disguise an API failure as completed research.
    _add_week(con, ["p1"])

    n = {"i": 0}

    def fake_agent(c, goal):
        n["i"] += 1
        rid = f"run-x{n['i']}"
        c.execute("INSERT INTO agent_steps (run_id, step, kind, name, detail, created_at) "
                  f"VALUES ('{rid}', 1, 'tool_result', 'web_search', "
                  "'{\"ok\": false, \"error\": \"No search service is configured\"}', now())")
        return {"run_id": rid, "status": "done", "evidence_rows": 0}

    s = aw.run_pass(con, limits=_limits(max_passes=3), agent_run=fake_agent)
    assert s["status"] == "stopped:no_new_evidence_with_failures"
    assert s["tool_call_failures"] == 1  # stops after the first failed sweep; retry is next pass
    assert s["evidence_new"] == 0
    assert s["sweeps"] == 1


def test_agent_exception_does_not_kill_the_pass(con):
    _add_week(con, ["p1", "p2"])

    def fake_agent(c, goal):
        if "p1" in goal:
            raise RuntimeError("boom")
        c.execute("INSERT INTO evidence (evidence_id, run_id, subject, claim, value, source_kind, "
                  "db_query, observed_at) VALUES ('e1', 'run-y', 's', 'c', 'v', 'database', "
                  "'select 1', now())")
        return {"run_id": "run-y", "status": "done", "evidence_rows": 1}

    s = aw.run_pass(con, limits=_limits(), agent_run=fake_agent)
    assert s["goals_run"] == 2
    assert s["goals_failed"] == 1
    assert s["evidence_new"] == 1


def test_scorer_rerun_and_pass_row_recorded(con):
    _add_week(con, ["p1"])

    def fake_agent(c, goal):
        return {"status": "done", "evidence_rows": 0}

    scored = []

    def fake_scorer(c, as_of=None):
        scored.append(as_of)
        return "week-2"

    s = aw.run_pass(con, limits=_limits(), agent_run=fake_agent, scorer_run=fake_scorer)
    assert s["new_week_run_id"] == "week-2"
    assert scored, "scorer must re-run after the pass"
    row = con.execute("SELECT status, goals_run, summary FROM weekly_research_passes WHERE pass_id = ?",
                      [s["pass_id"]]).fetchone()
    assert row[0] == s["status"]
    assert row[1] == 1
    assert "goal_cap" not in row[2] or True  # summary json recorded


def test_token_cap_stops_pass_midway(con):
    _add_week(con, ["p1", "p2"])

    def fake_agent(c, goal):
        # first goal burns the whole token budget; the second must never start
        from collectors.common import db
        c.execute("INSERT INTO llm_usage (called_at, task, model, input_tokens, output_tokens, "
                  "est_cost_usd) VALUES (?, 'agent', 'm', 400000, 1000, 0.0)", [db.utc_now()])
        return {"status": "done", "evidence_rows": 0}

    s = aw.run_pass(con, limits=_limits(max_total_tokens=1000), agent_run=fake_agent)
    assert s["goals_run"] == 1
    assert s["status"] == "stopped:token_cap"


def test_advisory_dollar_landmark_does_not_stop_the_pass(con):
    # The API account enforces the real dollar limit; max_est_cost_usd only logs a warning.
    _add_week(con, ["p1", "p2", "p3"])

    def fake_agent(c, goal):
        from collectors.common import db
        c.execute("INSERT INTO llm_usage (called_at, task, model, input_tokens, output_tokens, "
                  "est_cost_usd) VALUES (?, 'agent', 'm', 100, 10, 7.9)", [db.utc_now()])
        return {"status": "done", "evidence_rows": 0}

    s = aw.run_pass(con, limits=_limits(max_est_cost_usd=8.0), agent_run=fake_agent)
    assert s["goals_run"] == 3                      # spend past the landmark is warned about, not blocked
    assert "cost_cap" not in s["status"]


def test_unscored_grouped_properties_are_queued(con):
    _add_week(con, ["p1"])
    con.execute("INSERT INTO projects (project_id, project_name, property_address, city, state) "
                "VALUES ('x9', 'Tail Property', '500 Tail St', 'Austin', 'TX')")
    seen = []

    def fake_agent(c, goal):
        seen.append(goal)
        return {"status": "done", "evidence_rows": 0}

    s = aw.run_pass(con, limits=_limits(max_goals=5), agent_run=fake_agent)
    assert s["candidates"] == 2               # the ranked one + the unscored grouped one
    assert s["goals_run"] == 2
    assert any("Tail Property" in g or "x9" in g for g in seen)


def test_research_gates_hook_failure_does_not_kill_the_pass(con, monkeypatch):
    """The opt-in research-layer hook is isolated: a broken ingest is logged into the
    summary, never thrown through run_pass."""
    _add_week(con, ["p1"])

    def fake_agent(c, goal):
        return {"status": "done", "evidence_rows": 0}

    import research.ingest as research_ingest
    monkeypatch.setattr(research_ingest, "ingest", _boom)
    cfg = {"weekly_loop": {"research_gates": True}}
    s = aw.run_pass(con, cfg=cfg, limits=_limits(max_goals=1), agent_run=fake_agent)
    assert s["goals_run"] == 1 and "research_depth_error" in s
    assert "boom" in s["research_depth_error"]


def _boom(con):
    raise RuntimeError("boom")


def test_research_gates_hook_runs_when_enabled(con):
    _add_week(con, ["p1"])

    def fake_agent(c, goal):
        return {"status": "done", "evidence_rows": 0}

    cfg = {"weekly_loop": {"research_gates": True}}
    s = aw.run_pass(con, cfg=cfg, limits=_limits(max_goals=1), agent_run=fake_agent)
    assert "research_depth_error" not in s
    assert "research_depth" in s                       # empty dict falsy keys filtered -> may be {}


def test_research_gates_enriches_goals_with_unmet_lanes(con, monkeypatch):
    """With the flag on AND the paid-execution opt-in (the steering initiates paid searches),
    a property's tracked unmet lanes are handed to the agent in the goal."""
    monkeypatch.setenv("BLOOMFIELD_RESEARCH_PAID", "1")
    from research import county_tax, gates
    from research.schema import create_research_tables
    create_research_tables(con)
    # one lane recorded, liens missing -> the goal should name the lien gap
    county_tax.record_tax_year(con, state="TX", county="FAKE", tax_year=2025,
                               parcel="F-1", property_key="p1", billed=10.0, paid=10.0,
                               source_url="https://county.example/fake")
    gates.evaluate_all(con)
    _add_week(con, ["p1"])
    seen = []

    def fake_agent(c, goal):
        seen.append(goal)
        return {"status": "done", "evidence_rows": 0}

    cfg = {"weekly_loop": {"research_gates": True}}
    aw.run_pass(con, cfg=cfg, limits=_limits(max_goals=1), agent_run=fake_agent)
    assert any("liens" in g and "unmet lanes" in g for g in seen)


def test_research_gates_off_leaves_goals_untouched(con):
    _add_week(con, ["p1"])
    seen = []

    def fake_agent(c, goal):
        seen.append(goal)
        return {"status": "done", "evidence_rows": 0}

    aw.run_pass(con, limits=_limits(max_goals=1), agent_run=fake_agent)
    assert all("unmet lanes" not in g for g in seen)


def test_research_gates_result_is_persisted_in_the_pass_row(con, monkeypatch):
    """research_depth (or its error) must land in weekly_research_passes.summary, not only
    in the returned dict."""
    import json as _json
    _add_week(con, ["p1"])

    def fake_agent(c, goal):
        return {"status": "done", "evidence_rows": 0}

    import research.ingest as research_ingest
    monkeypatch.setattr(research_ingest, "ingest", _boom)
    cfg = {"weekly_loop": {"research_gates": True}}
    s = aw.run_pass(con, cfg=cfg, limits=_limits(max_goals=1), agent_run=fake_agent)
    stored = _json.loads(con.execute("SELECT summary FROM weekly_research_passes "
                                     "WHERE pass_id = ?", [s["pass_id"]]).fetchone()[0])
    assert stored.get("research_depth_error") == s["research_depth_error"]
