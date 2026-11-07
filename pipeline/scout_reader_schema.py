"""
scout_reader_schema.py - the database tables owned by Part B (the Scout and the Reader).

Part A's scripts/setup_db.py calls create_scout_reader_tables(). Any Part B code that
needs these tables calls open_db() instead of collectors.common.db.connect().

Numbers are stored as DOUBLE (like Part A). JSON columns hold lists/dicts as JSON text.
"""
from __future__ import annotations

from collectors.common import db

SCHEMA_SQL = """
-- One row per document the Reader has read. Parts C and D read this.
CREATE TABLE IF NOT EXISTS filing_fields (
    file_id                 TEXT PRIMARY KEY,      -- links to raw_files
    filing_type             TEXT,
    borrower_name           TEXT,
    developer_name          TEXT,
    project_name            TEXT,
    other_parties           JSON,                  -- list of {name, role}
    property_address        TEXT,
    property_city           TEXT,
    property_state          TEXT,
    property_zip            TEXT,
    parcel_number           TEXT,
    property_type           TEXT,
    lender_name             TEXT,
    servicer_name           TEXT,
    loan_kind               TEXT,
    loan_amount             DOUBLE,
    amount_owed             DOUBLE,
    recording_date          DATE,
    maturity_date           DATE,
    filing_date             DATE,
    sale_date               DATE,
    auction_date            DATE,
    lien_claimant           TEXT,
    lien_amount             DOUBLE,
    lien_work               TEXT,
    permit_number           TEXT,
    permit_status           TEXT,
    permit_expiration_date  DATE,
    stated_opening_phrase   TEXT,
    page_still_unopened     BOOLEAN,
    leasing_season          TEXT,
    starting_bid            DOUBLE,
    case_number             TEXT,
    distress_tags           JSON,
    quotes                  JSON,                  -- {field_name: exact source quote}
    confidence              TEXT,                  -- high / medium / low
    status                  TEXT,                  -- ok / needs_review / failed
    review_reason           TEXT,
    model_name              TEXT,
    prompt_version          TEXT,
    extracted_at            TIMESTAMP,
    input_tokens            INTEGER,
    output_tokens           INTEGER,
    est_cost_usd            DOUBLE
);

-- One row per announced development the Scout is tracking.
CREATE TABLE IF NOT EXISTS projects (
    project_id              TEXT PRIMARY KEY,
    project_name            TEXT,
    developer_name          TEXT,
    property_address        TEXT,
    city                    TEXT,
    state                   TEXT,
    county                  TEXT,
    property_type           TEXT,
    units_or_beds           INTEGER,
    main_url                TEXT,                  -- the project or leasing website
    first_seen_at           TIMESTAMP,
    stated_opening_phrase   TEXT,                  -- latest phrase, exactly as written
    opening_deadline        DATE,                  -- latest reasonable date that phrase could mean
    page_still_unopened     BOOLEAN,
    stale_candidate         BOOLEAN,               -- deadline passed and page still says coming soon
    months_overdue          DOUBLE,
    missed_lease_up         BOOLEAN,               -- student housing that missed its August move-in
    date_change_count       INTEGER,               -- how many times the stated date has changed
    first_stated_phrase     TEXT,                  -- the earliest phrase we ever saw
    last_checked_at         TIMESTAMP,
    check_status            TEXT,                  -- ok / unreachable / blocked / manual
    notes                   TEXT
);

-- Every time we check a project page (live or from the Wayback Machine).
CREATE TABLE IF NOT EXISTS project_snapshots (
    snapshot_id             TEXT PRIMARY KEY,
    project_id              TEXT,
    checked_at              TIMESTAMP,
    source                  TEXT,                  -- live or wayback
    snapshot_date           DATE,                  -- for wayback, the archive date
    url                     TEXT,
    file_id                 TEXT,                  -- the saved page in raw_files
    content_hash            TEXT,
    stated_opening_phrase   TEXT,
    page_still_unopened     BOOLEAN
);

-- Everything the Scout finds, before or after reading.
CREATE TABLE IF NOT EXISTS scout_hits (
    hit_id                  TEXT PRIMARY KEY,
    source_name             TEXT,                  -- from scout_sources.yaml
    url                     TEXT,
    title                   TEXT,
    found_at                TIMESTAMP,
    state                   TEXT,
    city                    TEXT,
    relevance               TEXT,                  -- relevant / not_relevant / unsure
    relevance_reason        TEXT,
    file_id                 TEXT,                  -- set once saved to raw_files
    status                  TEXT                   -- new / saved / skipped / blocked
);

-- Websites the Scout noticed but is NOT allowed to visit. A person reviews these.
CREATE TABLE IF NOT EXISTS suggested_sources (
    domain                  TEXT PRIMARY KEY,
    example_url             TEXT,
    reason                  TEXT,
    first_seen_at           TIMESTAMP,
    times_seen              INTEGER,
    status                  TEXT                   -- pending / approved / rejected
);

-- Saved AI answers, so we never pay to read the same thing twice.
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key               TEXT PRIMARY KEY,      -- hash of (file_hash, prompt_version, model, task)
    task                    TEXT,                  -- extract / relevance / summary / opening_phrase
    response_json           TEXT,
    created_at              TIMESTAMP
);

-- Extra table (not in the original spec): one row per REAL AI call, used for the cost cap.
CREATE TABLE IF NOT EXISTS llm_usage (
    called_at               TIMESTAMP,
    task                    TEXT,
    provider                TEXT,
    model                   TEXT,
    input_tokens            INTEGER,
    output_tokens           INTEGER,
    est_cost_usd            DOUBLE,
    file_hash               TEXT
);
"""

TABLES = ["filing_fields", "projects", "project_snapshots", "scout_hits",
          "suggested_sources", "llm_cache", "llm_usage"]


def create_scout_reader_tables(con) -> None:
    """Create Part B's tables if they do not exist yet (safe to run any time)."""
    con.execute(SCHEMA_SQL)


def open_db(path=None, read_only: bool = False):
    """Open the shared database with BOTH Part A's and Part B's tables ready."""
    con = db.connect(path, read_only=read_only)
    if not read_only:
        create_scout_reader_tables(con)
    return con


def upsert(con, table: str, row: dict, replace: bool = True) -> None:
    """Insert one row (dict of column -> value) into a Part B table; replaces on same key."""
    if table not in TABLES:
        raise ValueError(f"Not a Part B table: {table}")
    cols = [r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
    unknown = set(row) - set(cols)
    if unknown:
        raise ValueError(f"{table} has no column(s): {sorted(unknown)}")
    names = [c for c in cols if c in row]
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    con.execute(f"{verb} INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                [row[c] for c in names])
