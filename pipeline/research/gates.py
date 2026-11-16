"""
gates.py - the checks a property must pass before its research is ready for review.

The method, honestly stated: finding a source that mentions a property is not research.
The five gates this layer actually runs:

  1. identity_verified         at least one fact attached by CCN or parcel - a name-only
                               match never verifies identity
  2. primary_financial_source  at least one DATED primary financial record - a cost
                               report or a tax record from PRIMARY_FINANCIAL_SOURCES -
                               attached by that SAME verified identity. A financial row
                               with an unverified or missing identity does not count,
                               however official it looks, and neither does a figure the
                               research agent fetched from a web page: agent_research is
                               context, never a primary source.
  3. counter_evidence_searched the counter-evidence checks were run AND RECORDED in each
                               lane the method needs for the property: the county tax check
                               AND the lien search, each as a QUALIFIED check (bound
                               jurisdiction, period and identity - via county_tax's
                               recorders). Finding nothing still counts; never running one
                               lane does not. A bare search ATTEMPT (lane 'attempt:...' -
                               e.g. a web query containing the word 'tax') is recorded for
                               honesty but never satisfies this gate. One paid tax row is
                               not the whole counter-evidence method.
  4. caveats_stored            every financial fact carries its units and its qualifier
  5. no_open_conflicts         no unresolved disagreement between sources

Anything with evidence but unmet gates is PARTIAL, and the report names the unmet lanes.
Anything with an open conflict is HELD until a person resolves it - a conflict is never
scored over or averaged away. Depth is derived from what is actually in the tables, never
from a field someone remembered to fill in.

A property that passes all five gates is READY FOR REVIEW: minimum structural readiness,
not completed research. The live research lane's full-depth standard is not codified in
this repository (app-side assessments carry free-text confidence/coverage instead), so
this layer reports ready_for_review plus the unmet lanes rather than claiming a standard
it cannot recover. County collector coverage is itself unknown - which county sources are
reachable is not recorded anywhere in this repo - so the report keeps the county lanes
visible as work a person routes, never as coverage the code guarantees.

The gates say nothing about the Opportunity Score: scoring is the frozen equation's
business (scorer/), and this layer never writes to it.
"""
from __future__ import annotations

from collectors.common.db import utc_now

from .identity import VERIFIED_METHODS
from .schema import create_research_tables

GATE_NAMES = ("identity_verified", "primary_financial_source", "counter_evidence_searched",
              "caveats_stored", "no_open_conflicts")

# The counter-evidence lanes the method needs run-and-recorded per property. Each lane is
# a QUALIFIED search row in research_searches (a 0-result row still counts as having run
# it). Rows whose lane starts with 'attempt:' are unqualified attempts (agent_bridge) and
# never satisfy a lane.
COUNTER_EVIDENCE_LANES = ("county_tax", "liens")

# The only sources whose dated figures count as a property's primary financial record.
# Agent-fetched web figures (agent_research) are context and never clear gate 2.
PRIMARY_FINANCIAL_SOURCES = ("cms_snf_cost_report", "county_tax")

# What a person still has to do when a gate fails (the "unmet lanes" the report exposes).
UNMET_LANE = {
    "identity_verified": "identity: confirm a CCN or parcel match (name-only matches stay unverified)",
    "primary_financial_source": "primary financials: pull a dated cost report or tax record "
                                "attached by the verified identity (an agent-fetched web figure "
                                "is context, not a primary source)",
    "counter_evidence_searched": "counter-evidence: run and record the county tax check AND "
                                 "the lien search as qualified checks with bound jurisdiction, "
                                 "period and identity (a find-nothing row counts; a bare search "
                                 "attempt or an unrun lane does not)",
    "caveats_stored": "caveats: store units and the qualifier on every financial fact",
    "no_open_conflicts": "conflicts: resolve the open disagreement between sources",
}

DEPTHS = ("lead", "partial", "held", "ready_for_review")

_GROUP = "coalesce(property_key, 'identity:' || identity_key)"


def _group_keys(con) -> list[str]:
    keys = set()
    for table in ("research_evidence", "research_searches", "research_conflicts"):
        keys |= {r[0] for r in con.execute(
            f"SELECT DISTINCT {_GROUP} FROM {table}").fetchall() if r[0]}
    return sorted(keys)


