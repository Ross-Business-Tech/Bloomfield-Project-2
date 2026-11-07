"""
log.py - logging, plus the run log written to the collector_runs table.

In plain English: every collector wraps its work in a RunTracker. The tracker
counts new files, skipped duplicates and errors, and when the collector ends
(even if it crashes) it writes one summary row to collector_runs. A crash is
logged and swallowed so the NEXT collector still runs (design rule 6).
"""
from __future__ import annotations

import logging
import logging.handlers
import traceback
import uuid

from . import config
from .db import insert_row, utc_now

_configured = False


def setup_logging(level: int = logging.INFO, to_file: bool = True) -> None:
    """Send log messages to the screen and to data/logs/pipeline.log."""
    global _configured
    if _configured:
        return
    root = logging.getLogger("bloomfield")
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    if to_file:
        try:
            config.logs_dir().mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                config.logs_dir() / "pipeline.log", maxBytes=2_000_000, backupCount=3,
                encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            pass  # a read-only disk should not stop collection
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"bloomfield.{name}")


class RunTracker:
    """
    Usage:
        with RunTracker(con, "county.py:washtenaw") as run:
            ...                      # do the work
            run.new_file()           # a new file was saved
            run.skipped()            # a duplicate was skipped
            run.error("what broke")  # a problem we recovered from
            run.manual_inbox("washtenaw court", "site blocks robots")

    Status written at the end:
        ok       - no errors
        partial  - some errors, but some work succeeded
        failed   - the collector crashed, or every attempt errored
    """

    def __init__(self, con, collector_name: str, suppress_exceptions: bool = True):
        self.con = con
        self.collector_name = collector_name
        self.suppress = suppress_exceptions
        self.files_new = 0
        self.files_skipped = 0
        self.errors: list[str] = []
        self.fallbacks: list[tuple[str, str]] = []
        self.status = "running"
        self.log = get_logger(collector_name)
        self.run_id = ""

    def __enter__(self) -> "RunTracker":
        self.run_id = f"run-{utc_now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
        self.started_at = utc_now()
        # Written at the start so a hard crash still leaves a trace ("running").
        insert_row(self.con, "collector_runs", {
            "run_id": self.run_id, "collector_name": self.collector_name,
            "started_at": self.started_at, "status": "running",
            "files_new": 0, "files_skipped": 0, "errors": 0,
        })
        self.log.info("started")
        return self

    # --- counters the collector calls ---
    def new_file(self, n: int = 1) -> None:
        self.files_new += n

    def skipped(self, n: int = 1) -> None:
        self.files_skipped += n

    def error(self, message: str) -> None:
        self.errors.append(message)
        self.log.error(message)

    def manual_inbox(self, source: str, reason: str) -> None:
        """Record that a source fell back to the manual inbox, and why."""
        self.fallbacks.append((source, reason))
        self.log.warning("manual inbox mode for %s: %s", source, reason)

    # --- finish ---
    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.errors.append(f"CRASH: {exc_type.__name__}: {exc}")
            self.log.error("crashed:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
            self.status = "failed"
        elif self.errors:
            self.status = "partial" if (self.files_new + self.files_skipped) > 0 else "failed"
        else:
            self.status = "ok"

        parts = list(self.errors[:5])
        if len(self.errors) > 5:
            parts.append(f"...and {len(self.errors) - 5} more errors")
        parts += [f"manual inbox: {s} ({r})" for s, r in self.fallbacks]
        self.con.execute(
            "UPDATE collector_runs SET finished_at = ?, status = ?, files_new = ?, "
            "files_skipped = ?, errors = ?, message = ? WHERE run_id = ?",
            [utc_now(), self.status, self.files_new, self.files_skipped,
             len(self.errors), " | ".join(parts) or None, self.run_id],
        )
        self.log.info("finished: %s (%d new, %d skipped, %d errors)",
                      self.status, self.files_new, self.files_skipped, len(self.errors))
        return self.suppress  # True = swallow the crash so other collectors still run
