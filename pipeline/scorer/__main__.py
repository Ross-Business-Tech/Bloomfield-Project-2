"""
Run the scorer:   python -m scorer                (score everything, print the top 15)
                  python -m scorer --top 50
                  python -m scorer --state MI     (only show Michigan in the printout)
                  python -m scorer --csv          (also save data/exports/scores-<date>.csv)
                  python -m scorer --explain <property_id or part of its name>
                  python -m scorer --as-of 2026-06-30   (score as if it were that day)
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date

from collectors.common import config

from .schema import open_db
from .score import load_scoring_config, score_all, tier_counts, write_scores

TIER_MARK = {"hot": "HOT  ", "watch": "WATCH", "low": "low  "}


def _money(x):
    return f"${x / 1e6:,.1f}M" if x else "-"


def print_ranking(scored, top: int, state: str | None) -> None:
    rows = [sp for sp in scored if not state or (sp.facts["state"] or "").upper() == state.upper()]
    print(f"\n{'#':>3}  {'score':>5}  tier   {'loan':>8}  property")
    print("-" * 96)
    for i, sp in enumerate(rows[:top], 1):
        f = sp.facts
        name = f["property_name"] or f["property_address"] or sp.prop.property_id
        where = ", ".join(x for x in (f["city"], f["state"]) if x)
        print(f"{i:>3}  {sp.score:>5.0f}  {TIER_MARK[sp.tier]}  {_money(f['loan_amount']):>8}  {name}"
              + (f"  [{where}]" if where else ""))
        if sp.score > 0:
            print(f"{'':>26}why: {sp.summary}")
    if len(rows) > top:
        print(f"... {len(rows) - top} more (use --top)")


def explain(scored, query: str) -> int:
    q = query.lower()
    hits = [sp for sp in scored if q == sp.prop.property_id
            or q in (sp.facts["property_name"] or "").lower()
            or q in (sp.facts["property_address"] or "").lower()]
    if not hits:
        print(f"No property matches '{query}'.")
        return 1
    for sp in hits[:3]:
        f = sp.facts
        print(f"\n{f['property_name'] or f['property_address']}  ({sp.prop.property_id})")
        print(f"  address  {f['property_address']}   parcel {f['parcel_number'] or '-'}   county {f['county'] or '-'}")
        print(f"  borrower {f['borrower_name'] or '-'}   lender {f['lender_name'] or '-'}   loan {_money(f['loan_amount'])}")
        print(f"  SCORE {sp.score:.0f} / 100  ->  {sp.tier.upper()}   (records grouped by: {sp.prop.grouped_by})")
        for s in sorted(sp.signals, key=lambda s: -sp.counted[id(s)]):
            counted = sp.counted[id(s)]
            capped = "" if counted == s.points else f"  (capped from {s.points:.1f})"
            print(f"   +{counted:5.1f}  [{s.family}] {s.detail}{capped}")
            print(f"           evidence: {s.evidence_table}/{s.evidence_id}"
                  + (f"  dated {s.evidence_date}" if s.evidence_date else ""))
            if s.quote:
                print(f'           quote: "{s.quote}"')
        if not sp.signals:
            print("   no warning signs found")
    return 0


def save_csv(scored, as_of: date) -> str:
    out_dir = config.data_dir() / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"scores-{as_of}.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "score", "tier", "property_name", "property_address", "city", "state",
                    "county", "parcel_number", "borrower_name", "lender_name", "loan_amount",
                    "why", "property_id"])
        for i, sp in enumerate(scored, 1):
            f = sp.facts
            w.writerow([i, sp.score, sp.tier, f["property_name"], f["property_address"], f["city"],
                        f["state"], f["county"], f["parcel_number"], f["borrower_name"],
                        f["lender_name"], f["loan_amount"], sp.summary, sp.prop.property_id])
    return config.to_stored_path(path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scorer", description="Score and rank properties (Part D).")
    ap.add_argument("--db", help="database file (default: data/bloomfield.duckdb)")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--state", help="only print this state (still scores everything)")
    ap.add_argument("--csv", action="store_true", help="also save a CSV to data/exports/")
    ap.add_argument("--explain", metavar="PROPERTY", help="show every reason behind one property's score")
    ap.add_argument("--as-of", type=date.fromisoformat, default=None, help="score as of this date (YYYY-MM-DD)")
    ap.add_argument("--no-save", action="store_true", help="don't write the score tables")
    args = ap.parse_args(argv)

    cfg = load_scoring_config()
    as_of = args.as_of or date.today()
    con = open_db(args.db)
    scored = score_all(con, cfg, as_of)
    if not scored:
        print("No properties to score yet. Load data first (try: python scripts/make_fake_data.py "
              "then python scripts/make_fake_readings.py).")
        return 0

    if args.explain:
        return explain(scored, args.explain)

    if not args.no_save:
        write_scores(con, scored, cfg, as_of)
    counts = tier_counts(scored)
    print(f"Scored {len(scored)} properties as of {as_of} (scoring {cfg['version']}): "
          f"{counts['hot']} hot, {counts['watch']} watch, {counts['low']} low.")
    print_ranking(scored, args.top, args.state)
    if args.csv:
        print(f"\nSaved {save_csv(scored, as_of)}  (inside data/, never committed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
