# Part A: Collectors (Person 1, Neel)

Gets raw data into the shared database: `raw_files`, `loans`, `bank_stats`
(plus a proposed `licensing_records`). Table definitions: [`collectors/schema_draft.sql`](collectors/schema_draft.sql).

---

## ⚠️ NEEDS TEAM AGREEMENT

No shared schema existed when this was built, so these are **proposals**. Please
confirm or rename before Parts B–D start reading them. Changing a name now is
a one-line edit; later it breaks other people's code.

**Agreed already (from the build plan):** `raw_files.file_id`, `source`, `state`,
`date`, `fingerprint`, `local_path`, `downloaded_at`, and the three table names.

**Guessed by Part A:**

| Table | Guessed columns |
|---|---|
| `raw_files` | `source_url`, `content_type`, `size_bytes`, **`needs_reading`** (1 = a filing for Part B's AI reader), **`restricted`** (1 = licensed Trepp data: **Part B must never send these to an external API**) |
| `loans` (all columns) | `loan_id`, `reporting_period`, `source`, `file_id`, `deal_name`, `loan_number`, `property_name`, `property_address`, `city`, `state`, `zip`, `property_type`, `borrower_name`, `lender_name`, `servicer_name`, `original_balance`, `current_balance`, `interest_rate`, `origination_date`, `maturity_date`, `dscr`, `occupancy`, `delinquency_status`, `special_servicing`, `watchlist`, `watchlist_comment`, `loaded_at`. Key: (`loan_id`, `reporting_period`) |
| `bank_stats` (all columns) | `rssd_id`, `report_date`, `bank_name`, `city`, `state`, `cre_loans_total`, `cre_nonaccrual`, `cre_nonaccrual_ratio`, `nonaccrual_qoq_change`, `nonaccrual_trend`, `file_id`. Key: (`rssd_id`, `report_date`) |
| `licensing_records` (**new table, not in the plan**) | `record_id`, `as_of_date`, `source`, `file_id`, `facility_name`, `legal_name`, `license_number`, `facility_type`, `address`, `city`, `state`, `zip`, `status`, `distress_flag`, `distress_reason`. Key: (`record_id`, `as_of_date`) |

Conventions to agree on too:
- `source` values: `EDGAR`, `TREPP`, `FFIEC`, `CMS`, `STATE:<XX>`, `COUNTY:<name>`.
- `loan_id` = `<SOURCE>:<deal name>:<loan number>`, e.g. `TREPP:BANK 2019-BNK20:12`. The same loan from EDGAR and Trepp gets **two** IDs; linking them is Part C's job.
- Dates are `YYYY-MM-DD` text; booleans are 0/1; rates and occupancy are decimals (`0.0525`, `0.82`); DSCR is a plain ratio (`0.95`).
- Call report amounts are in **thousands of dollars**. `nonaccrual_qoq_change` is the dollar change ($000) from the bank's previous loaded quarter; `nonaccrual_trend` is `up`/`down`/`flat`, or empty for the first quarter loaded.
- EDGAR loans can have several properties; `loans` keeps the **first** property's address.
- EDGAR ABS-EE data has no borrower name or watchlist flag, so those are empty for `source = EDGAR`. They come from Trepp.

---

## Setup (once)

```bash
cd Neel
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then edit .env
```

In `.env`, set at least `USER_AGENT`, for example `Bloomfield AI Sprint Team yourname@umich.edu`.
SEC requires a name and a contact email. Leave `DB_URL` blank to use SQLite (`Neel/data/bloomfield.sqlite`).
For Postgres, set `DB_URL=postgresql+psycopg://user:pw@host/db` and `pip install "psycopg[binary]"`.

Tables are created automatically the first time any collector runs.

## Running each collector

Run these from the `Neel/` folder. Every run prints: files found, downloaded,
skipped as duplicates, rows written, and errors. Running any of them twice adds nothing new.

| Collector | Command | Writes |
|---|---|---|
| Trepp | `python -m collectors.trepp /path/to/trepp_export.xlsx` (or set `TREPP_EXPORT_PATH`). Add `--as-of 2026-10-31` if the file has no date column. | `loans` (source `TREPP`), `raw_files` (restricted) |
| EDGAR | `python -m collectors.edgar --since 2026-10-01` (options: `--until`, `--cik 1234567`, `--max-filings 5` for a quick first test) | `loans` (source `EDGAR`), `raw_files` |
| FFIEC | `python -m collectors.ffiec --zip "FFIEC CDR Call Bulk All Schedules 06302026.zip"` or `--dir folder_of_zips` | `bank_stats`, `raw_files` |
| State + CMS | `python -m collectors.state` (or `--only cms_nursing_home_provider_info`) | `licensing_records`, `raw_files` (`CMS`, `STATE:<XX>`) |
| County | `python -m collectors.county --since 2026-10-01` (or `--only dallas_tx -v`) | `raw_files` (`COUNTY:<name>`, `needs_reading = 1`) |

**Getting the FFIEC zip.** The FFIEC page is a web form, not a plain link, so download the zip by hand:
1. Go to https://cdr.ffiec.gov/public/PWS/DownloadBulkData.aspx
2. **Available Products**: *Call Reports -- Single Period*
3. **Reporting Period End Date**: the quarter you want. Do this for the last 2+ quarters so a trend can be computed.
4. **File Format**: *Tab Delimited*, then click **Download**
5. Put the zips in one folder and run `python -m collectors.ffiec --dir that_folder`

**Trepp is licensed.** `trepp.py` makes no network calls and doesn't copy the file anywhere. It records the file in `raw_files` with `restricted = 1`. Keep exports outside the repo or in `Neel/trepp_exports/` (git-ignored). If Trepp renames a column, add the new header to `config/trepp_mapping.yaml`.

## Adding a county

No code needed. Add an entry to [`config/counties.yaml`](config/counties.yaml):

```yaml
  - name: harris_tx                # becomes source COUNTY:harris_tx
    state: TX
    enabled: true
    verified: false
    base_url: "https://www.example-county.gov"
    pages: ["/foreclosure-notices"]        # can use {since:%m/%d/%Y} / {until:%Y-%m-%d}
    fetch: links                           # links | page | direct
    link_pattern: "(?i)\\.pdf($|\\?)"      # which links are filings
    next_text_pattern: "^next"             # optional paging
```

Then test it with `python -m collectors.county --only harris_tx -v`. Check that the log shows
files saved, then set `verified: true`. Use `fetch: page` when the page itself is the list of notices,
and `fetch: direct` when the URL is the filing itself (for example a monthly PDF).

The scraper obeys robots.txt and stops on any login or CAPTCHA page. If a county
needs either, set `enabled: false` and add it to the table below. Do not try to get around it.

## Site status and skipped sites

This code was written in a cloud sandbox whose network policy **blocked every data site**
(sec.gov, cdr.ffiec.gov, data.cms.gov, data.chhs.ca.gov and all county sites returned "403 Forbidden").
So everything was tested only against fake sample files. The first real run on a normal connection
is the real check. If a field comes back empty, fix the name in the matching `config/*.yaml` file.

| Source | Config | Status |
|---|---|---|
| SEC EDGAR ABS-EE (CMBS) | `config/edgar.yaml` | Not yet run live. Tag names follow the SEC CMBS asset-data schema. |
| FFIEC call reports | `config/ffiec.yaml` | Not yet run live. Bulk download is a web form, so the zip is downloaded by hand (above). |
| CMS nursing home Provider Information (`4pq5-n9py`) | `config/state_sources.yaml` | Not yet run live |
| California health facilities (CHHS open data) | `config/state_sources.yaml` | Not yet run live |
| Dallas County, TX: County Clerk foreclosure notices | `config/counties.yaml` | Not yet run live |
| Oakland County, MI: Sheriff sales | `config/counties.yaml` | Not yet run live |
| Cook County, IL: Clerk of Circuit Court | `config/counties.yaml` | Not yet run live. Case search may need a form. |
| Maricopa County, AZ: Recorder | `config/counties.yaml` | Not yet run live. Document search may need a form. |
| Miami-Dade County, FL: foreclosure auction calendar | `config/counties.yaml` | Not yet run live. May be JavaScript-only. |

**Skipped (login / CAPTCHA / robots.txt):** none confirmed yet. Add each one here after the first live run, with the reason.

## For Parts B and C

- **Part B (Reader):** read files where `needs_reading = 1 AND restricted = 0`. Never send `restricted = 1` files to an LLM or OCR API.
- **Part C (Matcher):** `bank_stats.rssd_id` is the FFIEC ID to match lenders against. Loans from the same deal share `deal_name`.

## Tests

```bash
cd Neel && python -m pytest -q
```

Tests use only the fake files in `fixtures/` and never touch the network. They cover fingerprint
dedupe, Trepp column mapping (including a renamed column fixed by config only), county template
parsing of a saved sample page, idempotent re-runs of every collector, robots.txt and login-wall
skipping, and one failing source not stopping the others.

## Files

```
collectors/common.py   database, download + SHA-256 dedupe, rate limits, retries, run stats
collectors/trepp.py    Trepp CSV/XLSX -> loans
collectors/edgar.py    SEC ABS-EE EX-102 XML -> loans
collectors/ffiec.py    call report bulk zip -> bank_stats (+ trend)
collectors/state.py    CMS + state licensing -> licensing_records
collectors/county.py   county scraper template -> raw_files
config/*.yaml          everything source-specific (column names, URLs, codes, counties)
fixtures/              FAKE sample data only
```
