"""
ingest_thread.py - poll the deployment volume for direct-research evidence batches.

Separate from the weekly scheduler (BLOOMFIELD_SCHEDULER): this thread never calls an
LLM and never uses the paid search service. It watches the incoming/ directory next to
the database for JSONL batches uploaded through the token-gated console in app.py,
verifies every row through agent/ingest.py (fetch the cited page, verbatim-quote check,
same record_evidence standard as the research agent), stores what passes, and re-runs
the deterministic scorer so new scorer-consumable evidence moves the rankings.

  BLOOMFIELD_INGEST=1                  turn it on (anything else = off)
  BLOOMFIELD_INGEST_INTERVAL_SECONDS   poll interval (default 60)
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from collectors.common.log import get_logger

log = get_logger("dashboard.ingest")

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()

_status = {"enabled": False, "started": False, "sweeps": 0, "last_sweep_at": None,
           "last_totals": None, "last_error": None}


def enabled() -> bool:
    return os.environ.get("BLOOMFIELD_INGEST") == "1"


def _interval() -> float:
    return float(os.environ.get("BLOOMFIELD_INGEST_INTERVAL_SECONDS", "60"))


def _say(msg: str) -> None:
    print(f"[ingest {datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def status() -> dict:
    return dict(_status)


def _loop(db_path: str | None) -> None:
    from agent import ingest

    _say(f"ingest thread started (dir: {ingest.incoming_dir(db_path)}, every {_interval():.0f}s)")
    while not _stop.is_set():
        try:
            totals = ingest.sweep(db_path)
            _status["sweeps"] += 1
            _status["last_sweep_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _status["last_totals"] = totals
            _status["last_error"] = None
            if totals.get("files"):
                _say(f"sweep: {totals}")
        except Exception as e:
            _status["last_error"] = str(e)[:300]
            _say(f"sweep failed: {e}")
            log.exception("ingest sweep failed")
        _stop.wait(_interval())
    _say("ingest thread stopped")


def ensure_started(db_path: str | None = None, source: str = "boot") -> dict:
    """Start the ingest thread if BLOOMFIELD_INGEST=1 and it is not already running."""
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
        _say(f"starting (trigger: {source})")
        _thread = threading.Thread(target=_loop, args=(db_path,),
                                   name="bloomfield-ingest", daemon=True)
        _thread.start()
        return dict(_status)
