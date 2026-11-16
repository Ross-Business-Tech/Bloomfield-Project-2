# Bloomfield Opportunity Score (0–100)

## What we heard from Bloomfield
- Construction loan still on the clock is the **strongest** signal. A project that opened with its construction loan still outstanding, or a loan from five years ago never paid off, is distress.
- Opening date watch is good but **weaker**. A 3–6 month delay is normal, so it's weighted accordingly.
- Creative signals are very promising once we find a reliable way to detect them.
- Court and lien filings come **late**: they show a situation that's already actionable, not an early warning.
- Forced and public sales are not really a signal, and CMBS data gives little edge because everyone has it.

## What we changed
- The construction-loan clock (weight 50) and opening date watch (20) outweigh the other signals (15 each). A building that **opened with its construction loan still outstanding** gets +0.25 on L.
- Court and lien filings are a **READY TO ACT** confirmation: at most 10 points, plus the tag.
- Auctions and CMBS are shown as context only.
- County records are found through **NETR Online** (`python -m collectors.discover`). Sites are screened in signal order: recorders (loan clock) → building departments (inspections, certificates of occupancy) → assessors (parcel IDs for Part C) → courts and public notices (ready to act). See `signal_priority` in `config/discovery.yaml`.

Built from Bloomfield's feedback: **early signals find the opportunity; court filings and liens only confirm it.**
All numbers live in `config/equation.yaml`. Code: `scorer/equation.py`. Tests: `tests/test_equation.py`.

## The equation

```
C     = 1 − Π (1 − q_j · s_j)        over the creative signals j        (noisy-or: several weak signals add up, never past 1)
rescale = min(2.0, 100 ÷ weights of the terms that CAN apply)          (applicability)
Early = Σ  weight × strength × ( max(boost(s), rescale) if s ≥ 0.60 else rescale )      over L (50), O (20), C (30)
boost(s) = 1 + 0.5 × (s − 0.60) ÷ 0.40      → ×1.0 at 60% strength, ×1.5 at 100%   (for C, s = strongest single creative signal)
Early = Early × 0.85 ^ (number of COUNTER-SIGNALS)
(a standout's 1.2 boost and the rescale do not stack: it gets whichever is larger)
Score = min(100, Early + Late)
```

| Term | What it measures | Strength 0 → 1 | Weight | Data from |
|---|---|---|---|---|
| **L** loan clock | A **construction loan**, or any **short-term loan that has needed an extension/modification** (bridge, value-add), still not paid off by a permanent loan or release | 0 before 30 months → 1 at 60 months. +0.1 per extension (max +0.2); +0.25 if a construction loan's building opened with the loan still outstanding | 50 | County recorder (`recorded_loans` / Reader) **and public SEC CMBS loan filings**: a loan past its maturity date and still outstanding = 1.0; the servicer's "modified" flag counts as an extension |
| **O** opening delay | Months past the latest reasonable opening date, page still "coming soon" | 0 under 6 months (normal) → 1 at 18 months. +0.1 missed student move-in, +0.05 per date slip (max +0.1) | 20 | Project websites (Part B `projects`); **if none, a public new-construction building permit with no certificate of occupancy**: months overdue = months since issue − 24 (typical build) |
| **C** creative | All creative signals below, combined | 1 − Π(1 − q·s) | 30 | see below |
| **C(k)** corroboration | **Nov 2026: removed.** A single signal counts in full (1, 2 or 3+ signals → 1.0), because much of the data isn't public | 1.0 | × | — |
| **Late** | Court/legal filing or lender/trustee default notice: 6. Contractor liens: 2 each, max 4. Older than 180 days counts half; filings dated after the scoring date are ignored | — | max 10 | Part B `filing_fields` |

### Creative signals: each one is measurable (code: `scorer/creative.py`)
| Signal | s = 0 → 1 | Reliability q | Data from |
|---|---|---|---|
| **R** rent vs. city | (city rent change − property rent change) ÷ 15 pts | 0.9 | B `rent_observations` + A `market_rents` |
| **I** site inactivity | 60 → 180 days since a city inspection on an open permit | 0.8 | A `permit_inspections` (filled by `collectors/permits.py` from city building departments; NETR does not list them) |
| **Q** occupancy slide | (peak occupancy in the last 36 months − occupancy now − market drop) ÷ 15 pts | 0.9 (×0.7 with no market data) | `operating_metrics` (public disclosures; SEC CMBS reports feed it one occupancy row per loan per report period) + `market_occupancy` |
| **V** income erosion | (best-year NOI in the last 48 months − NOI now) ÷ best-year NOI ÷ 50%. **No DSCR** (debt service is not available) | 1.0 | `operating_metrics` (public disclosures) |
| **X** cost squeeze | (expense growth − revenue growth since the best year) ÷ 15 pts | 0.6 | `operating_metrics` |
| **K** concession creep | (free months **per year of lease term** now − 6+ months earlier) ÷ 1 month. Unknown term = 12 months | 0.7 | B `rent_observations` (free rent + lease term) |
| **Lq** licensing / care | CMS 1-star 0.8, 2-star 0.5; abuse flag +0.2; state license distress 0.8 | 0.6 | A `licensing_records` |
| **W** value cut | (value at securitization − most recent value) ÷ value at securitization ÷ 50% | 1.0 | Public SEC CMBS loan filings (Schedule AL valuation fields) |

