-- DRAFT shared schema for the tables Part A (Collectors) writes.
-- No shared schema existed in the repo when this was written, so every column
-- not marked [AGREED] is a proposal. See README.md -> "NEEDS TEAM AGREEMENT".
--
-- Written in the SQL subset that works on both SQLite and Postgres:
--   * dates are ISO strings 'YYYY-MM-DD' in DATE columns
--   * timestamps are ISO-8601 UTC strings in TIMESTAMP columns
--   * booleans are INTEGER 0/1
--   * money is DOUBLE PRECISION (call report amounts are in THOUSANDS of dollars)

-- One row per downloaded or uploaded file.
CREATE TABLE IF NOT EXISTS raw_files (
    file_id        TEXT PRIMARY KEY,          -- [AGREED] random hex id
    source         TEXT NOT NULL,             -- [AGREED] EDGAR | FFIEC | TREPP | CMS | STATE:<XX> | COUNTY:<name>
    state          TEXT,                      -- [AGREED] 2-letter code, NULL for national sources
    date           DATE,                      -- [AGREED] date the data covers (filing date / reporting period)
    fingerprint    TEXT NOT NULL UNIQUE,      -- [AGREED] SHA-256 hex of the file bytes
    local_path     TEXT NOT NULL,             -- [AGREED] where the bytes live on disk
    downloaded_at  TIMESTAMP NOT NULL,        -- [AGREED] UTC time we got it
    source_url     TEXT,                      -- guessed: where it came from (NULL for uploads)
    content_type   TEXT,                      -- guessed: e.g. application/pdf, text/html, text/csv
    size_bytes     INTEGER,                   -- guessed
    needs_reading  INTEGER NOT NULL DEFAULT 0,-- guessed: 1 = a filing for Person 2's AI reader (county PDFs/HTML)
    restricted     INTEGER NOT NULL DEFAULT 0 -- guessed: 1 = licensed data (Trepp). NEVER send to an external API.
);

-- Loan-level records from EDGAR CMBS filings and Bloomfield's Trepp export.
CREATE TABLE IF NOT EXISTS loans (
    loan_id             TEXT NOT NULL,        -- guessed: '<SOURCE>:<deal_name>:<loan_number>' (stable across months)
    reporting_period    DATE NOT NULL,        -- guessed: period end date the numbers are for
    source              TEXT NOT NULL,        -- guessed: EDGAR | TREPP
    file_id             TEXT REFERENCES raw_files(file_id),
    deal_name           TEXT,
    loan_number         TEXT,
    property_name       TEXT,
    property_address    TEXT,
    city                TEXT,
    state               TEXT,
    zip                 TEXT,
    property_type       TEXT,
    borrower_name       TEXT,
    lender_name         TEXT,                 -- originator for EDGAR
    servicer_name       TEXT,
    original_balance    DOUBLE PRECISION,
    current_balance     DOUBLE PRECISION,
    interest_rate       DOUBLE PRECISION,     -- as a decimal fraction, e.g. 0.0525
    origination_date    DATE,
    maturity_date       DATE,
    dscr                DOUBLE PRECISION,     -- e.g. 0.95 = 0.95x
    occupancy           DOUBLE PRECISION,     -- decimal fraction, e.g. 0.82
    delinquency_status  TEXT,                 -- source code as given (e.g. '0','1','2','3','4','A','B')
    special_servicing   INTEGER,              -- 0/1, NULL if unknown
    watchlist           INTEGER,              -- 0/1, NULL if unknown
    watchlist_comment   TEXT,
    loaded_at           TIMESTAMP NOT NULL,
    PRIMARY KEY (loan_id, reporting_period)
);

-- FFIEC call report CRE nonaccrual figures, one row per bank per quarter.
CREATE TABLE IF NOT EXISTS bank_stats (
    rssd_id                 TEXT NOT NULL,
    report_date             DATE NOT NULL,     -- quarter end, e.g. 2026-06-30
    bank_name               TEXT,
    city                    TEXT,
    state                   TEXT,
    cre_loans_total         DOUBLE PRECISION,  -- $ thousands
    cre_nonaccrual          DOUBLE PRECISION,  -- $ thousands
    cre_nonaccrual_ratio    DOUBLE PRECISION,  -- cre_nonaccrual / cre_loans_total
    nonaccrual_qoq_change   DOUBLE PRECISION,  -- cre_nonaccrual minus the bank's previous quarter, $ thousands
    nonaccrual_trend        TEXT,              -- up | down | flat | NULL (no earlier quarter loaded)
    file_id                 TEXT REFERENCES raw_files(file_id),
    PRIMARY KEY (rssd_id, report_date)
);

-- NOT IN THE ORIGINAL PLAN. Structured rows from CMS nursing home data and state
-- licensing lists (state.py). Needs team agreement before anyone else reads it.
CREATE TABLE IF NOT EXISTS licensing_records (
    record_id        TEXT NOT NULL,           -- '<source>:<license or CMS number>'
    as_of_date       DATE NOT NULL,
    source           TEXT NOT NULL,           -- CMS | STATE:<XX>
    file_id          TEXT REFERENCES raw_files(file_id),
    facility_name    TEXT,
    legal_name       TEXT,                    -- owner / legal business name when given
    license_number   TEXT,
    facility_type    TEXT,
    address          TEXT,
    city             TEXT,
    state            TEXT,
    zip              TEXT,
    status           TEXT,                    -- license status / special-focus status as given
    distress_flag    INTEGER NOT NULL DEFAULT 0,
    distress_reason  TEXT,                    -- why distress_flag = 1, e.g. 'Special Focus Facility; fines $120,000'
    PRIMARY KEY (record_id, as_of_date)
);
