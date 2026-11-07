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

Built so far: shared building blocks (`collectors/common/`), database setup, county registry,
fake data. Still to come (in order): Trepp + inbox collector, FFIEC, EDGAR, county / recorded
loans / liens / permits, state, `run_all`. Instructions for each will be added here as it lands.
