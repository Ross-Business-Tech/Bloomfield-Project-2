"""
db.py - the shared DuckDB database.

In plain English: DuckDB is a single file (data/bloomfield.duckdb) that works like
a small spreadsheet-database. Parts A, B, C and D only talk to each other through
the tables created here, so the table and column names must not be changed
without telling the other parts.

Notes for the other parts:
  * All numbers are stored as DOUBLE (plain decimals) so pandas reads them easily.
  * file_path values are relative to the pipeline/ folder.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb

from . import config

# The allowed values for the "label" columns. Collectors check against these.
VALID_SOURCES = {
    "county_court", "recorder", "public_notice", "permit", "state_license",
    "cms", "sec_edgar", "ffiec", "trepp", "manual_upload",
    # added by Part B (the scout):
    "project_site", "news", "auction", "wsj_notice",
}
VALID_DOC_CATEGORIES = {
    "foreclosure_notice", "receivership", "lender_lawsuit", "lis_pendens",
    "recorded_loan", "mechanics_lien", "tax_delinquency", "building_permit",
    "certificate_of_occupancy", "license_action", "business_record", "other",
    # added by Part B (the scout):
    "project_website", "news_article", "auction_listing", "ucc_sale_notice",
}
VALID_LOAN_KINDS = {
    "construction", "permanent", "extension", "modification",
    "release", "assignment", "unknown",
}

# ---------------------------------------------------------------------------
# Table definitions (CREATE TABLE IF NOT EXISTS, so running setup twice is safe)
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
-- Every document or page we download. Part B reads this.
CREATE TABLE IF NOT EXISTS raw_files (
    file_id          TEXT PRIMARY KEY,
    source           TEXT,
    doc_category     TEXT,
    state            TEXT,
    county           TEXT,
    city             TEXT,
    url              TEXT,
    file_path        TEXT,
    file_type        TEXT,
    file_hash        TEXT,
    source_record_id TEXT,
    collected_at     TIMESTAMP,
    collector_name   TEXT,
    notes            TEXT
);

-- Index rows from county recorder searches. Part C builds loan histories from this.
CREATE TABLE IF NOT EXISTS recorded_loans (
    record_id          TEXT PRIMARY KEY,
    file_id            TEXT,
    state              TEXT,
    county             TEXT,
    instrument_number  TEXT,
    document_type      TEXT,
    loan_kind          TEXT,
    recording_date     DATE,
    grantor            TEXT,
    grantee            TEXT,
    amount             DOUBLE,
    parcel_number      TEXT,
    property_address   TEXT,
    legal_description  TEXT,
    related_instrument TEXT,
    collected_at       TIMESTAMP
);

-- Loan-level data from SEC EDGAR CMBS filings and the Trepp screen.
CREATE TABLE IF NOT EXISTS loans (
    loan_key             TEXT PRIMARY KEY,
    source               TEXT,
    deal_name            TEXT,
    property_name        TEXT,
    property_address     TEXT,
    property_city        TEXT,
    property_state       TEXT,
    property_zip         TEXT,
    property_type        TEXT,
    current_balance      DOUBLE,
    original_balance     DOUBLE,
    maturity_date        DATE,
    dscr                 DOUBLE,
    occupancy            DOUBLE,
    payment_status       TEXT,
    servicer_name        TEXT,
    watchlist_flag       BOOLEAN,
    servicer_commentary  TEXT,
    report_period        DATE,
    file_id              TEXT,
    collected_at         TIMESTAMP
);

-- One row per bank per quarter (FFIEC call reports).
CREATE TABLE IF NOT EXISTS bank_stats (
    rssd_id                TEXT,
    bank_name              TEXT,
    city                   TEXT,
    state                  TEXT,
    report_date            DATE,
    cre_construction       DOUBLE,
    cre_multifamily        DOUBLE,
    cre_nonfarm_nonres     DOUBLE,
    cre_total              DOUBLE,
    cre_past_due_30_89     DOUBLE,
    cre_past_due_90_plus   DOUBLE,
    cre_nonaccrual         DOUBLE,
    cre_nonaccrual_ratio   DOUBLE,
    nonaccrual_change_qoq  DOUBLE,
    rising_two_quarters    BOOLEAN,
    PRIMARY KEY (rssd_id, report_date)
);

-- A log of every collector run, so we can see what happened.
-- status is ok / partial / failed (or "running" while a run is in progress or was killed).
CREATE TABLE IF NOT EXISTS collector_runs (
    run_id          TEXT,
    collector_name  TEXT,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    status          TEXT,
    files_new       INTEGER,
    files_skipped   INTEGER,
    errors          INTEGER,
    message         TEXT
);

-- Bookmarks so a collector that stopped halfway can pick up where it left off.
CREATE TABLE IF NOT EXISTS collector_state (
    collector_name  TEXT,
    state_key       TEXT,
    state_value     TEXT,
    updated_at      TIMESTAMP,
    PRIMARY KEY (collector_name, state_key)
);

-- Every U.S. county, and whether anyone has set up collection for it yet.
-- config_status: configured / manual_inbox / not_started
CREATE TABLE IF NOT EXISTS county_registry (
    state          TEXT,
    county         TEXT,
    fips           TEXT PRIMARY KEY,
    config_status  TEXT,
    priority       INTEGER,
    notes          TEXT
);

-- Part C writes names it wants looked up in state business registries; state.py answers.
-- status: pending / done / failed
CREATE TABLE IF NOT EXISTS entity_lookup_requests (
    entity_name   TEXT,
    state         TEXT,
    requested_by  TEXT,
    requested_at  TIMESTAMP,
    status        TEXT
);
"""

