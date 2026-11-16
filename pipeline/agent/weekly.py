"""
weekly.py - a bounded research pass over this week's ranking candidates.

In plain English: the weekly ranking (scorer/weekly.py) scores every property we know about,
but some scores are low simply because we have no data. This driver hands the sourcing agent
(agent/loop.py) one research goal per candidate property, so it searches the web and saves
verified evidence - then the deterministic scorer re-runs and the ranking reflects what is
actually verifiable, not what happened to be in the database already.

It is BOUNDED on purpose. The dashboard scheduler starts one pass per weekly cycle
(a person can also start one with python -m agent.weekly), and the pass stops at the FIRST
of these limits (set in config/agent.yaml under weekly_loop):

  * max_goals          - how many candidate properties get one research goal each
  * max_model_calls    - total LLM calls across all goals this pass
  * max_total_tokens   - total input+output tokens across all goals this pass
  * max_search_calls   - total web searches this pass (1 search = 1 search-provider credit)
  * max_minutes        - wall-clock limit
  * max_passes         - sweeps over the candidate list (default 1)
  * max_est_cost_usd   - ADVISORY landmark only, NOT a stop: the API account enforces its
                         own dollar limit (user decision 2026-11-15). Passing it logs a
                         warning; llm_usage keeps the full accounting either way.

It also stops early when a full sweep finds zero new verified evidence AND every goal ran
clean (true convergence): more searching is not going to change this week's ranking. If a
sweep finds no new evidence but goals failed (exceptions or failed tool calls, e.g. the
search service missing or out of quota), that is NOT convergence: the pass stops with the
distinct status ``no_new_evidence_with_failures`` so a missing API key or quota can never
masquerade as completed research, and failed properties stay pending for the next pass.

Dedup: a property is skipped when an earlier agent run already researched that exact property
marker and saved at least one evidence row - re-researching what is already verified wastes
budget. The marker is the line "[property_id=...]" inside the goal text.

It never scores anything itself and never changes scoring config. Scores come only from
scorer/weekly.py, before and after the pass, so the ranking change is attributable to the
new evidence.

Run:   python -m agent.weekly                 (uses config/agent.yaml weekly_loop limits)
       python -m agent.weekly --max-goals 5   (override any limit for this pass)
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import date, datetime, timezone

from collectors.common import db
from collectors.common.log import get_logger

from . import loop as agent_loop

log = get_logger("agent.weekly")

PASS_TASK = "agent"          # llm_usage rows written by the agent loop carry this task name

DEFAULTS = {
    "max_goals": 20,
    "max_model_calls": 60,
    "max_total_tokens": 400_000,
    "max_search_calls": 100,
    "max_minutes": 45,
    "max_passes": 1,
    "max_est_cost_usd": 8.0,
    "max_lifetime_est_usd": None,   # hard stop: total est. spend across ALL tasks/passes
    "max_additional_est_usd": None, # hard stop: NEW est. spend on top of the pass-start baseline
}

# Worst-case dollars one goal can cost, reserved BEFORE the goal starts. A goal is at most
# max_steps model calls; each call's output is capped at max_output_tokens, and its input is
# bounded by the instructions plus at most max_steps of tool results capped at
# max_tool_result_chars each (2 per step allowed headroom). Tokens are priced at the official
# U-M Toolkit rates for the configured model from config/reader.yaml.
def _goal_reserve_usd(agent_cfg: dict, reader_cfg: dict | None = None) -> float:
    from llm import client, cost
    reader_cfg = reader_cfg or client.load_reader_config()
    model = client.model_for_task(agent_loop.AGENT_TASK, reader_cfg)
    pin, pout = cost.get_prices(model, reader_cfg)
    steps = int(agent_cfg.get("max_steps", 12))
    max_out = int(agent_cfg.get("max_output_tokens", 2000))
    max_chars = int(agent_cfg.get("max_tool_result_chars", 6000))
    instr_chars = len(agent_loop.load_instructions())
    chars_per_token = float((reader_cfg.get("llm") or {}).get("chars_per_token", 4)) or 4
    worst_in_per_call = (instr_chars + 2000 + steps * 2 * max_chars) / chars_per_token
    worst_out_per_call = max_out
    # get_prices returns DOLLARS PER TOKEN (e.g. gpt-4o: 2.5e-6 in, 1e-5 out).
    # Reserve covers every possible BILLABLE attempt: 1 try + max_retries per call.
    attempts = 1 + int((reader_cfg.get("llm") or {}).get("max_retries", 5))
    usd = attempts * steps * (worst_in_per_call * pin + worst_out_per_call * pout)
    return round(usd, 2)


def load_limits(cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else agent_loop.load_agent_config()
    limits = dict(DEFAULTS)
    for k, v in (cfg.get("weekly_loop") or {}).items():
        if k in limits and v is not None:
            limits[k] = v
    return limits


def create_tables(con) -> None:
    agent_loop.create_tables(con)
    con.execute("""
        CREATE TABLE IF NOT EXISTS weekly_research_passes (
            pass_id      TEXT PRIMARY KEY,
            week_run_id  TEXT,           -- the scorer run this pass researched for
            started_at   TIMESTAMP,
            finished_at  TIMESTAMP,
            status       TEXT,           -- completed | stopped:<reason>
            goals_run    INTEGER,
            goals_skipped INTEGER,
            evidence_new INTEGER,
            llm_calls    INTEGER,
            tokens       INTEGER,
            search_calls INTEGER,
            summary      TEXT
        )""")


# ---------------------------------------------------------------------------
# Candidates and goals
# ---------------------------------------------------------------------------

def _latest_week_run(con) -> str | None:
    row = con.execute("SELECT run_id FROM weekly_runs ORDER BY as_of DESC, run_id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def _candidates(con, week_run_id: str | None) -> list[dict]:
    """
    Every property the latest weekly run scored - shown or not - PLUS every other grouped
    property in the database. A property can be missing from the run (score 0, no context)
    exactly because missing evidence kept its score at zero, so restricting goals to the
    run's rows would hide the properties research could help most. Order: the run's shown
    list first by rank, then its unshown rows, then the remaining grouped properties, so
    limited budget goes to the visible ranking first and the long tail is queued behind it
    for later passes (dedup markers make re-runs resume where the last pass stopped).
    """
    rows = []
    if week_run_id:
        cur = con.execute(
            "SELECT property_id, property_name, address, city, state, shown, rank, deal_quality "
            "FROM weekly_flags WHERE run_id = ? "
            "ORDER BY shown DESC, rank ASC NULLS LAST, deal_quality DESC, property_id",
            [week_run_id])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    seen = {r["property_id"] for r in rows}
    from scorer.group import group_properties
    for p in group_properties(con):
        if p.property_id in seen:
            continue
        info: dict = {"property_id": p.property_id, "property_name": None,
                      "address": None, "city": None, "state": None,
                      "shown": False, "rank": None, "deal_quality": 0}
        for row in p.projects + p.cmbs_loans + p.recorded_loans + p.filings:
            info["property_name"] = info["property_name"] or row.get("project_name") or row.get("property_name")
            info["address"] = info["address"] or row.get("address") or row.get("property_address")
            info["city"] = info["city"] or row.get("city")
            info["state"] = info["state"] or row.get("state")
            if info["address"] or info["property_name"]:
                break
        rows.append(info)
    return rows


def _already_researched(con) -> set[str]:
    """property_id markers an earlier run researched and verified evidence for."""
    rows = con.execute(
        "SELECT DISTINCT r.goal FROM agent_runs r "
        "WHERE r.status IN ('done', 'step_cap') AND r.goal LIKE '%[property_id=%' "
        "AND EXISTS (SELECT 1 FROM evidence e WHERE e.run_id = r.run_id)").fetchall()
    found = set()
    for (goal,) in rows:
        marker = goal.split("[property_id=", 1)[1].split("]", 1)[0].strip()
        if marker:
            found.add(marker)
    return found


def _goal_for(c: dict) -> str:
    where = ", ".join(x for x in [c.get("address"), c.get("city"), c.get("state")] if x)
    name = c.get("property_name") or where or c["property_id"]
    return (
        f"[property_id={c['property_id']}] Research the property \"{name}\""
        + (f" at {where}" if where else "")
        + ". Search the public web for verifiable facts that feed Bloomfield's seven signals: "
        "loan maturity or refinance trouble, opening delays or construction status, rent or "
        "occupancy trends, health/fire inspection history, and anything else public records "
        "show about this property. Save every fact you rely on as an evidence row with its "
        "exact source URL and a verbatim quote; record nothing you cannot verify. If a source "
        "blocks you, say so honestly and move on. Work in as FEW steps as you can: two or three "
        "good searches and one or two page fetches are usually enough; stop as soon as you have "
        "saved what is verifiable or you know there is nothing to find."
    )


# ---------------------------------------------------------------------------
# Budget accounting (this pass only; hard stops, checked before every goal)
# ---------------------------------------------------------------------------

def _pass_usage(con, since: datetime) -> dict:
    calls, tokens, usd = con.execute(
        "SELECT count(*), COALESCE(sum(input_tokens + output_tokens), 0), "
        "COALESCE(sum(est_cost_usd), 0) "
        "FROM llm_usage WHERE task = ? AND called_at >= ?", [PASS_TASK, since]).fetchone()
    searches = con.execute(
        "SELECT count(*) FROM agent_steps WHERE kind = 'tool_call' AND name = 'web_search' "
        "AND created_at >= ?", [since]).fetchone()[0]
    return {"llm_calls": int(calls), "tokens": int(tokens or 0), "search_calls": int(searches),
            "est_cost_usd": float(usd or 0.0)}


def _budget_stop(limits: dict, usage: dict, started: float, goals_run: int,
                 reserve_usd: float = 0.0) -> str | None:
    # NOTE: no dollar stop here. The API account enforces its own dollar limit (user
    # decision 2026-11-15), so max_est_cost_usd is an advisory landmark that only logs.
    # The shape bounds below stay as the operational controls.
    if goals_run >= limits["max_goals"]:
        return "goal_cap"
    if usage["llm_calls"] >= limits["max_model_calls"]:
        return "model_call_cap"
    if usage["tokens"] >= limits["max_total_tokens"]:
        return "token_cap"
    if usage["search_calls"] >= limits["max_search_calls"]:
        return "search_cap"
    if (time.monotonic() - started) / 60 >= limits["max_minutes"]:
        return "time_cap"
    return None


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

def run_pass(con, *, cfg: dict | None = None, limits: dict | None = None,
             agent_run=agent_loop.run, scorer_run=None) -> dict:
    """
    One bounded research pass. agent_run/scorer_run are injectable for tests.
    Returns a summary dict; also writes a weekly_research_passes row.
    """
    limits = limits or load_limits(cfg)
    create_tables(con)
    pass_id = f"pass-{uuid.uuid4().hex[:12]}"
    started_clock = time.monotonic()
    started_at = db.utc_now()
    week_run_id = _latest_week_run(con)
    con.execute(
        "INSERT INTO weekly_research_passes (pass_id, week_run_id, started_at, status, "
        "goals_run, goals_skipped, evidence_new, llm_calls, tokens, search_calls) "
        "VALUES (?,?,?,?,0,0,0,0,0,0)",
        [pass_id, week_run_id, started_at, "running"])

    candidates = _candidates(con, week_run_id)
    researched = _already_researched(con)
    ev_before = con.execute("SELECT count(*) FROM evidence").fetchone()[0]
    # Freeze the spend baseline at pass start: the "additional spend" budget is measured
    # against everything the account had already used (all tasks, all time) RIGHT NOW.
    baseline_usd = float(con.execute("SELECT COALESCE(sum(est_cost_usd), 0) FROM llm_usage").fetchone()[0])

    reserve = _goal_reserve_usd(agent_loop.load_agent_config() if cfg is None else cfg)
    goals_run = goals_skipped = 0
    stop = None
    sweeps = 0
    warned = {"usd": False}
    failed_goals = 0
    failed_tool_calls = 0
    while sweeps < max(1, int(limits["max_passes"])):
        sweeps += 1
        sweep_new = 0
        sweep_failures = 0
        pending = [c for c in candidates if c["property_id"] not in researched]
        if not pending:
            stop = "all_candidates_researched"
            break
        for c in pending:
            usage = _pass_usage(con, started_at)
            landmark = float(limits.get("max_est_cost_usd", 0) or 0)
            if landmark and usage["est_cost_usd"] > landmark and not warned["usd"]:
                log.warning("agent pass est. spend $%.2f passed the advisory landmark $%.2f "
                            "(not enforced; the API account enforces the real dollar limit)",
                            usage["est_cost_usd"], landmark)
                warned["usd"] = True
            stop = _budget_stop(limits, usage, started_clock, goals_run, reserve)
            if stop:
                break
            lifetime_cap = limits.get("max_lifetime_est_usd")
            additional_cap = limits.get("max_additional_est_usd")
            if lifetime_cap or additional_cap:
                # Every chargeable task on the account counts, not just this pass, and the
                # next goal's WORST-CASE reserve must still fit under the cap: a goal can
                # overshoot, so the stop is spend + reserve >= cap, never spend alone.
                lifetime_usd = float(con.execute(
                    "SELECT COALESCE(sum(est_cost_usd), 0) FROM llm_usage").fetchone()[0])
                if lifetime_cap and lifetime_usd + reserve >= float(lifetime_cap):
                    stop = "lifetime_cost_cap"
                    break
                if additional_cap and (lifetime_usd - baseline_usd) + reserve >= float(additional_cap):
                    stop = "additional_cost_cap"
                    break
            before = con.execute("SELECT count(*) FROM evidence").fetchone()[0]
            try:
                result = agent_run(con, _goal_for(c))
            except Exception:
                # One bad goal must not kill the pass. The property stays pending so a
                # later pass retries it, and the failure is counted honestly.
                log.exception("pass %s goal %s: agent run raised", pass_id, c["property_id"])
                goals_run += 1
                failed_goals += 1
                sweep_failures += 1
                continue
            goals_run += 1
            tool_failures = 0
            if result.get("run_id"):
                tool_failures = con.execute(
                    "SELECT count(*) FROM agent_steps WHERE run_id = ? AND kind = 'tool_result' "
                    "AND detail LIKE '%\"ok\": false%'", [result["run_id"]]).fetchone()[0]
            failed_tool_calls += tool_failures
            if result.get("status") == "failed" or tool_failures > 0:
                sweep_failures += 1
            gained = con.execute("SELECT count(*) FROM evidence").fetchone()[0] - before
            sweep_new += max(0, gained)
            if result.get("evidence_rows", 0) > 0:
                researched.add(c["property_id"])
            log.info("pass %s goal %s: %s, +%d evidence, %d failed tool calls",
                     pass_id, c["property_id"], result.get("status"), gained, tool_failures)
        else:
            # full sweep finished without hitting a cap
            if sweep_new == 0 and sweep_failures == 0:
                stop = "converged_no_new_evidence"
                break
            if sweep_new == 0:
                # Zero new evidence WITH failures is not convergence: the search service
                # may be missing or out of quota. Say so explicitly.
                stop = "no_new_evidence_with_failures"
                break
            stop = None
            continue
        break
    goals_skipped = len([c for c in candidates if c["property_id"] in researched]) - goals_run
    stop = stop or ("passes_done" if sweeps >= int(limits["max_passes"]) else "completed")

    # Re-score: the deterministic scorer re-reads the database, so the ranking now reflects
    # the new verified evidence. Scoring config is never touched here.
    new_run_id = None
    if scorer_run is not None:
        new_run_id = scorer_run(con, as_of=date.today())

    usage = _pass_usage(con, started_at)
    ev_new = con.execute("SELECT count(*) FROM evidence").fetchone()[0] - ev_before
    summary = {
        "pass_id": pass_id, "week_run_id": week_run_id, "new_week_run_id": new_run_id,
        "status": f"stopped:{stop}" if stop not in ("completed", "passes_done") else stop,
        "candidates": len(candidates), "goals_run": goals_run,
        "goals_failed": failed_goals, "tool_call_failures": failed_tool_calls,
        "goals_skipped_already_researched": max(0, goals_skipped),
        "evidence_new": ev_new, "llm_calls": usage["llm_calls"],
        "tokens": usage["tokens"], "search_calls": usage["search_calls"],
        "est_cost_usd": round(usage["est_cost_usd"], 4), "goal_reserve_usd": reserve,
        "sweeps": sweeps, "limits": limits,
    }
    con.execute(
        "UPDATE weekly_research_passes SET finished_at=?, status=?, goals_run=?, "
        "goals_skipped=?, evidence_new=?, llm_calls=?, tokens=?, search_calls=?, summary=? "
        "WHERE pass_id=?",
        [db.utc_now(), summary["status"], goals_run, summary["goals_skipped_already_researched"],
         ev_new, usage["llm_calls"], usage["tokens"], usage["search_calls"],
         json.dumps(summary), pass_id])
    log.info("weekly research pass %s: %s", pass_id, json.dumps(summary))
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description="Bounded weekly-candidate research pass.")
    for k in DEFAULTS:
        p.add_argument(f"--{k.replace('_', '-')}", type=int, default=None)
    args = p.parse_args()
    overrides = {k: v for k, v in vars(args).items() if v is not None}
    limits = load_limits()
    limits.update(overrides)
    con = db.connect()
    from scorer import weekly as scorer_weekly
    summary = run_pass(con, limits=limits, scorer_run=scorer_weekly.run)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
