"""
scheduler.py - the optional weekly collection run, inside the dashboard's own process.

Why a thread here and not a second service or a cron job next to Streamlit:
DuckDB lets exactly ONE process have the database open for reading and writing.
Many threads inside that one process are fine (appends never conflict). A second
process - another Railway service, or a cron job beside this one - loses the
database file lock and fails. So the weekly run lives in the process that
already owns the database: this one.

OFF BY DEFAULT. Set BLOOMFIELD_SCHEDULER=1 to turn it on. Keep it off until the
team has settled source permissions (the NETR approval is supposed to be kept on
file) and the rules for showing real records in the app.

Settings (environment variables):
  BLOOMFIELD_SCHEDULER=1                     turn it on (anything else = off)
  BLOOMFIELD_SCHEDULER_INTERVAL_HOURS=168    time between runs (default: weekly)
  BLOOMFIELD_SCHEDULER_FIRST_DELAY_SECONDS   wait before the first run (default 3600)
  BLOOMFIELD_SCHEDULER_ONLY=inbox,edgar      optional: run only these collectors

Every cycle runs the whole weekly workflow, in the same order as doing it by hand:
collectors (collectors.run_all) -> AI reader (reader.batch) -> matcher -> weekly
scoring run (scorer.weekly), so the dashboard picks up the new week on its next
page load. The reader uses the configured LLM key and the repo's spending caps;
if no key is configured it is skipped with a warning and the rest of the cycle
still runs. Results are in collector_runs / weekly_runs, same as a manual run.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

PIPELINE = Path(__file__).resolve().parents[1]
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

from collectors import run_all  # noqa: E402
from collectors.common.log import get_logger  # noqa: E402
from scorer import weekly  # noqa: E402
from scorer.schema import open_db  # noqa: E402

log = get_logger("scheduler")

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_status: dict = {
    "enabled": False,      # BLOOMFIELD_SCHEDULER is set
    "started": False,      # the background thread is running
    "cycles": 0,           # weekly runs attempted
    "last_run_at": None,   # ISO time of the last attempt
    "last_ok": None,       # did the last cycle succeed
    "last_error": None,    # error text when it did not
    "last_run_id": None,   # weekly_runs run_id from the last good cycle
}


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return _truthy(os.environ.get("BLOOMFIELD_SCHEDULER"))


def _interval_seconds() -> float:
    return float(os.environ.get("BLOOMFIELD_SCHEDULER_INTERVAL_HOURS", "168")) * 3600


def _first_delay_seconds() -> float:
    return float(os.environ.get("BLOOMFIELD_SCHEDULER_FIRST_DELAY_SECONDS", "3600"))


def _only() -> list[str] | None:
    raw = os.environ.get("BLOOMFIELD_SCHEDULER_ONLY", "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()] or None


def status() -> dict:
    """A copy of the scheduler's current status (for tests and diagnostics)."""
    with _lock:
        return dict(_status)


def stop() -> None:
    """Ask the background thread to stop and reset status (used by tests; the app never calls this)."""
    global _thread
    _stop.set()
    thread = _thread
    if thread and thread.is_alive():
        thread.join(timeout=10)
    with _lock:
        _thread = None
        _status.update(enabled=False, started=False, cycles=0, last_run_at=None,
                       last_ok=None, last_error=None, last_run_id=None)


def with_catalog_retry(fn, tries: int = 30, wait_seconds: float = 1.0):
    """
    Run fn(), retrying DuckDB catalog write-write conflicts. Two threads in this
    process (dashboard + scheduler) can both run CREATE TABLE IF NOT EXISTS at
    the same moment; DuckDB's optimistic concurrency fails one of them and its
    docs say to rerun the transaction.
    """
    for attempt in range(tries):
        try:
            return fn()
        except duckdb.TransactionException as e:
            if "Catalog write-write conflict" not in str(e) or attempt == tries - 1:
                raise
            time.sleep(wait_seconds)