KNOWN_TABLES = {
    "raw_files", "recorded_loans", "loans", "bank_stats", "collector_runs",
    "collector_state", "county_registry", "entity_lookup_requests",
}


def utc_now() -> datetime:
    """Current time in UTC, without a timezone tag (what DuckDB TIMESTAMP stores)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create every table if it does not exist yet."""
    con.execute(SCHEMA_SQL)


def connect(path: str | Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """
    Open the database (creating the file and folders if needed) and make sure
    every table exists. Pass ":memory:" for a throwaway database in tests.
    """
    if str(path) == ":memory:":
        con = duckdb.connect(":memory:")
    else:
        db_file = Path(path) if path else config.db_path()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(db_file), read_only=read_only)
    if not read_only:
        create_tables(con)
    return con


# ---------------------------------------------------------------------------
# Insert helpers that skip duplicates
# ---------------------------------------------------------------------------

def _columns(con, table: str) -> list[str]:
    if table not in KNOWN_TABLES:
        raise ValueError(f"Unknown table: {table}")
    return [r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]


def insert_row(con, table: str, row: dict, replace: bool = False) -> None:
    """
    Insert one row (a dict of column -> value). Columns you leave out become NULL.
    With replace=True an existing row with the same primary key is overwritten.
    """
    cols = _columns(con, table)
    unknown = set(row) - set(cols)
    if unknown:
        raise ValueError(f"{table} has no column(s): {sorted(unknown)}")
    names = [c for c in cols if c in row]
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    sql = f"{verb} INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})"
    con.execute(sql, [row[c] for c in names])


def insert_if_new(con, table: str, row: dict, key_cols: list[str]) -> bool:
    """
    Insert the row only if no row with the same key columns exists.
    Returns True if inserted, False if it was a duplicate and skipped.
    """
    where = " AND ".join(f"{c} = ?" for c in key_cols)
    found = con.execute(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1",
                        [row[c] for c in key_cols]).fetchone()
    if found:
        return False
    insert_row(con, table, row)
    return True


def find_file_by_hash(con, file_hash: str) -> tuple[str, str] | None:
    """If we already stored a file with this fingerprint, return (file_id, file_path)."""
    return con.execute(
        "SELECT file_id, file_path FROM raw_files WHERE file_hash = ? LIMIT 1", [file_hash]
    ).fetchone()


# ---------------------------------------------------------------------------
# Bookmarks (so collectors can restart where they stopped)
# ---------------------------------------------------------------------------

def get_state(con, collector_name: str, key: str) -> str | None:
    row = con.execute(
        "SELECT state_value FROM collector_state WHERE collector_name = ? AND state_key = ?",
        [collector_name, key],
    ).fetchone()
    return row[0] if row else None


def set_state(con, collector_name: str, key: str, value: str) -> None:
    insert_row(con, "collector_state", {
        "collector_name": collector_name, "state_key": key,
        "state_value": value, "updated_at": utc_now(),
    }, replace=True)
