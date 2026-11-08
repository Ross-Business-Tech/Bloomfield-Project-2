"""
properties.py - decide which records are about the same building.

In plain English, in this order:
  1. Same parcel number in the same state            -> same property (certain)
  2. Same street address in the same state            -> same property (certain)
  3. Same house number, nearly the same street name   -> same property ("Campus Wy" = "Campus Way")
     ...but never when the street type or direction disagrees ("Main St" vs "Main Ave",
     "N Main" vs "S Main"): those go to a person instead.
  4. Records with no usable address are matched by property name in the same state/city.
  5. Close calls go to match_review; a person's yes/no is obeyed on the next run.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from . import normalize as nz
from .schema import rows, table_exists

ADDRESS_TOKENS = {"st", "ave", "rd", "blvd", "dr", "ln", "ct", "pl", "pkwy", "hwy", "cir", "ter",
                  "sq", "trl", "way"}
DIRECTIONS = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}


@dataclass
class Rec:
    table: str
    rid: str
    state: str
    city: str | None = None
    zip: str | None = None
    street: str | None = None          # normalized street key
    street_raw: str | None = None
    parcel: str | None = None          # normalized parcel key
    parcel_raw: str | None = None
    name: str | None = None            # normalized property name
    name_raw: str | None = None
    county: str | None = None
    ptype: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.table}:{self.rid}"

    def keys(self) -> list[str]:
        out = []
        if self.parcel:
            out.append(f"parcel:{self.state}|{self.parcel}")
        if self.street:
            out.append(f"addr:{self.state}|{self.street}")
        return out


# ---------------------------------------------------------------------------
# Reading the records
# ---------------------------------------------------------------------------
def _rec(table, rid, state, city, zp, address, parcel, name, county=None, ptype=None) -> Rec:
    street_raw, a_city, a_state, a_zip = nz.split_address(address)
    st = nz.state_key(state or a_state)
    return Rec(table, str(rid), st, nz.city_key(city or a_city), nz.zip5(zp or a_zip),
               nz.street_key(address), street_raw, nz.parcel_key(parcel), parcel,
               nz.property_name_key(name), name, county, ptype)


def load_records(con) -> list[Rec]:
    out: list[Rec] = []
    if table_exists(con, "recorded_loans"):
        for r in rows(con, "SELECT * FROM recorded_loans"):
            out.append(_rec("recorded_loans", r["record_id"], r["state"], None, None,
                            r["property_address"], r["parcel_number"], None, r["county"]))
    if table_exists(con, "filing_fields"):
        for r in rows(con, """SELECT f.*, w.state AS raw_state, w.city AS raw_city, w.county AS raw_county
                              FROM filing_fields f LEFT JOIN raw_files w USING (file_id)
                              WHERE coalesce(f.status, 'ok') <> 'failed'"""):
            out.append(_rec("filing_fields", r["file_id"], r["property_state"] or r["raw_state"],
                            r["property_city"] or r["raw_city"], r["property_zip"], r["property_address"],
                            r["parcel_number"], r["project_name"], r["raw_county"], r["property_type"]))
    if table_exists(con, "loans"):
        for r in rows(con, "SELECT * FROM loans"):
            out.append(_rec("loans", r["loan_key"], r["property_state"], r["property_city"],
                            r["property_zip"], r["property_address"], None, r["property_name"],
                            None, r["property_type"]))
    if table_exists(con, "projects"):
        for r in rows(con, "SELECT * FROM projects"):
            out.append(_rec("projects", r["project_id"], r["state"], r["city"], None,
                            r["property_address"], None, r["project_name"], r["county"], r["property_type"]))
    return out


# ---------------------------------------------------------------------------
# Union-find: "these things belong together"
# ---------------------------------------------------------------------------
class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            lo, hi = sorted((ra, rb))         # deterministic: smaller name wins
            self.parent[hi] = lo

    def groups(self) -> dict[str, list[str]]:
        out = defaultdict(list)
        for x in list(self.parent):
            out[self.find(x)].append(x)
        return out


def property_id_for(keys: list[str], fallback: str) -> str:
    """Stable ID: the same parcel/address always gives the same property_id (Part D uses this too)."""
    basis = min(keys) if keys else fallback
    return "prop-" + hashlib.sha1(basis.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Address comparison
# ---------------------------------------------------------------------------
def compare_streets(a: str, b: str) -> tuple[float, str | None]:
    """
    Score two normalized streets (same state assumed). Returns (score, blocker).
    blocker is a reason it must not auto-match even if the score is high.
    """
    if nz.house_number(a) != nz.house_number(b):
        return 0.0, "different house number"
    ta, tb = set(a.split()[1:]), set(b.split()[1:])
    sa, sb = ta & ADDRESS_TOKENS, tb & ADDRESS_TOKENS
    if sa and sb and sa != sb:
        blocker = "street type differs"
    elif (ta & DIRECTIONS) != (tb & DIRECTIONS) and (ta & DIRECTIONS) and (tb & DIRECTIONS):
        blocker = "direction differs"
    else:
        blocker = None
    score = fuzz.ratio(nz.street_name(a) or "", nz.street_name(b) or "")
    return score, blocker


def _city_ok(a: Rec, b: Rec) -> bool:
    if a.zip and b.zip:
        return a.zip == b.zip
    return not (a.city and b.city) or a.city == b.city


# ---------------------------------------------------------------------------
# The main matching run
# ---------------------------------------------------------------------------
@dataclass
class Review:
    kind: str
    left_ref: str
    right_ref: str
    left_label: str
    right_label: str
    score: float
    reason: str

    @property
    def review_id(self) -> str:
        pair = "|".join(sorted((self.left_ref, self.right_ref)))
        return "rev-" + hashlib.sha1(f"{self.kind}|{pair}".encode()).hexdigest()[:12]


@dataclass
class PropertyResult:
    links: list[dict] = field(default_factory=list)       # property_links rows
    properties: list[dict] = field(default_factory=list)  # properties rows
    reviews: list[Review] = field(default_factory=list)
    record_property: dict[str, str] = field(default_factory=dict)   # "table:id" -> property_id


def decisions(con, kind: str) -> dict[frozenset, str]:
    """A person's yes/no answers from match_review."""
    if not table_exists(con, "match_review"):
        return {}
    return {frozenset((l, r)): d for l, r, d in con.execute(
        "SELECT left_ref, right_ref, decision FROM match_review WHERE kind = ? AND decision IN ('yes','no')",
        [kind]).fetchall()}


