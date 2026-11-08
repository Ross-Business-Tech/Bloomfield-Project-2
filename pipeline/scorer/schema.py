"""
schema.py - the database tables owned by Part D (the Scorer).

In plain English: the scorer writes two tables.
  * property_scores  - one row per property: its score, tier, and a one-line "why".
  * score_signals    - every reason behind a score, with points and the evidence file.

It READS (never writes) Part A's tables (recorded_loans, loans, bank_stats, raw_files),
Part B's tables (filing_fields, projects) and, when it exists, Part C's property_links.
"""
from __future__ import annotations

from scout_reader_schema import open_db as _open_ab_db

SCHEMA_SQL = """
-- One row per property, newest scoring run only. The ranked list Bloomfield looks at.
CREATE TABLE IF NOT EXISTS property_scores (
    property_id      TEXT PRIMARY KEY,
    property_name    TEXT,
    property_address TEXT,
    city             TEXT,
    state            TEXT,
    county           TEXT,
    parcel_number    TEXT,
    borrower_name    TEXT,
    lender_name      TEXT,
    loan_amount      DOUBLE,          -- largest loan we know of on the property
    score            DOUBLE,          -- 0 to 100
    tier             TEXT,            -- hot / watch / low
    signal_count     INTEGER,
    summary          TEXT,            -- plain-English "why", strongest reasons first
    grouped_by       TEXT,            -- part_c (Part C's links) or fallback (parcel/address)
    as_of            DATE,
    scoring_version  TEXT,
    scored_at        TIMESTAMP
);

-- Every reason behind every score (the evidence trail).
CREATE TABLE IF NOT EXISTS score_signals (
    property_id      TEXT,
    signal           TEXT,            -- name from config/scoring.yaml
    family           TEXT,
    points           DOUBLE,          -- after recency discount, before the family cap
    counted_points   DOUBLE,          -- what actually counted after the family cap
    detail           TEXT,            -- plain-English sentence
    evidence_table   TEXT,            -- where the evidence row lives
    evidence_id      TEXT,            -- its key (file_id, record_id, loan_key, rssd_id...)
    evidence_date    DATE,
    quote            TEXT,            -- exact source quote when the Reader has one
    scoring_version  TEXT,
    scored_at        TIMESTAMP
);
"""

TABLES = ["property_scores", "score_signals"]


def create_scorer_tables(con) -> None:
    """Create Part D's tables if they do not exist yet (safe to run any time)."""
    con.execute(SCHEMA_SQL)


def open_db(path=None, read_only: bool = False):
    """Open the shared database with Part A's, Part B's and Part D's tables ready."""
    con = _open_ab_db(path, read_only=read_only)
    if not read_only:
        create_scorer_tables(con)
    return con


def table_exists(con, name: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]).fetchone())
