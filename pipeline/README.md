> U-M Toolkit setup: [UMICH-WORKFLOW.md](UMICH-WORKFLOW.md).

# Bloomfield Sourcing Tool: Part A, Public Records Collectors

These collectors get raw public data **into a DuckDB database** (`data/bloomfield.duckdb`).
They do not interpret documents (Part B), match records (Part C) or score anything (Part D).
Everything runs from the `pipeline/` folder.

> This folder lives inside a repo that is **public**. Never commit `.env`, the database,
> downloaded files, the inbox, or anything from Bloomfield. `.gitignore` and a
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
`raw_files`. Duplicates go to `data/inbox/_duplicates/`.

## Safety rules built into the code

* Every web request goes through `collectors/common/fetch.py`: checks robots.txt, waits between
  requests (default 3 s per site), identifies us with our contact email, retries only on
  temporary errors, and **never** tries to get past CAPTCHAs, logins or "access denied". Such
  sites are switched to manual inbox and the reason is logged.
* Every file is saved unchanged and fingerprinted (SHA-256); identical files are never loaded twice.
* One collector crashing never stops the others; each run is logged in `collector_runs`.

## Safety (public repo)

`pipeline/.gitignore` keeps `.env`, `data/`, databases, spreadsheets and anything with "trepp"
in its name out of git. `scripts/check_no_data_staged.py` (installed
by `install_hooks.py`) refuses commits containing them, and `tests/test_repo_safety.py` fails if
anything private is tracked.

## Status