def evaluate_property(con, key: str) -> dict:
    """The five gates for one property, derived from the tables. See the module docstring."""
    rows = con.execute(
        f"SELECT claim_type, identity_method, caveat, units, as_of, period_end, source_name "
        f"FROM research_evidence WHERE {_GROUP} = ?", [key]).fetchall()
    conflict_rows = con.execute(
        f"SELECT topic, detail FROM research_conflicts WHERE {_GROUP} = ? AND status = 'open'",
        [key]).fetchall()
    searched_lanes = {r[0] for r in con.execute(
        f"SELECT DISTINCT lane FROM research_searches WHERE {_GROUP} = ?", [key]).fetchall()}

    verified = [r for r in rows if r[1] in VERIFIED_METHODS]
    fin = [r for r in rows if r[0] == "financial_fact"]
    # The financial record must hang on the verified identity AND come from a primary
    # source - a dated number tied to a name-only or unmatched key, or a figure the
    # research agent fetched from a web page, is not the property's primary financial
    # source.
    verified_fin = [r for r in fin if r[1] in VERIFIED_METHODS
                    and r[6] in PRIMARY_FINANCIAL_SOURCES]
    missing_lanes = [l for l in COUNTER_EVIDENCE_LANES if l not in searched_lanes]
    gates = {
        "identity_verified": bool(verified),
        "primary_financial_source": any(r[4] is not None or r[5] is not None
                                        for r in verified_fin),
        "counter_evidence_searched": not missing_lanes,
        "caveats_stored": all(r[2] and r[3] for r in fin),
        "no_open_conflicts": not conflict_rows,
    }
    unmet = [UNMET_LANE[g] for g in GATE_NAMES if not gates[g]]
    if missing_lanes:
        attempted = [l for l in missing_lanes if f"attempt:{l}" in searched_lanes]
        unmet.append("missing counter-evidence lanes: " + ", ".join(missing_lanes)
                     + ("; attempts recorded but unqualified (no bound jurisdiction/period/"
                        "identity): " + ", ".join(attempted) if attempted else ""))
    if conflict_rows:
        depth = "held"
    elif all(gates.values()):
        depth = "ready_for_review"
    elif rows:
        depth = "partial"
    else:
        depth = "lead"
    return {"property_key": key, "depth": depth, "gates": gates, "unmet": unmet,
            "open_conflicts": len(conflict_rows),
            "hold_reason": "; ".join(sorted({t for t, _d in conflict_rows})) or None,
            "evidence_count": len(rows)}


def evaluate_all(con) -> dict[str, int]:
    """Re-derive every property's depth and store it. Returns counts by depth.

    First links research identity keys to the matcher's universe property_ids
    (properties.resolve_all), so CMS rows and county rows for the same building are
    grouped under the same property_id the rest of the app uses.
    """
    create_research_tables(con)
    from . import properties
    properties.resolve_all(con)
    counts = {d: 0 for d in DEPTHS}
    for key in _group_keys(con):
        r = evaluate_property(con, key)
        counts[r["depth"]] += 1
        con.execute(
            "INSERT OR REPLACE INTO research_status (property_key, depth, identity_verified, "
            "primary_financial_source, counter_evidence_searched, caveats_stored, open_conflicts, "
            "hold_reason, evidence_count, last_evaluated_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [key, r["depth"], r["gates"]["identity_verified"], r["gates"]["primary_financial_source"],
             r["gates"]["counter_evidence_searched"], r["gates"]["caveats_stored"],
             r["open_conflicts"], r["hold_reason"], r["evidence_count"], utc_now(),
             " | ".join(r["unmet"]) or None])
    return counts


def readiness_for(con, property_key: str) -> dict | None:
    """The current research depth for one universe property (the matcher's property_id),
    or None if nothing is tracked for it. Derived live from the tables. Read-only: this is
    the assessment INPUT other parts of the app may consult - it never writes an
    assessment, a score, or a tier. Keys: depth, gates, unmet, open_conflicts,
    hold_reason, evidence_count.

    A property the matcher's universe knows but no research row tracks yet is a LEAD
    with every gate unmet (an honest 'nothing done'), not a silent None; None is
    reserved for keys the universe does not know at all."""
    create_research_tables(con)
    tracked = set(_group_keys(con))
    if property_key not in tracked:
        from matcher.schema import table_exists
        if table_exists(con, "properties") and con.execute(
                "SELECT 1 FROM properties WHERE property_id = ? LIMIT 1",
                [property_key]).fetchone():
            return evaluate_property(con, property_key)
        return None
    return evaluate_property(con, property_key)


def report(con) -> str:
    """The honest counts, the held properties, and the unmet lanes - never coverage dressed
    up as completed research."""
    create_research_tables(con)
    evaluate_all(con)
    rows = con.execute("SELECT property_key, depth, hold_reason, notes, evidence_count "
                       "FROM research_status ORDER BY depth, property_key").fetchall()
    counts = {d: 0 for d in DEPTHS}
    for _k, d, _h, _n, _e in rows:
        counts[d] += 1
    lines = [f"Research depth across {len(rows)} tracked properties:"]
    for d in DEPTHS:
        lines.append(f"  {d.replace('_', ' '):<16}{counts[d]}")
    held = [(k, h) for k, d, h, _n, _e in rows if d == "held"]
    if held:
        lines.append("Held (open conflicts - a person must resolve these):")
        for k, h in held[:20]:
            lines.append(f"  {k}: {h}")
    partial = [(k, n) for k, d, _h, n, _e in rows if d == "partial" and n]
    if partial:
        lines.append(f"Partial research - unmet lanes (first 10 of {len(partial)}):")
        for k, n in partial[:10]:
            lines.append(f"  {k}: {n}")
    lines.append("Note: a source match (a CMS row tied to a CCN, a name that looks right) is "
                 "not completed research. 'Ready for review' means minimum structural "
                 "readiness - every gate this layer knows passed - not full depth: the live "
                 "research lane's full-depth standard is not codified in this repo, and "
                 "which county sources are reachable is not recorded here, so county "
                 "coverage stays a person's routing decision. The score equation is "
                 "unchanged by any of this.")
    return "\n".join(lines)
