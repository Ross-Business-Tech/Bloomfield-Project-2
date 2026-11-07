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
