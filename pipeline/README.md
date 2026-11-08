# Bloomfield Sourcing Tool: Part A, Public Records Collectors

These collectors get raw public data **into a DuckDB database** (`data/bloomfield.duckdb`).
They do not interpret documents (Part B), match records (Part C) or score anything (Part D).
Everything runs from the `pipeline/` folder.

> This folder lives inside a repo that is **public**. Never commit `.env`, the database,
> downloaded files, the inbox, or anything from Trepp or Bloomfield. `.gitignore` and a
> pre-commit check protect you (see "Safety" below).

## One-time setup

```bash
cd pipeline
pip install -r requirements.txt
cp .env.example .env          # then edit .env and put your real email in CONTACT_EMAIL
python scripts/setup_db.py            # creates the database and all tables
python scripts/load_county_registry.py   # loads all ~3,140 U.S. counties (needs internet)
python scripts/install_hooks.py       # blocks commits that contain private data
```

If the county download is blocked, save the Census file in your browser
(`https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt`)
and run `python scripts/load_county_registry.py --file <that file>`.

## Try it with fake data (no internet needed)

```bash
python scripts/make_fake_data.py      # one troubled Ann Arbor project + 20 healthy ones
python -m pytest                      # run the tests
```

## The tables (shared with Parts B, C, D)

`raw_files`, `recorded_loans`, `loans`, `bank_stats`, `collector_runs`, plus
`collector_state` (restart bookmarks), `county_registry` (every U.S. county and whether it
is set up) and `entity_lookup_requests` (Part C asks us to look up business entities).
Numbers are stored as plain decimals (DOUBLE). `file_path` values are relative to `pipeline/`.

## The manual inbox (works for every source)

When a site blocks robots, download the documents by hand and drop them in:

```
data/inbox/<source>/<state>/<county>/            e.g. data/inbox/recorder/MI/washtenaw/lien1.pdf
data/inbox/<source>/<state>/_statewide/
data/inbox/<source>/                             (national sources)
```

`<source>` is one of `county_court, recorder, public_notice, permit, state_license, cms,
sec_edgar, ffiec, manual_upload`. Add a category folder one level deeper
(`.../washtenaw/mechanics_lien/lien1.pdf`) or a sidecar file `lien1.pdf.json` with
`{"url": "...", "source_record_id": "...", "doc_category": "...", "city": "...", "doc_type": "..."}`
to give details. Loading moves each file into `data/raw/`, fingerprints it, and logs it in
`raw_files`. Duplicates go to `data/inbox/_duplicates/`. (Trepp files have their own folder,
`data/inbox/trepp/`, handled by `trepp.py`.)

## Safety rules built into the code

* Every web request goes through `collectors/common/fetch.py`: checks robots.txt, waits between
  requests (default 3 s per site), identifies us with our contact email, retries only on
  temporary errors, and **never** tries to get past CAPTCHAs, logins or "access denied". Such
  sites are switched to manual inbox and the reason is logged.
* Every file is saved unchanged and fingerprinted (SHA-256); identical files are never loaded twice.
* One collector crashing never stops the others; each run is logged in `collector_runs`.

## Safety (public repo)

`pipeline/.gitignore` keeps `.env`, `data/`, databases, spreadsheets and anything with "trepp"
in its name out of git (except our own Trepp code). `scripts/check_no_data_staged.py` (installed
by `install_hooks.py`) refuses commits containing them, and `tests/test_repo_safety.py` fails if
anything private is tracked.

## Status

