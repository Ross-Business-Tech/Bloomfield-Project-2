# Bloomfield Opportunity Score (0–100)

Built from Bloomfield's feedback: **early signals find the opportunity; court filings and liens only confirm it.**
All numbers live in `config/equation.yaml`. Code: `scorer/equation.py`. Tests: `tests/test_equation.py`.

## The equation

```
Score = min(100,  C(k) × (50·L + 20·O + 15·R + 15·I)  +  Late)
```

| Term | What it measures | Strength 0 → 1 | Weight | Data from |
|---|---|---|---|---|
| **L** loan clock | Construction loan still not paid off by a permanent loan or release | 0 before 30 months → 1 at 60 months (5 years). +0.1 per extension, max +0.2 | 50 | Part A `recorded_loans` |
| **O** opening delay | Months past the latest reasonable opening date, page still "coming soon" | 0 under 6 months (normal) → 1 at 18 months. +0.1 missed student move-in, +0.05 per date slip (max +0.1) | 20 | Part B `projects` |
| **R** rent decline | Property's asking-rent change **minus** its city's rent change, same dates | 0 if keeping up with the city → 1 at 15 points worse | 15 | Part B `rent_observations` vs. Part A `market_rents` |
| **I** inspection gap | Days since the last city inspection on a permit with no certificate of occupancy | 0 under 60 days → 1 at 180 days | 15 | Part A `permit_inspections` + Part B permits in `filing_fields` |
| **C(k)** anti-fluke | k = how many of L, O, R, I are at least 0.25 | 1 signal → 0.5 · 2 → 0.8 · 3 or 4 → 1.0 | × | — |
| **Late** | Court/legal filing (foreclosure, receiver, lis pendens, lender lawsuit, UCC sale, tax sale): 6. Contractor liens: 2 each, max 4 (a lien ≥ 2% of the loan counts twice). Older than 180 days counts half | — | max 10 | Part B `filing_fields` |

**Not scored** (Bloomfield: public, forced, or everyone already has it): Ten-X auctions, Trepp / CMBS DSCR and watchlists.

**Tiers:** Hot ≥ 50 (call this week) · Watch ≥ 25 · Low below that.
**Stage:** *Early* = no late filings yet (Bloomfield gets there first). *Actionable* = a court filing or lien already exists.

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
