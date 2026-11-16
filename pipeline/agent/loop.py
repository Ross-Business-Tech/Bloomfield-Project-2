"""
loop.py - the sourcing agent's think-act loop.

In plain English: you give the agent a goal in plain words ("find what the public record says about
the Willow Creek project"). It reads its standing instructions from config/agent.md, plans, calls the
read-only tools in agent/tools.py one step at a time, records every fact it relies on as an evidence
row with the exact web address it came from, and finishes with a plain-English answer.

What the agent is FOR: sourcing information and compiling evidence. What it is NOT for: scoring.
Scores and flags come only from the deterministic scorer (scorer/), which reads what is in the
database. The team changes behaviour by editing config/agent.md (sourcing instructions) and the
scorer config (scoring) - never by letting the AI grade its own homework.

The agent is OFF unless you turn it on: set BLOOMFIELD_AGENT=1 in .env or `enabled: true` in
config/agent.yaml. Nothing in the scheduler or the dashboard calls it on its own.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import yaml

from bot import run as bot_run
from bot.search import Budget
from collectors.common import config, db
from collectors.common.log import get_logger
from llm import client

from . import tools

log = get_logger("agent.loop")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "agent.yaml"
INSTRUCTIONS_PATH = Path(__file__).resolve().parent.parent / "config" / "agent.md"

AGENT_TASK = "agent"           # which model tier to use (task_models in config/reader.yaml)


class AgentDisabled(RuntimeError):
    """The agent was called while switched off."""


def load_agent_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_instructions(path: str | Path | None = None) -> str:
    p = Path(path) if path else INSTRUCTIONS_PATH
    return p.read_text(encoding="utf-8")


def is_enabled(cfg: dict) -> bool:
    """On switch: BLOOMFIELD_AGENT=1 in the environment, or enabled: true in config/agent.yaml."""
    return os.environ.get("BLOOMFIELD_AGENT", "").strip() == "1" or bool(cfg.get("enabled"))


def create_tables(con) -> None:
    tools.create_tables(con)


def _log_step(con, run_id: str, step: int, kind: str, name: str, detail: str) -> None:
    con.execute("INSERT INTO agent_steps (run_id, step, kind, name, detail, created_at) VALUES (?,?,?,?,?,?)",
                [run_id, step, kind, name, (detail or "")[:8000], db.utc_now()])


def run(con, goal: str, *, cfg: dict | None = None, reader_cfg: dict | None = None) -> dict:
    """
    Run the agent on one goal. Returns a summary dict: run_id, status, steps, evidence count,
    cost so far, and the agent's final plain-English answer.
    """
    cfg = cfg or load_agent_config()
    if not is_enabled(cfg):
        raise AgentDisabled(
            "The sourcing agent is off. Turn it on with BLOOMFIELD_AGENT=1 in pipeline/.env "
            "(or enabled: true in config/agent.yaml). It needs a working LLM key and spends money "
            "per run within the reader.yaml spending cap.")
    reader_cfg = reader_cfg or client.load_reader_config()
    instructions = load_instructions()
    max_steps = int(cfg.get("max_steps", 12))
    max_result_chars = int(cfg.get("max_tool_result_chars", 6000))
    max_output = int(cfg.get("max_output_tokens", 2000))

    bot_cfg = bot_run.load_config()                      # the search/bot settings from config/bot.yaml
    budget = Budget(bot_cfg)                             # shared page/query limits, same as the bot's
    model_name = client.model_for_task(AGENT_TASK, reader_cfg)

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    from scout_reader_schema import create_scout_reader_tables
    create_scout_reader_tables(con)                      # llm_usage / llm_cache: cost cap needs them on a fresh DB
    create_tables(con)
    con.execute("INSERT INTO agent_runs (run_id, goal, status, started_at, steps, llm_calls, cost_usd) "
                "VALUES (?,?,?,?,0,0,0.0)", [run_id, goal, "running", db.utc_now()])

    allowed = set(cfg.get("tools") or tools.TOOL_NAMES)
    schemas = [t for t in tools.TOOL_SCHEMAS if t["function"]["name"] in allowed]
    messages = [{"role": "user", "content": goal}]
    total_cost = 0.0
    llm_calls = 0
    status, final = "step_cap", ""
    try:
        for step in range(1, max_steps + 1):
            result = client.call_with_tools(AGENT_TASK, instructions, messages, schemas,
                                            max_output, con=con, cfg=reader_cfg)
            llm_calls += 1
            total_cost += result["cost_usd"] or 0.0
            messages.append(result["message"])
            if not result["tool_calls"]:
                status, final = "done", result["text"]
                _log_step(con, run_id, step, "note", "final_answer", final)
                break
            for call in result["tool_calls"]:
                _log_step(con, run_id, step, "tool_call", call["name"], json.dumps(call["arguments"]))
                out = tools.run_tool(con, run_id, call["name"], call["arguments"],
                                     bot_cfg=bot_cfg, budget=budget, model_name=model_name)
                _log_step(con, run_id, step, "tool_result", call["name"], out)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": out[:max_result_chars]})
        else:
            final = ("I ran out of steps before I could finish. Everything I verified is saved as "
                     "evidence rows for this run; anything I did not verify is not claimed.")
    except Exception:
        status = "failed"
        con.execute("UPDATE agent_runs SET status=?, finished_at=?, steps=?, llm_calls=?, cost_usd=? WHERE run_id=?",
                    [status, db.utc_now(), max_steps, llm_calls, total_cost, run_id])
        raise
    con.execute("UPDATE agent_runs SET status=?, finished_at=?, steps=?, llm_calls=?, cost_usd=?, final_answer=? "
                "WHERE run_id=?",
                [status, db.utc_now(), max_steps if status == "step_cap" else llm_calls, llm_calls, total_cost, final, run_id])
    ev_count = con.execute("SELECT count(*) FROM evidence WHERE run_id = ?", [run_id]).fetchone()[0]
    log.info("agent run %s: %s in %d llm calls, %d evidence rows, $%.4f",
             run_id, status, llm_calls, ev_count, total_cost)
    return {"run_id": run_id, "status": status, "llm_calls": llm_calls,
            "evidence_rows": ev_count, "cost_usd": round(total_cost, 6), "final_answer": final}
