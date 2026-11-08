"""
Run the matcher:  python -m matcher                    (match everything, print a summary)
                  python -m matcher --review           (list "maybe" matches waiting for a person)
                  python -m matcher --decide rev-abc123 yes   (or no; the next run obeys it)
                  python -m matcher --show "maize row" (one property: its records, loans, entities)
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from collectors.common import db

from .run import load_matching_config, run_matcher
from .schema import open_db, rows


def print_summary(s: dict) -> None:
    print(f"Matched {s['records_linked']} records into {s['properties']} properties "
          f"({s['multi_record_properties']} with more than one record).")
    print("  how records were linked: " + ", ".join(f"{k} {v}" for k, v in sorted(s["link_methods"].items())))
    print(f"Loan histories: {s['loan_histories']} loans, {s['open_construction_loans']} construction loans still open.")
    print(f"CMBS: {s['cmbs_loans']} loans, {s['cmbs_cross_source_groups']} reported by more than one source.")
    print(f"Lenders: {s['lenders_linked']} of {s['lender_names']} lender names linked to an FFIEC bank.")
    print(f"Entities: {s['entities']} ({s['multi_property_entities']} involved in more than one property).")
    print(f"State registry lookups requested: {s['lookup_requests_added']} new.")
    print(f"Needs a person: {s['pending_reviews']} maybe-matches ({s['new_reviews']} new). "
          "See: python -m matcher --review")


def print_reviews(con) -> int:
    pending = rows(con, "SELECT * FROM match_review WHERE decision IS NULL ORDER BY kind, score DESC")
    if not pending:
        print("Nothing waiting for review.")
        return 0
    for r in pending:
        print(f"\n{r['review_id']}  [{r['kind']}]  score {r['score']:.0f}  ({r['reason']})")
        print(f"   {r['left_label']}")
        print(f"   {r['right_label']}")
    print("\nAnswer with: python -m matcher --decide <review_id> yes|no   then run python -m matcher again.")
    return 0


def decide(con, review_id: str, answer: str) -> int:
    if answer not in ("yes", "no"):
        print("Answer must be yes or no.")
        return 1
    if not con.execute("SELECT 1 FROM match_review WHERE review_id = ?", [review_id]).fetchone():
        print(f"No review called {review_id}.")
        return 1
    con.execute("UPDATE match_review SET decision = ?, decided_at = ? WHERE review_id = ?",
                [answer, db.utc_now(), review_id])
    print(f"Saved: {review_id} = {answer}. Run python -m matcher to apply it.")
    return 0


def show(con, query: str) -> int:
    q = f"%{query.lower()}%"
    props = rows(con, """SELECT * FROM properties WHERE lower(coalesce(property_name,'')) LIKE ?
                         OR lower(coalesce(street_address,'')) LIKE ? OR property_id = ?""", [q, q, query])
    if not props:
        print(f"No property matches '{query}'. Run python -m matcher first?")
        return 1
    for p in props[:3]:
        print(f"\n{p['property_name'] or p['street_address']}  ({p['property_id']})")
        print(f"  {p['street_address']}, {p['city']}, {p['state']} {p['zip'] or ''}   parcel {p['parcel_number'] or '-'}"
              f"   county {p['county'] or '-'}   type {p['property_type'] or '-'}")
        print("  records:")
        for link in rows(con, "SELECT * FROM property_links WHERE property_id = ? ORDER BY record_table, record_id",
                         [p["property_id"]]):
            print(f"    {link['record_table']:<15} {link['record_id']:<40} via {link['match_method']} ({link['match_score']:.0f})")
        hist = rows(con, "SELECT * FROM loan_histories WHERE property_id = ? ORDER BY recording_date", [p["property_id"]])
        if hist:
            print("  loan histories:")
            for h in hist:
                amt = f"${h['amount'] / 1e6:.1f}M" if h["amount"] else "?"
                print(f"    {h['recording_date']} {h['loan_kind']:<12} {amt:>7} from {h['lender_name']}"
                      f" -> {h['status'].upper()} after {h['months_open']:.0f} months;"
                      f" {h['modification_count']} modification(s)"
                      + (f"; lender RSSD {h['lender_rssd_id']}" if h["lender_rssd_id"] else ""))
        ents = rows(con, """SELECT DISTINCT e.canonical_name, l.role, e.property_count FROM entity_links l
                            JOIN entities e USING (entity_id) WHERE l.property_id = ? ORDER BY l.role""",
                    [p["property_id"]])
        if ents:
            print("  people and companies:")
            for e in ents:
                other = f"  (also on {e['property_count'] - 1} other propert{'y' if e['property_count'] == 2 else 'ies'})" \
                    if e["property_count"] > 1 else ""
                print(f"    {e['role']:<14} {e['canonical_name']}{other}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m matcher", description="Link records about the same thing (Part C).")
    ap.add_argument("--db", help="database file (default: data/bloomfield.duckdb)")
    ap.add_argument("--review", action="store_true", help="list maybe-matches waiting for a person")
    ap.add_argument("--decide", nargs=2, metavar=("REVIEW_ID", "YES_OR_NO"))
    ap.add_argument("--show", metavar="PROPERTY", help="show one property's records, loans and entities")
    ap.add_argument("--as-of", type=date.fromisoformat, default=None)
    args = ap.parse_args(argv)

    con = open_db(args.db)
    if args.review:
        return print_reviews(con)
    if args.decide:
        return decide(con, args.decide[0], args.decide[1].lower())
    if args.show:
        return show(con, args.show)
    print_summary(run_matcher(con, load_matching_config(), args.as_of))
    return 0


if __name__ == "__main__":
    sys.exit(main())
