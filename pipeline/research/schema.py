"""
schema.py - the database tables owned by the research layer (pipeline/research/).

In plain English: the research layer is the careful-analyst step between collecting
public records and trusting what they say. It writes four tables:

  * research_evidence   - one row per fact, with where it came from and exactly what it covers
  * research_searches   - one row per lookup we ran (even the ones that found nothing)
  * research_conflicts  - one row per place where two sources disagree
  * research_status     - one row per property: how deep the research really is today

It READS (never writes) Part A's raw_files and licensing_records, and it never touches
the scorer's tables or the Opportunity Score equation.
"""
from __future__ import annotations

from scorer.schema import open_db as _open_abd_db

SCHEMA_SQL = """
-- Every fact the research layer stores, always with its provenance.
CREATE TABLE IF NOT EXISTS research_evidence (
    evidence_id      TEXT PRIMARY KEY,
    property_key     TEXT,          -- our universe key for the property; NULL while only source-matched
    claim_type       TEXT,          -- see research.evidence.CLAIM_TYPES
    direction        TEXT,          -- distress / counter / neutral (missing data is never "counter")
    source_name      TEXT,          -- cms_provider_info / cms_penalties / cms_ownership / cms_snf_cost_report / county_tax / ...
    source_url       TEXT,          -- the page or file the fact was actually read from
    dataset_vintage  TEXT,          -- dataset + its as-of/vintage, e.g. "cms_penalties (processing 2026-10-01)"
    as_of            DATE,          -- the single date the fact describes, when there is one
    identity_method  TEXT,          -- ccn / parcel / address_county_spelling / name_only_unverified / unmatched
    identity_key     TEXT,          -- the exact key used to attach the fact (ccn:999001, parcel:IL:morgan:12-..., name:...)
    period_begin     DATE,          -- reporting period, verbatim from the source
    period_end       DATE,
    period_months    DOUBLE,        -- computed from the stated dates; never annualized
    units            TEXT,          -- usd_unconfirmed / days / count / ratio / stars ...
    unit_confidence  TEXT,          -- confirmed / unconfirmed
    raw_record_id    TEXT,          -- the source's own row id (rpt_rec_num, parcel number, case number)
    field_name       TEXT,          -- which field this is (net_income, fine_amount_total, ...)
    worksheet_ref    TEXT,          -- the source's own worksheet line when it publishes one (e.g. G3 L31)
    value_number     DOUBLE,        -- as reported; never adjusted
    value_text       TEXT,          -- as reported, when the value is text
    caveat           TEXT,          -- the qualifier that must travel with this fact
    coverage_limit   TEXT,          -- what this source cannot tell us (the bounded claim)
    file_id          TEXT,          -- the raw_files row the fact was parsed from, when one exists
    collected_at     TIMESTAMP
);

-- Every lookup, so "we searched for counter-evidence and found none" is a recorded fact,
-- not someone's memory. A 0-result row still counts as having searched.
CREATE TABLE IF NOT EXISTS research_searches (
    search_id      TEXT PRIMARY KEY,
    property_key   TEXT,
    identity_key   TEXT,            -- the key the search ran under, when there was no property mapping
    lane           TEXT,            -- counter_evidence / county_tax / liens / court_docket / ownership / press
    query          TEXT,            -- the exact query string or source scope used
    result_count   INTEGER,
    coverage_limit TEXT,            -- e.g. "result is bounded to the exact query string"
    searched_at    TIMESTAMP,
    notes          TEXT
);

-- Every place two sources disagree. An open conflict holds the property out of full depth.
CREATE TABLE IF NOT EXISTS research_conflicts (
    conflict_id  TEXT PRIMARY KEY,
    property_key TEXT,
    identity_key TEXT,
    topic        TEXT,              -- identity / period / value / spelling ...
    detail       TEXT,              -- both sides, each with its source
    status       TEXT,              -- open / resolved
    resolution   TEXT,
    opened_at    TIMESTAMP,
    resolved_at  TIMESTAMP
);

-- Assessment DRAFTS, isolated from the live research_assessments store: the dashboard and
-- the scorer never read this table, so a draft here can never overwrite, demote or inflate
-- a reviewed assessment or any UI count. Promotion into research_assessments is a
-- separate, person-reviewed step that this layer does not perform.
CREATE TABLE IF NOT EXISTS research_drafts (
    draft_id      TEXT PRIMARY KEY,
    property_key  TEXT,
    draft_kind    TEXT,          -- ready | held
    tier_label    TEXT,          -- always an "Unscored ..." label; drafts never carry a score
    confidence    TEXT,
    coverage      TEXT,
    score_reasons TEXT,          -- JSON array
    one_line      TEXT,
    created_at    TIMESTAMP
);

-- One row per property: the honest answer to "how researched is this?".
CREATE TABLE IF NOT EXISTS research_status (
    property_key              TEXT PRIMARY KEY,
    depth                     TEXT,     -- lead / partial / held / ready_for_review
    identity_verified         BOOLEAN,
    primary_financial_source  BOOLEAN,
    counter_evidence_searched BOOLEAN,
    caveats_stored            BOOLEAN,
    open_conflicts            INTEGER,
    hold_reason               TEXT,
    evidence_count            INTEGER,
    last_evaluated_at         TIMESTAMP,
    notes                     TEXT
);
"""

TABLES = ["research_evidence", "research_searches", "research_conflicts", "research_status",
          "research_drafts"]


def create_research_tables(con) -> None:
    """Create the research layer's tables if they do not exist yet (safe to run any time)."""
    con.execute(SCHEMA_SQL)


def open_db(path=None, read_only: bool = False):
    """Open the shared database with Parts A, B, D and the research layer's tables ready."""
    con = _open_abd_db(path, read_only=read_only)
    if not read_only:
        create_research_tables(con)
    return con
