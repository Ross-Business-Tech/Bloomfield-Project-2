"""
lenders.py - link every lender name we've seen to its bank's FFIEC ID (bank_stats.rssd_id).

In plain English: the county record says "FIRST FAKELAND BANK, N.A."; the call report says
"First Fakeland Bank". Exact (after cleanup) = linked. Very close = linked. Close-ish = a person
decides (match_review). Anything else = no link: a wrong bank is worse than no bank.
"""
from __future__ import annotations

from collections import Counter

from rapidfuzz import fuzz

from . import normalize as nz
from .properties import Review, decisions
from .schema import rows, table_exists

LENDER_KINDS = ("construction", "permanent", "modification", "extension", "unknown")


def lender_mentions(con) -> Counter:
    """Every lender name as written, and how many records use it."""
    seen: Counter = Counter()
    if table_exists(con, "recorded_loans"):
        for (name,) in con.execute(
                f"SELECT grantee FROM recorded_loans WHERE coalesce(loan_kind,'unknown') IN "
                f"({', '.join('?' * len(LENDER_KINDS))}) AND grantee IS NOT NULL", list(LENDER_KINDS)).fetchall():
            seen[name.strip()] += 1
    if table_exists(con, "filing_fields"):
        for (name,) in con.execute("SELECT lender_name FROM filing_fields WHERE lender_name IS NOT NULL "
                                   "AND coalesce(status,'ok') <> 'failed'").fetchall():
            seen[name.strip()] += 1
    return seen


def banks(con) -> dict[str, str]:
    """rssd_id -> latest bank name."""
    if not table_exists(con, "bank_stats"):
        return {}
    return {r["rssd_id"]: r["bank_name"] for r in rows(con, """
        SELECT rssd_id, bank_name FROM bank_stats
        QUALIFY row_number() OVER (PARTITION BY rssd_id ORDER BY report_date DESC) = 1""")
        if r["bank_name"]}


def match_lenders(con, cfg: dict) -> tuple[list[dict], list[Review]]:
    l_cfg = cfg["lender"]
    bank_names = banks(con)
    bank_keys = {rssd: nz.bank_key(n) for rssd, n in bank_names.items()}
    decided = decisions(con, "lender")
    out, reviews = [], []
    for name, times in sorted(lender_mentions(con).items()):
        key = nz.bank_key(name)
        best, best_score = None, 0.0
        for rssd, bk in bank_keys.items():
            s = 100.0 if bk == key else fuzz.token_sort_ratio(key or "", bk or "")
            if s > best_score:
                best, best_score = rssd, s
        # A person's earlier answer wins.
        manual_yes = next((r for (pair, d) in decided.items() if d == "yes" and name in pair
                           for r in pair if r != name and r in bank_names), None)
        said = decided.get(frozenset((name, best))) if best else None
        row = {"lender_name": name, "lender_key": key, "times_seen": times,
               "rssd_id": None, "bank_name": None, "match_score": round(best_score, 1), "match_method": "none"}
        if manual_yes:
            row.update(rssd_id=manual_yes, bank_name=bank_names[manual_yes], match_method="manual", match_score=100.0)
        elif best and said != "no" and best_score >= l_cfg["auto"]:
            row.update(rssd_id=best, bank_name=bank_names[best],
                       match_method="exact" if best_score == 100 else "fuzzy")
        elif best and said is None and best_score >= l_cfg["review"]:
            reviews.append(Review("lender", name, best, name, f"{bank_names[best]} (RSSD {best})",
                                  best_score, "lender name is close to a bank in the call reports"))
        out.append(row)
    return out, reviews
