"""
cmbs.py - the same CMBS loan reported by more than one source.

In plain English: SEC EDGAR and Trepp both report CMBS loans, each with its own ID. Two loan rows
are the same loan when they are on the same property, come from different sources, and either
share the deal name or have (almost) the same original balance. Rows from the same source are
never merged (a property can have two real loans, e.g. a senior loan and a mezzanine piece).
Every report period of one loan keeps the same loan_key, so it's already one row per period.
"""
from __future__ import annotations

import hashlib
import re

from .properties import UnionFind
from .schema import rows, table_exists


def _deal(name: str | None) -> str | None:
    return re.sub(r"[^a-z0-9]", "", name.lower()) if name else None


def _source(row: dict) -> str:
    s = (row.get("source") or "").lower()
    if s:
        return s
    return (row["loan_key"].split(":", 1)[0] or "").lower()


def link_cmbs_loans(con, record_property: dict[str, str], cfg: dict) -> list[dict]:
    if not table_exists(con, "loans"):
        return []
    tol = cfg["cmbs"]["balance_tolerance"]
    # One row per loan_key (latest report period).
    loans = rows(con, """SELECT * FROM loans
                         QUALIFY row_number() OVER (PARTITION BY loan_key ORDER BY report_period DESC NULLS LAST) = 1""")
    uf = UnionFind()
    method: dict[str, str] = {}
    by_prop: dict[str, list[dict]] = {}
    for ln in loans:
        uf.find(ln["loan_key"])
        pid = record_property.get(f"loans:{ln['loan_key']}")
        if pid:
            by_prop.setdefault(pid, []).append(ln)

    for group in by_prop.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if _source(a) == _source(b):
                    continue
                same_deal = _deal(a.get("deal_name")) and _deal(a.get("deal_name")) == _deal(b.get("deal_name"))
                ba, bb = a.get("original_balance"), b.get("original_balance")
                same_bal = bool(ba and bb and abs(ba - bb) <= tol * max(ba, bb))
                if same_deal or same_bal:
                    uf.union(a["loan_key"], b["loan_key"])
                    m = "same_deal_and_property" if same_deal else "same_property_balance"
                    method[a["loan_key"]] = method[b["loan_key"]] = m

    out = []
    for ln in loans:
        root = uf.find(ln["loan_key"])
        out.append({"loan_key": ln["loan_key"],
                    "loan_group_id": "lg-" + hashlib.sha1(root.encode()).hexdigest()[:12],
                    "property_id": record_property.get(f"loans:{ln['loan_key']}"),
                    "match_method": method.get(ln["loan_key"], "alone")})
    return out