Built: shared building blocks (`collectors/common/`), database setup, county registry, fake data, and the
collectors below (ported from Neel's version onto this shared design). NOT built yet: `recorded_loans.py`,
`liens.py`, `permits.py`. See "Running the collectors".

---

# Demo for a screenshare (no API, no internet)

```bash
cd pipeline
python -m demo --open        # builds demo/demo.html in ~5 seconds from the real pipeline on FAKE data, and opens it
python -m demo --no-build --open   # just open the page that is already built (instant)
```

Or simply double-click `pipeline/demo/demo.html` (a ready-built copy is committed). It is one self-contained file: it works
offline, needs no server and no API key, and every name and number in it is invented. `python -m demo --serve` serves it at
http://localhost:8000 (a phone on the same wifi can open it with your computer's address).

Tabs: **Ranked flags** (click a flag for the evidence, the source document with the exact quote highlighted, the timeline,
loan history, the lender's health and the researcher's approve/dismiss buttons; office properties are shown by default and a
"Hide office" switch filters them out) · **AI Reader** (real Gemini answers recorded earlier, shown next to the source text with
the quote checks run on them, including one the checks caught) · **Data sources** (what is automatic, manual or unverified) ·
**How it works**. Approve/dismiss choices and notes are saved in your browser only.

* The demo runs the real steps in a scratch folder (`data/demo/`, wiped each build): create tables, FAKE collected data,
  sample readings, `python -m matcher`, `python -m scorer`. It never touches your real database.
* `python -m demo --live 6` also lets the real AI Reader read the first 6 documents (needs an API key with quota).
* To record fresh real answers for the Reader tab: `python -m demo.record --data-dir <data folder> --file-ids <fake ids>`.

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
OPENAI_API_KEY=<your-U-M-Toolkit-key-set-locally>
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.toolkit.umgpt.umich.edu/v1
LLM_MODEL_CHEAP=gpt-4o-mini
LLM_MODEL_STRONG=gpt-4o
```

Every AI step (the reader, the scout, the plain-English summaries) goes through one wrapper (`llm/client.py`) and uses
`OPENAI_API_KEY` unless you switch provider. A website must never hold the key: if the web page ever calls the AI,
it goes through a here.now proxy route that references `${OPENAI_API_KEY}` (your instructor adds that variable).

Model names, prices, spending caps and privacy switches all live in `config/reader.yaml`.
**The prices in that file are placeholders. Check your provider's price list and edit them.**

## Changing AI provider or model

Supported: `anthropic`, `gemini` (Google) and `openai_compatible`. Set `LLM_PROVIDER` in `.env`, or `provider` in
`config/reader.yaml`. Model names are listed **per provider** in `reader.yaml` (`models:`), so switching provider also
switches models; override with `LLM_MODEL_CHEAP` / `LLM_MODEL_STRONG` in `.env`. All AI calls go through
`llm/client.py`; to add a new company, add a request builder and a reply parser there and list them in `PROVIDERS`.

**Only Part B's Reader (and later the Scout) calls an AI.** Part C (matcher) and Part D (scorer) are plain code, so they
work the same whichever provider you choose.

### Using Gemini

```bash
# in pipeline/.env
LLM_PROVIDER=gemini
GEMINI_API_KEY=your-key        # GOOGLE_API_KEY or LLM_API_KEY also work. Never commit or paste it.
```

```bash
python -m llm.check        # 30-second test with two tiny real calls (a fraction of a cent); explains any problem
python -m reader.batch --limit 5
```

* **Models** (tested against the real Gemini API on 2026-11-08): cheap = `gemini-3.5-flash-lite`, strong =
  `gemini-3.5-flash`. Google retires models quickly (the 2.5 models already refuse new accounts, with a 404): if
  `llm.check` says a model is "no longer available", list current names and edit `models.gemini` in `reader.yaml`.
* **Thinking:** Gemini "thinks" before answering, which costs tokens and can cut the JSON answer off. The setting that
  turns it down differs per model (`model_generation_config` in `reader.yaml`: Flash-Lite wants `thinkingLevel: minimal`,
  Flash wants `thinkingBudget: 0`). If you switch models and get HTTP 400, try the other setting. Gemini counts thinking
  tokens as output tokens in the cost estimate.
* **Prices** for the Gemini 3.5 models are not listed yet: the (higher) default price is used for the cost cap until you add
  the real ones under `prices_per_million_tokens`.
* **Busy models:** some Gemini models are slow or return "high demand" (503) errors at times. The wrapper retries up to 5
  times with waits (and obeys Google's "retry in N seconds" hint); the timeout is 180 s.
* **Free keys:** Google may use prompts sent through free (unbilled) keys to improve its products. Our reader only sends
  public records (never Bloomfield material), but use a paid, billing-enabled key if that matters to Bloomfield.
* Gemini through its OpenAI-compatible address also works: `LLM_PROVIDER=openai_compatible`,
  `LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai`, `LLM_API_KEY=<key>`, plus Gemini model names under
  `models.openai_compatible`.

## Reading the cost summary

```bash
python -m llm.cost            # last 7 days, by job and model
python -m llm.cost --days 30
```

Columns: calls made, input/output tokens, and estimated dollars (tokens x the prices in `reader.yaml`). Answers served from the cache are free and not listed. The bottom line shows the last 7 days against `weekly_cost_cap_usd`. Before each call the wrapper checks the worst case against both `weekly_cost_cap_usd` and `run_cost_cap_usd` and stops with a clear message instead of going over.

## Privacy

* Bloomfield's own material is never sent to the AI (`bloomfield_material_ai_allowed: false`).
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
* **It skips** SEC EDGAR, FFIEC and CMS files (Part A already turns those into tables) and anything confidential.
* **Confidential material:** files from `data/inbox/manual_upload/bloomfield/` (you can still put `MI/washtenaw/` folders inside), or with a sidecar saying `"confidential": true`, are loaded into `raw_files` with `[CONFIDENTIAL]` at the start of `notes` and are never sent to the AI.
* **Safe to stop and restart.** Finished files are saved immediately; answers already paid for come from the cache.
* **Scans:** need Tesseract (`brew install tesseract poppler`). Without it, scanned files are left unread (not marked failed) and picked up next time.
* **Changing the instructions:** edit `reader/prompt.py` and bump `PROMPT_VERSION`; the reader then re-reads everything.
* **Spending:** the batch stops with a message when `weekly_cost_cap_usd` or `run_cost_cap_usd` would be passed.

---

# Part C: the Matcher (Maahik)

The **Matcher** works out which records are about the same thing. County clerks, the SEC,
County clerks, the AI Reader and the Scout all spell names and addresses differently and use different
IDs. Part C links them so Part D (and people) see one property, one loan, one bank and one
company instead of five half-stories. No AI, no internet, no cost.

## Run it

```bash
cd pipeline
python scripts/setup_db.py
python scripts/make_fake_data.py        # Part A's fake records
python scripts/make_fake_readings.py    # pretends Part B's Reader already read them
python -m matcher                       # link everything, print a summary
python -m matcher --show "maize row"    # one property: its records, loan story, companies
python -m matcher --review              # "maybe" matches waiting for a person
python -m matcher --decide rev-xxxx yes # or no; the next run obeys it
python -m scorer                        # Part D then scores using Part C's links
```

Expected on the fake data: 24 properties; the Ann Arbor project's 5 records linked into one,
with one construction loan still OPEN after ~42 months and 1 modification; the Phoenix loan that
The same CMBS loan reported twice recognized as one loan; all 6 lender names linked to their FFIEC
bank; and **one** maybe-match for a person (a tax notice that misspells the hotel's street as
"Exmaple Drive"). Answer it `yes` and re-run: 23 properties, and the hotel's score rises.

## What it links, and how carefully

| What | Rule | Table |
|---|---|---|
| **Same property** | 1. same parcel number in the same state; 2. same street address in the same state ("12 North Main Street, Suite 2" = "12 N. Main St"); 3. same house number + nearly the same street name (92+/100), same city/ZIP; 4. records with no address: same property name (92+) in the same state/city | `properties`, `property_links` |
| **Never auto-linked** | street type differs (Main **St** vs Main **Ave**), direction differs (**N** Elm vs **S** Elm), different ZIP/city, scores 80-92: these go to `match_review` | `match_review` |
| **Loan histories** | each construction/permanent mortgage with its follow-ups: modifications and extensions, assignments, the permanent loan that took out a construction loan, and the release. Follow-ups use the instrument number they cite; otherwise the newest open loan (releases: the oldest) | `loan_histories` (status `open` / `refinanced` / `released`, `months_open`) |
| **Same CMBS loan, two sources** | same property, different source, and same deal name or original balance within 2%. Two loans from one source are never merged | `loan_links` (`loan_group_id`) |
| **Lender -> bank** | lender name vs `bank_stats.bank_name` after cleanup ("N.A.", "Inc."...): exact or 92+ = linked; 80-92 = a person decides; otherwise no link (a wrong bank is worse than none) | `lender_links` (`rssd_id`) |
| **Companies and people** | every borrower, developer, lender, servicer, lien claimant and other party; spellings merged when they clean up the same ("Maize Row Holdings, L.L.C." = "MAIZE ROW HOLDINGS LLC") or are 95+ alike **and** start with the same word **and** don't differ by a number or phase ("Example Property 01" stays apart from "02") | `entities`, `entity_links` |
| **State registry lookups** | LLCs, corporations and partnerships that are borrowers or developers are added to Part A's `entity_lookup_requests` (once per name and state) | `entity_lookup_requests` |

Every run rebuilds Part C's tables from scratch (in one transaction), except `match_review`,
where people's answers are kept. Property IDs are stable: the same parcel/address always gets the
same `property_id`, and it's the same ID Part D's fallback would give.

All thresholds live in `config/matching.yaml`.

## For the other parts

* **Part D** reads `property_links` (grouping), `lender_links` (which bank) and `loan_links`
  (count a loan reported by two sources once). It still runs without Part C, using a cautious fallback.
* **Part A** gets lookup requests in `entity_lookup_requests` (`requested_by = part_c_matcher`).
* `property_links.record_table` is `recorded_loans`, `filing_fields`, `loans` or `projects`;
  `record_id` is that table's key (`record_id`, `file_id`, `loan_key`, `project_id`).

## Tests

```bash
python -m pytest tests/test_matcher.py
```

---

# Part D: the Scorer (Maahik)

The **Scorer** gathers everything we know about each property, looks for warning signs, and
ranks every property from 0 to 100 so Bloomfield knows whom to call first. Every point comes
with a plain-English reason and a pointer to the record that proves it. No AI, no internet,
no cost: it only reads the tables Parts A, B and C already filled.

---

# Part E: the Sourcing Agent (the API does the legwork)

The idea in one line: **the AI does the work of sourcing information and compiling evidence;
the team only changes the scoring.** The scorer (Part D) stays a normal, deterministic program -
the AI never grades its own homework.

The **agent** (`agent/`) is the part that talks to the AI (the U-M GPT Toolkit by default) in a
think-act loop:

1. It reads its standing instructions from **`config/agent.md`** - that file is what tells the
   API what to do. Edit it to change how the agent sources.
2. It plans and acts through a short list of read-only tools (`agent/tools.py`): search the web
   (when a search key is configured), fetch a public page (saved unchanged as proof), run one of
   the project's public-data collectors, and ask the local database read-only questions.
3. Every fact it relies on must be saved as a VERIFIED **evidence row** (`record_evidence`):
   a page the project actually fetched (file_id + exact address + a quote copied verbatim from the
   saved bytes) or the database SELECT whose result shows the value. The tool re-checks the proof
   and refuses anything else - a made-up address or a paraphrased "quote" never becomes evidence.
   Verified web evidence also lands in the existing filings workflow as a needs-review lead for a
   person to confirm, same as the news bot's leads.
4. Scoring never changes: the scorer reads what is in the database, as before. Fetched pages are
   data to quote, never instructions to obey, and the agent is instructed accordingly.

## Run it

The agent is **off by default** because each run spends LLM budget (within the reader.yaml
spending cap). Turn it on and give it a goal:

```bash
# in pipeline/.env:  BLOOMFIELD_AGENT=1   (plus the usual OPENAI_API_KEY / LLM_BASE_URL)
python -m agent "find what the public record says about the Willow Creek project"
```

Every run is logged in `agent_runs` and `agent_steps` (what it asked, what each tool returned),
and the facts it verified land in the `evidence` table. Knobs live in `config/agent.yaml`
(step cap, result size, which tools are allowed).

## Honest limits

- Open-web search needs a search-service key: Brave (`BRAVE_SEARCH_API_KEY`, `provider: brave`) or
  Tavily (`TAVILY_API_KEY`, `provider: tavily` - free plan is 1,000 searches/month, no card) in .env
  and config/bot.yaml; without one the agent says so and works from known addresses and the
  collectors instead.
- The collectors only cover public datasets. A dataset that covers one state, one loan type,
  or one month is not proof that nothing exists elsewhere, and the agent is instructed to say
  exactly that.
- The agent fetches public pages only: no logins, no paywalls, robots.txt respected, and private,
  local, or cloud-internal addresses are refused outright (checked again after any redirect).
- Database access is read-only at the parser level: exactly one SELECT, verified by DuckDB's own
  statement parser, capped at 200 rows.

## Try it on the fake data (no API key needed)

```bash
cd pipeline
python scripts/setup_db.py
python scripts/make_fake_data.py        # Part A's fake records
python scripts/make_fake_readings.py    # pretends Part B's Reader already read them
python -m matcher                       # Part C: link records (optional but recommended)
python -m scorer                        # score and print the top 15
python -m scorer --explain "maize row"  # every reason behind one property's score
python -m scorer --csv                  # also save data/exports/scores-<date>.csv
python -m scorer --state MI --top 50    # print only Michigan, more rows
python -m scorer --as-of 2026-06-30     # score as if it were that day
```

Expected: the fake Ann Arbor student-housing project is #1 (HOT), two fake properties are on
the WATCH list (an office with a weak CMBS loan, a hotel with a lis pendens), a tax notice with
a misspelled hotel address scores low on its own until a person confirms the match in Part C,
and the 20 healthy ones score 0.

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

* **When Part C (the Matcher) has run**, the scorer uses its `property_links`. Contract:
  `property_links(property_id TEXT, record_table TEXT, record_id TEXT)`, where `record_table` is
  `recorded_loans`, `filing_fields`, `loans` or `projects` and `record_id` is that table's key
  (`record_id`, `file_id`, `loan_key`, `project_id`). Extra columns are fine.
* It also uses Part C's `lender_links` (which bank a lender name is, including a person's
  answers) and `loan_links` (the same CMBS loan from two sources is counted once).
* **Until Part C has run (or for records it didn't link)**, a cautious fallback: records belong together
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


## Finding county record websites (NETR Online)

`collectors/discover.py` reads the NETR Online directory (approved by NETR), collects each county's recorder,
court, public-notice and assessor links, screens every site (robots.txt, login or CAPTCHA wall, terms wording),
and writes proposed county settings files. No setup is needed: requests identify the team with the default contact address in `config/sources.yaml` (set `CONTACT_EMAIL` in `pipeline/.env` to use a different one). Run from `pipeline/`:

```
python -m collectors.discover --check                                        # can it reach and read NETR?
python -m collectors.discover --state MI --county "Washtenaw County" --screen --proposals
python -m collectors.discover --state MI --limit 10 --screen --proposals     # a batch of counties
python -m collectors.discover --report
```

If the network blocks NETR, save the county page from a browser and use
`--from-html saved_page.html --state MI --county "Washtenaw County"`. Screened sites are only candidates:
a person confirms each site's terms and adds its search settings before it becomes automatic.


## The web bot (`python -m bot`)

One searcher per signal, run weekly. Settings are in `config/bot.yaml`.

| Signal | What it does | Code |
|---|---|---|
| `opening_date` | Opens project websites you list, finds "Opening Fall 2026" or "coming soon", turns it into a deadline and flags late projects and dates that keep moving | `bot/opening.py` |
| `distress_news` | Reads approved news feeds for foreclosures, receiverships, lis pendens, UCC sales, auctions and lender lawsuits on commercial property. Each lead is saved as `needs_review` with a word-for-word quote | `bot/news.py` |
| `county_records` | Runs the county collectors for the sites that are set up | `collectors/county.py` |
| `lenders` | Bank call reports (rising nonaccruals) | `collectors/ffiec.py` |
| `cmbs` | SEC CMBS filings | `collectors/edgar.py` |

```
python -m bot --list                          # what each signal does
python -m bot --full                          # the whole week: crawl, AI reads, link, score
python -m bot --full --no-ai                  # the same without the AI (keyword reading only)
python -m bot                                 # crawl only
python -m bot --signals opening_date          # just one signal
```

`python -m bot --full` is the one command for a real week. The bot's crawlers collect pages and records (project sites, news,
county, bank and SEC data). The **AI reader (OpenAI)** then reads each page or document into fields with an exact, checked
quote and spots the signals in it. The matcher links records about the same property, and the Opportunity Score equation
ranks them. The score is plain arithmetic on the evidence, so a researcher can always see why a property ranked where it did:
the AI reads and identifies, it never invents a score. Without a key it says so and still runs with keyword reading.
Sample (FAKE) properties are left out of every real list; the demo database opts in explicitly.

Rules it follows: every page goes through `collectors/common/fetch.py` (robots.txt obeyed, polite pace, stops at any
login, CAPTCHA or "access denied", never gets around a block); third-party listing sites such as Zillow are never opened;
one run opens at most `max_pages_per_run` pages. Searching the open web needs a licensed search service: set
`search.provider: brave` in `config/bot.yaml` and `BRAVE_SEARCH_API_KEY` in `.env`. Without one the bot still checks the
project sites and feeds you list. Court and lien news arrives late, so it confirms a deal is ready to act rather than warning early.
