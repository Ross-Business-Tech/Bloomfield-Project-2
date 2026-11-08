# Part D: Scorer and dashboard (Rex)

Ranks every flagged property each week and gives the researcher a dashboard to review them.
All sample data is made up.

## Run it
```
cd pipeline
pip install -r requirements-dashboard.txt
python -m scorer.demo_data                                   # demo database with two made-up weeks
BLOOMFIELD_DB=data/demo.duckdb streamlit run dashboard/app.py
```
On real data (after Parts A, B and C have run): `python -m scorer.rank`, then `streamlit run dashboard/app.py`.
Tests: `python -m pytest tests_partd -q`

## Files
| File | What it does |
|---|---|
| `scorer/settings.yaml` | Bloomfield's weights and filters. Edit numbers here, not code. |
| `scorer/signals.py` | Finds warning signs on each property from Parts A/B/C tables, with source quotes. |
| `scorer/rank.py` | Desperation + deal-quality scores, filters, ranking, week-over-week changes. |
| `scorer/store.py` | Part D tables: `score_runs`, `flags`, `research_log`. |
| `scorer/demo_data.py` | Made-up demo database (Kestrel Hall, Ann Arbor, and 8 others). |
| `dashboard/app.py` | Streamlit pages: Ranked flags, Flag details, What changed this week, Research log. |

## Team decisions used
- Who's pushing, most to least serious: court/tax 40, lender 30, contractor 20, market 10.
- Default filters: loans $5M–$50M, all U.S., office hidden.
- First dashboard pages: research log and what changed this week.

## Reads from other parts
- Part A: `recorded_loans`, `loans`, `bank_stats`
- Part B: `filing_fields`, `projects` (column names from Vishwa's schema)
- Part C: `links(property_id, record_table, record_id)`. Until Part C exists, records are grouped by cleaned street address + state.

Missing tables are treated as empty, so the scorer runs before every part is finished.

## Not built yet
Match review, settings and source-list pages; AI-written summaries (a template summary is used now); contacts from Part C.
