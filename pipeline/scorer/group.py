"""
group.py - gather every record about the same property into one "case file".

In plain English: before we can score a property we need all of its paperwork in one place:
its recorded loans, the documents the Reader read (liens, permits, notices), its CMBS loan
rows, and the Scout's project page.

  * If Part C (the Matcher) has written its `property_links` table, we use those links.
  * For anything Part C hasn't linked (or before Part C exists), we use a simple fallback:
    two records belong together when they share a parcel number or the same street address
    in the same state. This is deliberately cautious; fuzzy name matching is Part C's job.

Part C contract (what the scorer expects, if Part C provides it):
    property_links(property_id TEXT, record_table TEXT, record_id TEXT, ...)
    record_table is one of: recorded_loans, filing_fields, loans, projects
    record_id is that table's key: record_id, file_id, loan_key, project_id
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from matcher import normalize as nz

from .schema import table_exists

# Same cleanup and keys as Part C (matcher/normalize.py), so the fallback gives the same
# property_id that Part C would.
street_key = nz.street_key
parcel_key = nz.parcel_key


def record_keys(state, parcel, address) -> list[str]:
    st = nz.state_key(state)
    keys = []
    if (p := parcel_key(parcel)):
        keys.append(f"parcel:{st}|{p}")
    if (a := street_key(address)):
        keys.append(f"addr:{st}|{a}")
    return keys


@dataclass
class Property:
    """Everything we know about one property."""
    property_id: str
    grouped_by: str = "fallback"
    recorded_loans: list[dict] = field(default_factory=list)
    filings: list[dict] = field(default_factory=list)       # filing_fields + raw_files columns
    cmbs_loans: list[dict] = field(default_factory=list)
    projects: list[dict] = field(default_factory=list)

    def all_rows(self):
        return self.recorded_loans + self.filings + self.cmbs_loans + self.projects


def _rows(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load_records(con) -> dict[str, list[dict]]:
    """Read every input table the scorer uses (missing tables just give no rows)."""
    out = {"recorded_loans": [], "filing_fields": [], "loans": [], "projects": []}
    if table_exists(con, "recorded_loans"):
        out["recorded_loans"] = _rows(con, "SELECT * FROM recorded_loans")
    if table_exists(con, "filing_fields"):
        # Bring along Part A's labels for where the file came from. Failed reads are useless.
        out["filing_fields"] = _rows(con, """
            SELECT f.*, r.source AS raw_source, r.doc_category, r.state AS raw_state,
                   r.county AS raw_county, r.city AS raw_city, r.notes AS raw_notes
            FROM filing_fields f LEFT JOIN raw_files r USING (file_id)
            WHERE coalesce(f.status, 'ok') <> 'failed'""")
    if table_exists(con, "loans"):
        if table_exists(con, "loan_links"):
            # Part C's loan_group_id: the same loan reported by EDGAR and Trepp is scored once.
            out["loans"] = _rows(con, """SELECT l.*, k.loan_group_id FROM loans l
                                         LEFT JOIN loan_links k USING (loan_key)""")
        else:
            out["loans"] = _rows(con, "SELECT * FROM loans")
    if table_exists(con, "projects"):
        out["projects"] = _rows(con, "SELECT * FROM projects")
    return out


# The key column and the (state, parcel, address) used for fallback grouping, per table.
def _id_and_keys(table: str, row: dict) -> tuple[str, list[str]]:
    if table == "recorded_loans":
        return row["record_id"], record_keys(row.get("state"), row.get("parcel_number"),
                                             row.get("property_address"))
    if table == "filing_fields":
        return row["file_id"], record_keys(row.get("property_state") or row.get("raw_state"),
                                           row.get("parcel_number"), row.get("property_address"))
    if table == "loans":
        return row["loan_key"], record_keys(row.get("property_state"), None,
                                            row.get("property_address"))
    if table == "projects":
        return row["project_id"], record_keys(row.get("state"), None, row.get("property_address"))
    raise ValueError(table)


_BUCKET = {"recorded_loans": "recorded_loans", "filing_fields": "filings",
           "loans": "cmbs_loans", "projects": "projects"}


class _UnionFind:
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
            # Keep the alphabetically smaller root so results don't depend on load order.
            lo, hi = sorted((ra, rb))
            self.parent[hi] = lo


def _part_c_links(con) -> dict[tuple[str, str], str]:
    if not table_exists(con, "property_links"):
        return {}
    return {(t, str(r)): p for p, t, r in con.execute(
        "SELECT property_id, record_table, record_id FROM property_links").fetchall()}


def group_properties(con) -> list[Property]:
    """Build one Property per real-world property, from every input table."""
    records = load_records(con)
    links = _part_c_links(con)

    props: dict[str, Property] = {}
    uf = _UnionFind()
    pending: list[tuple[str, str, dict, list[str]]] = []   # fallback records

    for table, rows in records.items():
        for row in rows:
            rid, keys = _id_and_keys(table, row)
            if (table, str(rid)) in links:
                pid = links[(table, str(rid))]
                prop = props.setdefault(pid, Property(pid, grouped_by="part_c"))
                getattr(prop, _BUCKET[table]).append(row)
                continue
            if not keys:
                continue          # we can't tell which property this is about
            for k in keys[1:]:
                uf.union(keys[0], k)
            pending.append((table, rid, row, keys))

    # Make the fallback groups.
    for table, rid, row, keys in pending:
        root = uf.find(keys[0])
        pid = "prop-" + hashlib.sha1(root.encode()).hexdigest()[:12]
        prop = props.setdefault(pid, Property(pid))
        getattr(prop, _BUCKET[table]).append(row)

    return sorted(props.values(), key=lambda p: p.property_id)