Related creative signals (e.g. falling occupancy and falling income) count as at most **2** anti-fluke confirmations, so one story can't confirm itself.

**Not scored** (Bloomfield: public, forced, or everyone already has it): Ten-X auctions, CMBS DSCR and watchlists.

**Tiers:** Hot ≥ 50 (call this week) · Watch ≥ 25 · Low below that.
**Stage:** *Early* = no late filings yet (Bloomfield gets there first). *Ready to act* = a court filing or lien already exists.

## Applicability (works for any industry)
A term that **cannot** apply is different from one with **no data yet**:
- **L** applies if a construction loan is recorded, the project is pre-opening or late, or a building permit / certificate of occupancy is less than 60 months old.
- **O** applies if the project is pre-opening or late, its opening date is less than 36 months old, or something is being built.
- **C** always applies.

The weights of the terms that apply are scaled up to fill 100, **at most 2×**. Example: a building open for years with no construction financing has only C (30) → ×2 → creative signals can reach 60 (72 with a standout), never 100 on their own.

## Valuation (any property type) — `scorer/valuation.py`
```
Stabilized occupancy = max(own best occupancy in the last 48 months, market occupancy)
Rent                 = market rent per unit of space if known, else best-year revenue per occupied unit
Stabilized revenue   = space × stabilized occupancy × rent
Stabilized NOI       = stabilized revenue − latest 12-month expenses                        (Bloomfield's method)
Cost to stabilize    = max(0, stabilized occ. − occupancy now) × space × (build-out per unit + commission % × rent × lease years)
                       (office $40/SF + 6% × 5 yrs; housing, senior housing, hotels: 0)
Value                = Stabilized NOI ÷ cap rate for the property type − cost to stabilize   (office 10%, senior housing 8%)
Stress cases   = run-rate expenses at cap + 1%;  today's NOI at cap + 2%
LTV            = what's owed ÷ value          → deal quality: ≤75% +15 · 75–100% +5 · >100% −10
Recovery       = value × (1 − 3% closing) − $25K/month legal × 24 months + reserves held for lenders
Bid            = recovery ÷ (1 + 15% target IRR) ^ 2
```
Sale comps are data, not formulas: value = Σ units × comparable $ per unit.

## Extremes: STANDOUT and COUNTER-SIGNAL
- **Strength boost (upside extreme, Nov 2026):** a signal at 60%+ strength counts more, rising in a straight line to ×1.5 at 100%. Example: a 5-year-old unpaid construction loan alone scores 50 × 1.0 × 1.5 = **75 → Hot**. Signals at 90%+ are tagged `STANDOUT` so the researcher knows why a property ranks high on a single signal.
- **COUNTER-SIGNAL (downside extreme):** strong evidence the other way cuts early points by 15% each and is tagged `COUNTER-SIGNAL`. Today's counter-signals: rent beating its city by 15+ points, or a city inspection in the last 14 days (construction is active).

## Why the anti-fluke rule
Each early signal is checked against the others. A single extreme number (one bad rent reading, a loan the recorder mis-labeled) keeps only half its points until a second, independent signal agrees. Inside each signal, more guards apply:
- **Rent** compares medians of early and late readings (not single points), needs 2+ readings 90+ days apart, and is measured **against the city**. A citywide dip doesn't count.
- **Loan clock** resets to 0 the moment a permanent loan or release is recorded.
- **Inspection gap** resets to 0 once a certificate of occupancy exists.
- **Opening delay** ignores the normal 3–6 month slip.

## Worked example (made up: Kestrel Hall, Ann Arbor)
| | Input | Strength | Points |
|---|---|---|---|
| L | Construction loan Mar 2022, 1 extension, 56 months | 0.86 + 0.10 = 0.96 | 48.2 |
| O | 8 months late, missed August move-in, slipped twice | 0.17 + 0.10 + 0.10 = 0.37 | 7.3 |
| R | Rent −12.4% while Ann Arbor +1% | 13.4 / 15 = 0.89 | 13.4 |
| I | Last inspection 162 days ago | 0.85 | 12.8 |
| | 4 signals showing → C = 1.0 | | **81.7** |
| Late | Contractor lien $184.5K, recent | | +2 |
| | **Score** | | **83.7 → Hot, Actionable** |