Built: shared building blocks (`collectors/common/`), database setup, county registry, fake data, and the
collectors below (ported from Neel's version onto this shared design). NOT built yet: `recorded_loans.py`,
`liens.py`, `permits.py`. See "Running the collectors".

---

# Part B: the Scout and the Reader

* The **Reader** uses an AI model to turn documents into clean fields (with the exact quote each came from) in the `filing_fields` table.
* The **Scout** checks a fixed list of approved websites and tracks announced projects' opening dates.

Status: steps (a) and (b) are built (AI wrapper, cost cap, tables, field definitions, and the whole Reader: OCR, chunking, prompt, checks, batch runner). Reader accuracy tests and the scout follow.

## One-time setup

```bash
cd pipeline
pip install -r requirements.txt
brew install tesseract poppler      # Mac: needed only to read scanned PDFs and images
python scripts/setup_db.py          # creates Part A's AND Part B's tables
```

Add to `pipeline/.env` (never commit it, never paste a key into chat):

```
LLM_API_KEY=your-key-here
# LLM_PROVIDER=anthropic            # or openai_compatible (then also set LLM_BASE_URL)
```

Model names, prices, spending caps and privacy switches all live in `config/reader.yaml`.
**The prices in that file are placeholders. Check your provider's price list and edit them.**

## Changing AI provider or model

Edit `provider` / `models` in `config/reader.yaml`, or set `LLM_PROVIDER`, `LLM_MODEL_CHEAP`, `LLM_MODEL_STRONG` in `.env`. All AI calls go through `llm/client.py`; to add a new company, add one function there and list it in `PROVIDERS`.

## Reading the cost summary

```bash
python -m llm.cost            # last 7 days, by job and model
python -m llm.cost --days 30
```

Columns: calls made, input/output tokens, and estimated dollars (tokens x the prices in `reader.yaml`). Answers served from the cache are free and not listed. The bottom line shows the last 7 days against `weekly_cost_cap_usd`. Before each call the wrapper checks the worst case against both `weekly_cost_cap_usd` and `run_cost_cap_usd` and stops with a clear message instead of going over.

## Privacy

* Trepp data is never sent to the AI (`trepp_ai_allowed: false`).
* Files marked `"confidential": true`, or dropped in `manual_upload/bloomfield/`, are never sent (`bloomfield_material_ai_allowed: false`). Only Bloomfield decides whether these switches change.
* The AI only reads text we hand it; it never browses or uses tools.

## Running the Reader

```bash
cd pipeline
python scripts/make_fake_data.py          # optional: fake documents to practice on
python -m reader.batch --limit 5          # read 5 waiting files (needs your API key in .env)
python -m reader.batch --state MI         # only Michigan
python -m reader.batch --source recorder  # only one source
python -m reader.batch --retry-failed     # also re-try files that failed before
python -m llm.cost                        # what it cost
```

Each document becomes one row in `filing_fields` with a status:

| status | meaning |
|---|---|
| `ok` | nothing suspicious |
| `needs_review` | data was kept, but a person should look; `review_reason` says why (quote not found, low confidence, truncated, poor scan, impossible amount, label disagrees with Part A...) |
| `failed` | the AI's answer was unusable twice, or the file could not be read |

Good to know:
* **Quote check:** every quote must appear in the document; a made-up quote always means `needs_review`.
* **It skips** SEC EDGAR, FFIEC, CMS and Trepp files (Part A already turns those into tables) and anything confidential.
* **Confidential material:** files from `data/inbox/manual_upload/bloomfield/` (you can still put `MI/washtenaw/` folders inside), or with a sidecar saying `"confidential": true`, are loaded into `raw_files` with `[CONFIDENTIAL]` at the start of `notes` and are never sent to the AI.
* **Safe to stop and restart.** Finished files are saved immediately; answers already paid for come from the cache.
* **Scans:** need Tesseract (`brew install tesseract poppler`). Without it, scanned files are left unread (not marked failed) and picked up next time.
* **Changing the instructions:** edit `reader/prompt.py` and bump `PROMPT_VERSION`; the reader then re-reads everything.
* **Spending:** the batch stops with a message when `weekly_cost_cap_usd` or `run_cost_cap_usd` would be passed.

---

# Part D: the Scorer (Maahik)

The **Scorer** gathers everything we know about each property, looks for warning signs, and
ranks every property from 0 to 100 so Bloomfield knows whom to call first. Every point comes
with a plain-English reason and a pointer to the record that proves it. No AI, no internet,
no cost: it only reads the tables Parts A, B and C already filled.

## Try it on the fake data (no API key needed)

```bash
cd pipeline
python scripts/setup_db.py
python scripts/make_fake_data.py        # Part A's fake records
python scripts/make_fake_readings.py    # pretends Part B's Reader already read them
python -m scorer                        # score and print the top 15
python -m scorer --explain "maize row"  # every reason behind one property's score
python -m scorer --csv                  # also save data/exports/scores-<date>.csv
python -m scorer --state MI --top 50    # print only Michigan, more rows
python -m scorer --as-of 2026-06-30     # score as if it were that day
```

Expected: the fake Ann Arbor student-housing project is #1 (HOT), two fake properties are on
the WATCH list (an office with a weak CMBS loan, a hotel with a lis pendens), and the 20
healthy ones score 0.

## What it looks for

| Family (cap) | Warning sign | Comes from |
|---|---|---|
| Construction loan (35) | construction loan 30+ months old with no permanent loan or payoff; each modification/extension after it | Part A `recorded_loans` |
| Permits (20) | permit extended; permit expired; permit 2+ years old with no certificate of occupancy | Part B `filing_fields` |
| Liens (20) | mechanic's lien (full points if filed in the last 180 days, bonus if it's 2%+ of the loan) | Part B `filing_fields` |
| Lender (15) | the lender's CRE nonaccruals rose two quarters in a row; nonaccrual ratio 3%+ | Part A `bank_stats` (lender name matched with a strict fuzzy match) |
| CMBS (35) | DSCR under 1; occupancy under 80%; watchlist; delinquent; past maturity; matures within 12 months | Part A `loans` |
| Legal (40) | foreclosure, receiver, lis pendens, lender lawsuit, UCC sale, auction, defaults, tax delinquency, license problems... | Part B `filing_fields` (`filing_type` and `distress_tags`) |
| Opening (25) | the Scout's stale "coming soon" page, missed student move-in, opening date moved | Part B `projects` |

