"""
schema.py - the database tables owned by Part C (the Matcher).

In plain English: Part C reads Part A's and Part B's tables and writes down which records
are about the same thing:
  * properties / property_links  - which records are about the same building
  * loan_histories               - each recorded loan's story: modified? refinanced? paid off?
  * loan_links                   - the same CMBS loan reported by EDGAR and by Trepp
  * lender_links                 - lender names -> the bank's FFIEC ID (bank_stats.rssd_id)
  * entities / entity_links      - borrowers, developers, contractors... with spelling variants merged
  * match_review                 - "maybe" matches a person should confirm or reject
It also adds rows to Part A's entity_lookup_requests (state business-registry lookups).

Every run rebuilds these tables from scratch, except match_review decisions people made.
"""
from __future__ import annotations

from scout_reader_schema import open_db as _open_ab_db

SCHEMA_SQL = """
-- One row per real-world property.
CREATE TABLE IF NOT EXISTS properties (
    property_id       TEXT PRIMARY KEY,
    property_name     TEXT,
    street_address    TEXT,
    city              TEXT,
    state             TEXT,
    zip               TEXT,
    county            TEXT,
    parcel_number     TEXT,
    property_type     TEXT,
    record_count      INTEGER,
    sources           TEXT,          -- e.g. "filing_fields, loans, recorded_loans"
    matched_at        TIMESTAMP
);

-- Which record belongs to which property (Part D reads this).
CREATE TABLE IF NOT EXISTS property_links (
    property_id       TEXT,
    record_table      TEXT,          -- recorded_loans / filing_fields / loans / projects
    record_id         TEXT,          -- that table's key
    match_method      TEXT,          -- parcel / address / fuzzy_address / property_name / manual / alone
    match_score       DOUBLE,        -- 100 = exact
    matched_at        TIMESTAMP,
    PRIMARY KEY (record_table, record_id)
);

-- One row per original recorded loan (construction or permanent mortgage) and what happened to it.
CREATE TABLE IF NOT EXISTS loan_histories (
    history_id            TEXT PRIMARY KEY,   -- = the original loan's record_id
    property_id           TEXT,
    instrument_number     TEXT,
    loan_kind             TEXT,               -- construction / permanent / unknown
    recording_date        DATE,
    amount                DOUBLE,
    borrower_name         TEXT,
    lender_name           TEXT,
    lender_rssd_id        TEXT,
    modification_count    INTEGER,            -- modifications + extensions recorded after it
    last_modification_date DATE,
    assignment_count      INTEGER,            -- the loan/note was sold or assigned
    takeout_record_id     TEXT,               -- the permanent loan that replaced it (construction only)
    takeout_date          DATE,
    release_record_id     TEXT,               -- the discharge/satisfaction
    release_date          DATE,
    status                TEXT,               -- open / refinanced / released
    months_open           DOUBLE,             -- recording to payoff, or to today if still open
    record_ids            TEXT,               -- every recorded_loans row in this story, in order
    built_at              TIMESTAMP
);

-- The same CMBS loan in more than one source (EDGAR and Trepp give it different keys).
CREATE TABLE IF NOT EXISTS loan_links (
    loan_key          TEXT PRIMARY KEY,
    loan_group_id     TEXT,             -- shared by every report of the same loan
    property_id       TEXT,
    match_method      TEXT,             -- alone / same_deal_and_property / same_property_balance
    matched_at        TIMESTAMP
);

-- Lender names as written in records -> the bank in bank_stats.
CREATE TABLE IF NOT EXISTS lender_links (
    lender_name       TEXT PRIMARY KEY, -- exactly as written in the record
    lender_key        TEXT,
    rssd_id           TEXT,             -- NULL = no confident match
    bank_name         TEXT,
    match_score       DOUBLE,
    match_method      TEXT,             -- exact / fuzzy / manual / none
    times_seen        INTEGER,
    matched_at        TIMESTAMP
);

-- Borrowers, developers, contractors, lenders... with spelling variants merged.
CREATE TABLE IF NOT EXISTS entities (
    entity_id         TEXT PRIMARY KEY,
    canonical_name    TEXT,             -- the most common spelling
    entity_key        TEXT,
    entity_kind       TEXT,             -- llc / corporation / partnership / trust / bank / other
    roles             TEXT,             -- e.g. "borrower, developer"
    states            TEXT,
    property_count    INTEGER,
    name_variants     TEXT,             -- JSON list of spellings seen
    matched_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entity_links (
    entity_id         TEXT,
    record_table      TEXT,
    record_id         TEXT,
    role              TEXT,             -- borrower / developer / lender / lien_claimant / party ...
    raw_name          TEXT,
    property_id       TEXT,
    matched_at        TIMESTAMP
);

-- Matches the matcher wasn't sure about. A person sets decision = yes / no; the next run obeys it.
CREATE TABLE IF NOT EXISTS match_review (
    review_id         TEXT PRIMARY KEY,
    kind              TEXT,             -- property / lender / entity
    left_ref          TEXT,             -- e.g. "loans:edgar:..."  or a name
    right_ref         TEXT,             -- e.g. a property_id, an rssd_id, an entity_id
    left_label        TEXT,
    right_label       TEXT,
    score             DOUBLE,
    reason            TEXT,
    decision          TEXT,             -- NULL (waiting) / yes / no
    first_seen_at     TIMESTAMP,
    decided_at        TIMESTAMP
);
"""

TABLES = ["properties", "property_links", "loan_histories", "loan_links", "lender_links",
          "entities", "entity_links", "match_review"]
REBUILT_EACH_RUN = [t for t in TABLES if t != "match_review"]


def create_matcher_tables(con) -> None:
    """Create Part C's tables if they do not exist yet (safe to run any time)."""
    con.execute(SCHEMA_SQL)


def open_db(path=None, read_only: bool = False):
    """Open the shared database with Part A's, Part B's and Part C's tables ready."""
    con = _open_ab_db(path, read_only=read_only)
    if not read_only:
        create_matcher_tables(con)
    return con


def table_exists(con, name: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]).fetchone())


def rows(con, sql: str, params=None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