def _say(msg: str) -> None:
    """One timestamped line on stdout so the host log shows what the scheduler did and when."""
    print(f"[scheduler {datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def ensure_started(db_path: str | None = None, source: str = "page") -> dict:
    """
    Start the weekly collector thread if BLOOMFIELD_SCHEDULER=1 and it is not
    already running. Safe to call on every Streamlit rerun. db_path is the
    dashboard's database (None = the default data/bloomfield.duckdb).
    """
    global _thread
    with _lock:
        _status["enabled"] = enabled()
        if not _status["enabled"]:
            return dict(_status)
        if _thread and _thread.is_alive():
            _status["started"] = True
            return dict(_status)
        _stop.clear()
        _status["started"] = True
        _say(f"starting weekly collector thread (trigger: {source}; first run in {_first_delay_seconds():.0f}s; collectors: {_only() or 'all'})")
        _thread = threading.Thread(target=_loop, args=(db_path,),
                                   name="bloomfield-weekly-collector", daemon=True)
        _thread.start()
        log.info("weekly collector started: every %.0f h, first run in %.0f s, collectors: %s",
                 _interval_seconds() / 3600, _first_delay_seconds(), _only() or "all")
        return dict(_status)


def run_cycle(db_path: str | None = None) -> dict:
    """
    One weekly cycle: collectors, then the scoring run. Opens its own connection
    to the same database the dashboard uses - safe because this is a thread in
    the dashboard's process, not a second process.
    """
    con = with_catalog_retry(lambda: open_db(db_path))
    try:
        from matcher.schema import create_matcher_tables
        with_catalog_retry(lambda: weekly.create_tables(con))
        with_catalog_retry(lambda: create_matcher_tables(con))

        results = run_all.run_all(con, only=_only())
        errors = sum(r["errors"] for r in results)

        # AI reader: pulls the facts out of whatever the collectors just stored.
        # Skipped (not fatal) when no LLM key is configured or the step breaks.
        reader_summary: dict = {}
        try:
            from reader import batch as reader_batch
            reader_summary = reader_batch.run(con=con, quiet=True)
            log.info("reader: %d read ok, %d need review, $%.4f",
                     reader_summary.get("ok", 0), reader_summary.get("needs_review", 0),
                     reader_summary.get("cost_usd", 0.0))
        except Exception as e:
            log.warning("reader step skipped this cycle: %s", e)
            reader_summary = {"skipped": str(e)}

        from matcher.run import run_matcher
        matcher_summary = run_matcher(con)
        log.info("matcher: %d records into %d properties",
                 matcher_summary.get("records_linked", 0), matcher_summary.get("properties", 0))

        run_id = weekly.run(con, None, date.today())

        # Sourcing agent: one bounded research pass over every scored property plus the
        # unscored long tail, then a re-score so the ranking reflects the new evidence.
        # Skipped (not fatal) when the agent is off or the step breaks; the pass enforces
        # its own shape bounds from config/agent.yaml weekly_loop.
        agent_summary: dict = {}
        try:
            from agent import loop as agent_loop
            from agent import weekly as agent_weekly
            acfg = agent_loop.load_agent_config()
            if agent_loop.is_enabled(acfg):
                _say("agent research pass start")
                agent_summary = agent_weekly.run_pass(con, scorer_run=weekly.run)
                _say(f"agent research pass done: {agent_summary.get('status')}, "
                     f"{agent_summary.get('goals_run')} goals, "
                     f"+{agent_summary.get('evidence_new')} evidence, "
                     f"${agent_summary.get('est_cost_usd', 0):.2f} est")
            else:
                agent_summary = {"skipped": "agent off (BLOOMFIELD_AGENT / config/agent.yaml)"}
        except Exception as e:
            log.warning("agent step skipped this cycle: %s", e)
            _say(f"agent research pass skipped: {e}")
            agent_summary = {"skipped": str(e)}

        log.info("weekly cycle done: %d collector error(s), scoring run %s", errors, run_id)
        return {"collector_results": results, "errors": errors, "run_id": run_id,
                "reader": reader_summary, "matcher": matcher_summary, "agent": agent_summary}
    finally:
        con.close()


def _loop(db_path: str | None) -> None:
    if _stop.wait(_first_delay_seconds()):
        return
    while not _stop.is_set():
        try:
            _say("cycle start")
            out = run_cycle(db_path)
            _say(f"cycle done: run {out['run_id']}, {out['errors']} collector error(s), "
                 f"{out['matcher'].get('records_linked', 0)} records into {out['matcher'].get('properties', 0)} properties")
            with _lock:
                _status.update(cycles=_status["cycles"] + 1,
                               last_run_at=datetime.now(timezone.utc).isoformat(),
                               last_ok=True, last_error=None, last_run_id=out["run_id"])
        except Exception as e:                       # a bad week must not kill the dashboard
            log.exception("weekly cycle failed")
            _say(f"cycle FAILED: {e}")
            with _lock:
                _status.update(cycles=_status["cycles"] + 1,
                               last_run_at=datetime.now(timezone.utc).isoformat(),
                               last_ok=False, last_error=str(e))
        if _stop.wait(_interval_seconds()):
            return