How the points add up:
* **Caps per family** stop one story being counted five times (e.g. five bad CMBS numbers max out at 35).
* **One document counts once**, at its strongest tag.
* **Old events** (legal notices, older than 2 years) count half. **Unchecked evidence** (the Reader's `needs_review`) counts 75%. Failed reads are ignored.
* Total is capped at 100. **HOT** = 60+, **WATCH** = 30+, otherwise **low**.

All points, caps and thresholds live in `config/scoring.yaml`. Change them there (and bump
`version`), not in the code.

## The tables it writes

* `property_scores`: one row per property: score, tier, one-line summary, name, address, parcel, borrower, lender, biggest loan, `grouped_by`, `scoring_version`.
* `score_signals`: every reason: signal name, family, points, points actually counted after caps, sentence, `evidence_table` + `evidence_id` (the exact row/file), date, and the Reader's exact quote when there is one.

Each run replaces both tables, so they always show the latest picture.

## How it decides which records are the same property

* **If Part C (the Matcher) writes `property_links`**, the scorer uses it. Contract:
  `property_links(property_id TEXT, record_table TEXT, record_id TEXT)`, where `record_table` is
  `recorded_loans`, `filing_fields`, `loans` or `projects` and `record_id` is that table's key
  (`record_id`, `file_id`, `loan_key`, `project_id`). Extra columns are fine.
* **Until then (or for records Part C didn't link)**, a cautious fallback: records belong together
  when they share a parcel number, or the same street address in the same state
  ("12 North Main Street" = "12 N. Main St"). No fuzzy name matching: that's Part C's job.

## Tests

```bash
python -m pytest tests/test_scorer.py
```


---

# Running the collectors (Part A)

All commands run from the `pipeline/` folder. Everything is logged in the `collector_runs` table.

```bash
python -m collectors.run_all                                  # every collector, in order, with a summary
python -m collectors.run_all --only county --county mi_washtenaw
python -m collectors.run_all --state MI                       # one state: inbox + county only
```

| Collector | What it does | Where its input comes from |
|---|---|---|
| `trepp.py` | Loads the Trepp screen (DSCR under 1.0x) into `loans` (source `trepp`). Stays local; never sent anywhere. | Drop the CSV/Excel in `data/inbox/trepp/`. Columns: `config/trepp_columns.yaml`. |
| `edgar.py` | CMBS loans from SEC ABS-EE filings into `loans` (source `sec_edgar`), newest report per loan; every XML kept in `raw_files`. At most 20 trusts per run. | Automatic (SEC, 4 requests/sec). Manual fallback: `data/inbox/sec_edgar/` plus a `.json` sidecar `{"notes": "deal=<trust name>"}`. |
| `ffiec.py` | Bank CRE balances, past dues, nonaccruals and trends into `bank_stats`. | Download by hand (below) into `data/inbox/ffiec/`. |
| `county.py` | County/statewide court, recorder and public-notice records into `raw_files`, driven by `config/counties/*.yaml`. | Per-county YAML. Today every sample county is `manual_inbox`. |
| `state.py` | CMS nursing home data (all states), state licensing actions, business-registry lookups for Part C. | `config/state_sources.yaml`, `config/state_registry.yaml`. |

## Things that are NOT verified yet (please check)

The sandbox these were built in could not reach ffiec.gov, federalreserve.gov or sec.gov, so:

* **FFIEC item codes** (`config/ffiec.yaml`) and **EDGAR XML tag names** (`config/edgar.yaml`) are marked UNVERIFIED. The
  first real run prints a **code audit / tag audit** that shows what the real file says each one means and warns about
  mismatches. Check it by eye, then fix the YAML. One code was changed from Neel's draft (owner-occupied nonfarm
  nonresidential nonaccrual: F182 -> F180) because F182 looks like another line; verify it.
  `python -m collectors.ffiec --audit some.zip` prints just the audit.
* **CMS dataset IDs** and the **column names** in `config/state_sources.yaml`; **every county site and state agency page**.
  Anything unchecked stays `manual_inbox` / `enabled: false` until a person checks its terms of use.

## Getting the FFIEC files

The FFIEC page is a web form, not a plain link: 1) open https://cdr.ffiec.gov/public/PWS/DownloadBulkData.aspx,
2) Available Products: *Call Reports -- Single Period*, 3) Reporting Period End Date: a quarter, 4) File Format:
*Tab Delimited*, click Download, 5) repeat for the last 6 quarters and drop the zips in `data/inbox/ffiec/`, then
`python -m collectors.ffiec`. Amounts are stored in dollars (the call report uses thousands). `cre_nonaccrual_ratio` is a
fraction (0.03 = 3%); `nonaccrual_change_qoq` is the change in that ratio; `rising_two_quarters` is true when the ratio rose
this quarter and the one before.

## Adding a county

1. `python scripts/load_county_registry.py` once (loads every county into `county_registry`).
2. `python scripts/add_county.py 26161` (the FIPS code), or `--state MI --county Washtenaw`. This writes
   `config/counties/mi_washtenaw.yaml` with everything set to `manual_inbox`.
3. Check the county's court, recorder and notice sites (terms of use, robots.txt, login/CAPTCHA). If automation is allowed,
   describe the site in the YAML (see `config/counties/_template.yaml`: CSS selectors for the case number, filing date,
   parties, document type and document link, or an open-data API) and set `status: configured`.
4. `python scripts/load_county_registry.py` again updates the county's status in the registry. No Python changes.

A file with `statewide: true` covers a whole state (statewide court search, press-association notice sites).
If a site blocks automation, `county.py` switches that source to the manual inbox and logs why.