def _label(r: Rec) -> str:
    bits = [r.name_raw, r.street_raw, (r.city or "").title() or None, r.state]
    return ", ".join(b for b in bits if b) or r.ref


def match_properties(con, cfg: dict) -> PropertyResult:
    recs = load_records(con)
    decided = decisions(con, "property")
    uf = UnionFind()
    method: dict[str, tuple[str, float]] = {}
    reviews: list[Review] = []

    # 1-2. Exact parcel / address keys.
    key_users: Counter = Counter()
    for r in recs:
        node = f"rec:{r.ref}"
        uf.find(node)
        for k in r.keys():
            uf.union(node, k)
            key_users[k] += 1
    for r in recs:
        for k in r.keys():
            if key_users[k] > 1:
                kind = "parcel" if k.startswith("parcel:") else "address"
                if r.ref not in method or method[r.ref][0] == "address":
                    method[r.ref] = (kind, 100.0)

    # 3. Near-identical streets: same state, same house number.
    a_cfg = cfg["address"]
    by_num: dict[tuple, dict[str, Rec]] = defaultdict(dict)
    for r in recs:
        if r.street:
            by_num[(r.state, nz.house_number(r.street))].setdefault(r.street, r)
    for bucket in by_num.values():
        streets = sorted(bucket)
        for i, sa in enumerate(streets):
            for sb in streets[i + 1:]:
                ra, rb = bucket[sa], bucket[sb]
                if uf.find(f"addr:{ra.state}|{sa}") == uf.find(f"addr:{rb.state}|{sb}"):
                    continue
                score, blocker = compare_streets(sa, sb)
                said = decided.get(frozenset((ra.ref, rb.ref)))
                if said == "no" or not _city_ok(ra, rb):
                    continue
                if said == "yes" or (score >= a_cfg["auto"] and not blocker):
                    uf.union(f"addr:{ra.state}|{sa}", f"addr:{rb.state}|{sb}")
                    m = ("manual", 100.0) if said == "yes" else ("fuzzy_address", score)
                    for x in (ra, rb):
                        method.setdefault(x.ref, m)
                elif score >= a_cfg["review"]:
                    reviews.append(Review("property", ra.ref, rb.ref, _label(ra), _label(rb), score,
                                          blocker or "street names are similar but not identical"))

    # 4. Records with no address or parcel: match by property name.
    n_cfg = cfg["property_name"]
    located = [r for r in recs if r.keys()]
    for r in recs:
        if r.keys() or not r.name:
            continue
        best, best_score = None, 0.0
        for o in located:
            if o.state != r.state or not o.name or not _city_ok(r, o):
                continue
            s = fuzz.token_sort_ratio(r.name, o.name)
            if s > best_score:
                best, best_score = o, s
        if not best:
            continue
        said = decided.get(frozenset((r.ref, best.ref)))
        if said == "yes" or (said != "no" and best_score >= n_cfg["auto"]):
            uf.union(f"rec:{r.ref}", f"rec:{best.ref}")
            method[r.ref] = ("manual", 100.0) if said == "yes" else ("property_name", best_score)
        elif said != "no" and best_score >= n_cfg["review"]:
            reviews.append(Review("property", r.ref, best.ref, _label(r), _label(best), best_score,
                                  "no address; property names are similar"))

    # Build the output.
    res = PropertyResult(reviews=reviews)
    by_ref = {r.ref: r for r in recs}
    for root, members in uf.groups().items():
        refs = [m[4:] for m in members if m.startswith("rec:")]
        keys = [m for m in members if not m.startswith("rec:")]
        group = [by_ref[x] for x in refs]
        if not group:
            continue
        if not keys and not any(g.name and g.city for g in group):
            continue          # no address, no parcel, no name+city: we can't say which building
        pid = property_id_for(keys, f"rec:{min(refs)}")
        for g in group:
            m, s = method.get(g.ref, ("alone", 100.0))
            res.links.append({"property_id": pid, "record_table": g.table, "record_id": g.rid,
                              "match_method": m, "match_score": round(s, 1)})
            res.record_property[g.ref] = pid
        res.properties.append(_property_row(pid, group))
    return res