## How it connects to the rest of the pipeline
1. **Part A** (collectors) loads `recorded_loans` → **L**, and `market_rents` + `permit_inspections` → **R**, **I**.
2. **Part B** (scout + reader) fills `projects` → **O**, `rent_observations` → **R**, and `filing_fields` (permits, liens, court filings) → **I** and **Late**.
3. **Part C** (matcher) writes `property_links`, so `scorer.group` puts each property's records together before scoring.
4. **Part D** runs `python -m scorer.equation` and writes `equation_scores` (one row per property, with an `explanation` of every term).

## New tables this needs (created by `create_equation_tables`)
- `rent_observations(project_id, observed_at, asking_rent, unit_basis, source_url, file_id)`: Part B scout, from leasing pages and Wayback copies.
- `market_rents(city, state, period, rent_index, source)`: Part A, e.g. HUD Fair Market Rents, Census ACS or Zillow ZORI.
- `permit_inspections(permit_number, state, city, inspection_date, inspection_type, result, file_id)`: Part A `permits.py`.

Until those are filled, R and I count as "no data". They can't confirm anything, so properties lean on L and O.

## Run it
```
cd pipeline
python -m scorer.equation                    # score everything, print the top 15
python -m scorer.equation --explain Kestrel  # every term for one property
python -m pytest tests/test_equation.py -q
```

## The dashboard (Part D)
```
cd pipeline
python -m scorer.demo_week                                    # made-up demo with two weeks
BLOOMFIELD_DB=data/demo.duckdb streamlit run dashboard/app.py
```
On real data, after Parts A, B and C have run: `python -m scorer.weekly`, then `streamlit run dashboard/app.py`.

| Piece | File |
|---|---|
| The equation | `scorer/equation.py` |
| Weekly run: filters (office shown by default with a switch to hide it, $2.5M minimum and no upper limit, all U.S.), ranking, deal quality, deal angles, history, research log | `scorer/weekly.py` |
| Every number | `config/equation.yaml` |
| Pages: Ranked flags · Flag details · What changed this week · Research log · How the score works | `dashboard/app.py` |
| Demo data (uses the team's fake-data scripts and matcher, plus extra FAKE properties) | `scorer/demo_week.py` |

The older scorer (`python -m scorer`, `config/scoring.yaml`) still runs and writes `property_scores`. The dashboard uses the equation.

## Backtesting a real deal
`python -m scorer.case_study data/cases/<deal>.yaml` loads a deal into a throwaway database, scores it on several past dates (only data disclosed by each date), and runs valuation scenarios (`scorer/valuation.py`). Real deal files stay in `data/` (git-ignored): this repository is public.

## Where permits, inspections and certificates of occupancy come from
NETR Online lists recorders, assessors and tax offices only, not building departments. Each county file has a
`permits:` source pointing at the main city's building department (Ann Arbor A2Trak, Phoenix permit export,
Austin open data, Miami-Dade open data hub, St. Petersburg permit service). Exports dropped in
`data/inbox/permit/<ST>/<county>/{building_permit,permit_inspection,certificate_of_occupancy}/` (or fed by an
open-data source) are loaded by `python -m collectors.permits` (part of `collectors.run_all`) into
`permit_inspections` and `filing_fields`. That turns on **I** (site inactivity), the certificate-of-occupancy
check behind "opened but construction loan still outstanding", and the stale-opening check. Column names follow
the BLDS open permit-data standard, with aliases and per-county overrides. Residential permits are skipped.

## Public data behind each signal (Nov 2026)
Every input now has a public, regularly published source:

| Signal | Public source |
|---|---|
| L loan clock | County recorder (via NETR); SEC EDGAR CMBS Schedule AL: maturity date + balance, `modifiedIndicator` |
| O opening delay | Project / leasing websites and Wayback Machine; city building permits + certificates of occupancy |
| Q occupancy slide | SEC Schedule AL: occupancy at securitization vs. most recent (plus each monthly report) |
| V income erosion | SEC Schedule AL: NOI at securitization vs. most recent operating statement |
| X cost squeeze | SEC Schedule AL: revenue and operating expenses at securitization vs. most recent |
| W value cut | SEC Schedule AL: value at securitization vs. most recent value |
| R rent vs. city | Leasing pages / Wayback (property) vs. Zillow ZORI city index (`collectors/market_rents.py`) |
| K concession creep | Leasing pages / Wayback (free rent and lease term) |
| I site inactivity | City open-data permit and inspection records (`collectors/permits.py`) |
| Lq licensing / care | CMS Care Compare and state licensing data |

DSCR is still not scored (team decision), even though SEC filings report it.
