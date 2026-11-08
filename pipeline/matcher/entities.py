"""
entities.py - merge spelling variants of borrowers, developers, contractors and lenders.

In plain English: "Maize Row Holdings FAKE LLC", "MAIZE ROW HOLDINGS FAKE, L.L.C." and
"Maize Row Holdings Fake" are one company. We collect every name from every record, clean it
up (normalize.entity_key), merge exact matches, and merge near-identical spellings only when
they start with the same word ("Smith Development" vs "Smyth Development" stays separate if the
score is below the bar). Then we ask Part A's state.py to look up LLCs and corporations that
are borrowers or developers (entity_lookup_requests), so we can learn who's behind them.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict

from rapidfuzz import fuzz

from collectors.common import db

from . import normalize as nz
from .properties import UnionFind
from .schema import rows, table_exists

MORTGAGE_KINDS = {"construction", "permanent", "modification", "extension", "unknown"}


def mentions(con, record_property: dict[str, str]) -> list[dict]:
    """Every (name, role, record) we can find."""
    out = []

    def add(name, role, table, rid, state):
        if name and str(name).strip():
            out.append({"raw_name": str(name).strip(), "role": role, "record_table": table,
                        "record_id": str(rid), "state": nz.state_key(state),
                        "property_id": record_property.get(f"{table}:{rid}")})

    if table_exists(con, "recorded_loans"):
        for r in rows(con, "SELECT * FROM recorded_loans"):
            mortgage = (r["loan_kind"] or "unknown") in MORTGAGE_KINDS
            add(r["grantor"], "borrower" if mortgage else "party", "recorded_loans", r["record_id"], r["state"])
            add(r["grantee"], "lender" if mortgage else "party", "recorded_loans", r["record_id"], r["state"])
    if table_exists(con, "filing_fields"):
        for r in rows(con, """SELECT f.*, w.state AS raw_state FROM filing_fields f
                              LEFT JOIN raw_files w USING (file_id) WHERE coalesce(f.status,'ok') <> 'failed'"""):
            st = r["property_state"] or r["raw_state"]
            for col, role in (("borrower_name", "borrower"), ("developer_name", "developer"),
                              ("lender_name", "lender"), ("servicer_name", "servicer"),
                              ("lien_claimant", "lien_claimant")):
                add(r[col], role, "filing_fields", r["file_id"], st)
            try:
                parties = json.loads(r["other_parties"]) if r["other_parties"] else []
            except (TypeError, ValueError):
                parties = []
            for p in parties if isinstance(parties, list) else []:
                if isinstance(p, dict):
                    add(p.get("name"), (p.get("role") or "party").lower(), "filing_fields", r["file_id"], st)
    if table_exists(con, "projects"):
        for r in rows(con, "SELECT * FROM projects"):
            add(r["developer_name"], "developer", "projects", r["project_id"], r["state"])
    return out


_ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def numbers_conflict(a: str, b: str) -> bool:
    """'example property 01' vs '... 02', or 'phase i' vs 'phase ii': different entities."""
    ta, tb = set(a.split()), set(b.split())
    diff = ta ^ tb
    return any(any(ch.isdigit() for ch in t) or t in _ROMAN or len(t) == 1 for t in diff)


def build_entities(con, record_property: dict[str, str], cfg: dict):
    ms = [m for m in mentions(con, record_property) if nz.entity_key(m["raw_name"])]
    for m in ms:
        m["key"] = nz.entity_key(m["raw_name"])
        m["kind"] = nz.entity_kind(m["raw_name"])

    # Merge near-identical keys that start with the same word and aren't bank vs non-bank.
    uf = UnionFind()
    kind_of = {}
    for m in ms:
        uf.find(m["key"])
        kind_of.setdefault(m["key"], set()).add("bank" if m["kind"] == "bank" else "other")
    by_first = defaultdict(list)
    for k in kind_of:
        by_first[k.split()[0]].append(k)
    for keys in by_first.values():
        keys.sort()
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if kind_of[a] != kind_of[b] or numbers_conflict(a, b):
                    continue
                if fuzz.ratio(a, b) >= cfg["entity"]["auto"]:
                    uf.union(a, b)

    groups = defaultdict(list)
    for m in ms:
        groups[uf.find(m["key"])].append(m)

    entities, links = [], []
    for root, group in groups.items():
        eid = "ent-" + hashlib.sha1(root.encode()).hexdigest()[:12]
        spellings = Counter(m["raw_name"] for m in group)
        kinds = Counter(m["kind"] for m in group)
        entities.append({
            "entity_id": eid, "canonical_name": spellings.most_common(1)[0][0], "entity_key": root,
            "entity_kind": kinds.most_common(1)[0][0],
            "roles": ", ".join(sorted({m["role"] for m in group})),
            "states": ", ".join(sorted({m["state"] for m in group if m["state"] != "??"})),
            "property_count": len({m["property_id"] for m in group if m["property_id"]}),
            "name_variants": json.dumps(sorted(spellings)),
        })
        seen = set()
        for m in group:
            k = (m["record_table"], m["record_id"], m["role"])
            if k not in seen:
                seen.add(k)
                links.append({"entity_id": eid, "record_table": m["record_table"], "record_id": m["record_id"],
                              "role": m["role"], "raw_name": m["raw_name"], "property_id": m["property_id"]})
    entities.sort(key=lambda e: e["entity_id"])
    return entities, links


def request_lookups(con, entities: list[dict], cfg: dict) -> int:
    """Add state business-registry lookups for Part A. Never asks twice for the same name+state."""
    if not table_exists(con, "entity_lookup_requests"):
        return 0
    l_cfg = cfg["entity_lookups"]
    asked = {(n.lower(), s) for n, s in con.execute(
        "SELECT entity_name, state FROM entity_lookup_requests").fetchall()}
    n = 0
    for e in entities:
        if e["entity_kind"] not in l_cfg["kinds"]:
            continue
        if not set(e["roles"].split(", ")) & set(l_cfg["roles"]):
            continue
        for st in filter(None, e["states"].split(", ")):
            if (e["canonical_name"].lower(), st) in asked:
                continue
            db.insert_row(con, "entity_lookup_requests", {
                "entity_name": e["canonical_name"], "state": st, "requested_by": "part_c_matcher",
                "requested_at": db.utc_now(), "status": "pending"})
            asked.add((e["canonical_name"].lower(), st))
            n += 1
    return n