def _most_common(values):
    vals = [v for v in values if v]
    return Counter(vals).most_common(1)[0][0] if vals else None


def _property_row(pid: str, group: list[Rec]) -> dict:
    by_table = defaultdict(list)
    for g in group:
        by_table[g.table].append(g)
    # Names: the Scout's project name first, then CMBS, then the Reader.
    name = (_most_common(g.name_raw for g in by_table["projects"])
            or _most_common(g.name_raw for g in by_table["loans"])
            or _most_common(g.name_raw for g in by_table["filing_fields"]))
    return {
        "property_id": pid, "property_name": name,
        "street_address": _most_common(g.street_raw for g in group),
        "city": (_most_common(g.city for g in group) or "").title() or None,
        "state": _most_common(g.state for g in group if g.state != "??"),
        "zip": _most_common(g.zip for g in group),
        "county": _most_common(g.county for g in group),
        "parcel_number": _most_common(g.parcel_raw for g in group),
        "property_type": _most_common(g.ptype for g in group),
        "record_count": len(group),
        "sources": ", ".join(sorted(by_table)),
    }


def review_rows(reviews: list[Review]) -> list[dict]:
    return [{"review_id": r.review_id, "kind": r.kind, "left_ref": r.left_ref, "right_ref": r.right_ref,
             "left_label": r.left_label, "right_label": r.right_label, "score": round(r.score, 1),
             "reason": r.reason} for r in reviews]


def dumps(x) -> str:
    return json.dumps(x, default=str)
