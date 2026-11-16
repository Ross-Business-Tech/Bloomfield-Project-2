"""
serve.py - start the dashboard AND the optional weekly collector when the container boots.

Streamlit only runs app.py when a visitor opens a page, so a scheduler started from app.py
would sit idle after every restart until somebody visited. This launcher starts the scheduler
thread first, then runs the normal `streamlit run dashboard/app.py` in THE SAME PROCESS
(DuckDB allows only one read-write process; threads inside it are fine).

The scheduler is still OFF unless BLOOMFIELD_SCHEDULER=1. Use as the start command:
    python -m dashboard.serve
It reads BLOOMFIELD_DB (database path) and PORT, like the old command line did.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1]
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))


def main() -> None:
    from dashboard import scheduler

    scheduler.ensure_started(os.environ.get("BLOOMFIELD_DB") or None, source="boot")

    from dashboard import ingest_thread

    ingest_thread.ensure_started(os.environ.get("BLOOMFIELD_DB") or None, source="boot")

    from streamlit.web import cli

    sys.argv = [
        "streamlit", "run", str(PIPELINE / "dashboard" / "app.py"),
        "--server.port", os.environ.get("PORT", "8501"),
        "--server.address", "0.0.0.0",
        "--server.headless", "true",
    ]
    cli.main()


if __name__ == "__main__":
    main()
