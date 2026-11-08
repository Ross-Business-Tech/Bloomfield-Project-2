"""
store.py - Part D's own tables, and safe reading of the other parts' tables.

Part D only READS raw_files, recorded_loans, loans, bank_stats (Part A),
filing_fields, projects (Part B) and links (Part C, when it exists).
It WRITES only the tables below.
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd

PART_D_SCHEMA = """
-- One row per scoring run (normally once a week).
CREATE TABLE IF NOT EXISTS score_runs (
    run_id          TEXT PRIMARY KEY,
    run_date        DATE,
    created_at      TIMESTAMP,
    settings_json   TEXT,
    flag_count      INTEGER
);

-- One row per property per run. Parts of the dashboard read this.
CREATE TABLE IF NOT EXISTS flags (
    run_id            TEXT,
    property_key      TEXT,
    property_name     TEXT,
    address           TEXT,
    city              TEXT,
    state             TEXT,
    property_type     TEXT,
    sponsor           TEXT,
    lender            TEXT,
    loan_amount       DOUBLE,
    desperation       INTEGER,
    deal_quality      INTEGER,
    rank              INTEGER,       -- 1 = best; NULL when filtered out
    shown             BOOLEAN,       -- passes Bloomfield's filters
    filter_reason     TEXT,
    next_deadline     DATE,
    next_deadline_what TEXT,
    signals_json      TEXT,          -- list of signals with evidence and source quotes
    score_parts_json  TEXT,          -- how each score was built, for "why this score?"
    angles_json       TEXT,          -- suggested deal angles for Bloomfield
    summary           TEXT,
    PRIMARY KEY (run_id, property_key)
);

-- What the researcher did with each flag. Never overwritten: every action is a new row.
CREATE TABLE IF NOT EXISTS research_log (
    log_id          TEXT PRIMARY KEY,
    property_key    TEXT,
    property_name   TEXT,
    status          TEXT,      -- looked_into / called / meeting / passed / deal_closed
    note            TEXT,
    deal_amount     DOUBLE,    -- only for deal_closed
    researcher      TEXT,
    logged_at       TIMESTAMP
);
"""

RESEARCH_STATUSES = ["looked_into", "called", "meeting", "passed", "deal_closed"]
STATUS_LABELS = {
    "looked_into": "Looked into",
    "called": "Called",
    "meeting": "Meeting set",
    "passed": "Passed",
    "deal_closed": "Deal closed",
}


def default_db_path() -> Path:
    """Same file as Part A's collectors.common.config.db_path(): pipeline/data/bloomfield.duckdb."""
    if os.environ.get("BLOOMFIELD_DB"):
        return Path(os.environ["BLOOMFIELD_DB"])
    try:
        from collectors.common import config  # Part A's helper, when it is on the same branch

        return config.db_path()
    except Exception:
        return Path(__file__).resolve().parents[1] / "data" / "bloomfield.duckdb"


def connect(path: str | Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if str(path) == ":memory:":
        con = duckdb.connect(":memory:")
    else:
        db_file = Path(path) if path else default_db_path()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(db_file), read_only=read_only)
    if not read_only:
        con.execute(PART_D_SCHEMA)
    return con


def table_exists(con, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ? LIMIT 1", [table]
        ).fetchone()
    )


def read_table(con, table: str, columns: list[str]) -> pd.DataFrame:
    """Read the columns Part D needs. A missing table or column comes back empty / None instead of crashing."""
    if not table_exists(con, table):
        return pd.DataFrame(columns=columns)
    have = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]
    ).fetchall()}
    select = ", ".join(c if c in have else f"NULL AS {c}" for c in columns)
    return con.execute(f"SELECT {select} FROM {table}").df()
