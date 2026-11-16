"""
agent_bridge.py - fold the weekly agent pass's verified output into the research layer.

The weekly pass (agent/weekly.py) is the project's autonomous discovery/read/extract
engine: the agent searches the web, fetches pages, and agent/tools.record_evidence refuses
any fact without proof (a verbatim quote on the fetched page, or a re-run SELECT). This
bridge carries that output across - under the method's guardrails, which are stricter than
"the agent verified a quote":

  * IDENTITY: a goal's [property_id=...] marker is ROUTING context, never verified identity.
    A bridged fact attaches to that property only when the content CORROBORATES it (see
    research/binding.py): the saved page or quote must show a strong element - the
    property's name, street or parcel - PLUS a second independent element (city, county,
    ZIP...). One bare mention is not identity: facilities share names and streets repeat
    across towns. Even a corroborated bind is CONTENT identity only: the fact attaches to
    the property but keeps identity_method name_only_unverified, so a web page never
    clears the identity_verified gate - verified identity comes from record-key lanes
    (CCN, parcel, county address). A fact that cannot be bound is stored UNATTACHED
    (property_key NULL, identity unmatched) with the routing context kept in
    coverage_limit - it stays visible, and it can never satisfy a gate. A misattributed
    claim is never propagated.
  * CLAIM TYPES: the agent's claim name and its qualifiers travel unchanged
    (operating_noi stays operating_noi, a ratio stays a ratio). Agent-fetched money figures
    become financial_fact with units unconfirmed and an as-reported caveat - but their
    source_name is agent_research, which is NOT a primary financial source, so a web
    figure the agent found can never clear the primary_financial_source gate. Direction
    stays NEUTRAL: the bridge carries facts, it does not judge them.
  * PERIOD: period_end / period_months are taken from the evidence row's own notes when
    the agent stated them; when it did not, the period is UNKNOWN and the caveat says so.
    Nothing is invented.
  * SEARCHES: every web_search step is recorded as an ATTEMPT (lane "attempt:<guessed>").
    An attempt records that a query ran - it is NOT a qualified check: it has no bound
    jurisdiction, period or identity, so attempt rows NEVER satisfy the counter-evidence
    gates. A query containing the words "tax" or "lien" is not a completed tax or lien
    check. Qualified checks enter through county_tax.record_tax_year /
    record_lien_search (see the agent's record_research_check tool), which record the
    jurisdiction, the period and the identity explicitly. Failed fetches are recorded as
    attempts too: a failed fetch means UNKNOWN, and unknown never clears a gate.
  * CONTRADICTIONS: only COMPATIBLE figures can contradict - same property, same claim
    field, same units and the SAME stated period (a 2023 figure and a 2025 figure are two
    periods, not a disagreement; an unstated period can contradict nothing). When
    compatible figures disagree, a conflict is opened with both sides and their
    provenance, and the property is HELD (no-score) until a person resolves it.

Nothing here scores, tiers or publishes; it records. Rerunning replaces, never duplicates.
"""
from __future__ import annotations

import re

from collectors.common.log import get_logger
from matcher.schema import table_exists

from . import binding, evidence as ev
from .schema import create_research_tables

log = get_logger("research.agent_bridge")

# Agent claim names (agent/tools.record_evidence) that are dated money figures.
FINANCIAL_CLAIMS = {"operating_noi", "operating_revenue", "operating_expenses", "asking_rent"}
# Units per claim name - the qualifier travels with the claim, never flattened to "money".
CLAIM_UNITS = {"operating_noi": "usd_unconfirmed", "operating_revenue": "usd_unconfirmed",
               "operating_expenses": "usd_unconfirmed", "asking_rent": "usd_unconfirmed",
               "market_rent_index": "index", "occupancy_rate": "ratio",
               "market_occupancy": "ratio", "permit_inspection": "count"}

AGENT_FINANCIAL_CAVEAT = ("figure extracted by the research agent from a fetched public page; "
                          "as-reported and not audited; units unconfirmed; a web figure is context, "
                          "not a primary financial source (cost report or tax record)")
AGENT_DESCRIPTIVE_CAVEAT = ("claim extracted by the research agent and verified against a fetched "
                            "page (verbatim quote on file); descriptive - not proof of distress "
                            "or of its absence")
AGENT_DB_CAVEAT = ("claim recorded by the research agent and verified by re-running its SELECT "
                   "against the project database")
ATTEMPT_LIMIT = ("an attempted search is an attempt, not a qualified check: it records no bound "
                 "jurisdiction, period or identity, and it never satisfies a counter-evidence "
                 "gate; lane guessed from the query wording")
UNBOUND_LIMIT = ("goal marker is routing context, not verified identity: the page and quote "
                 "do not corroborate the property (a strong element - name, street or parcel - "
                 "plus a second independent element), so this fact is held UNATTACHED rather "
                 "than risk a misattributed claim")
PERIOD_UNKNOWN_CAVEAT = "reporting period unknown (the source row does not state one)"


def _marker(goal: str | None) -> str | None:
    """The [property_id=...] marker a weekly goal carries - routing context only."""
    if goal and "[property_id=" in goal:
        return goal.split("[property_id=", 1)[1].split("]", 1)[0].strip() or None
    return None


def _lane_for(query: str) -> str:
    q = (query or "").lower()
    if "tax" in q:
        return "county_tax"
    if "lien" in q:
        return "liens"
    if any(w in q for w in ("lawsuit", "docket", "court", "foreclos")):
        return "court_docket"
    if any(w in q for w in ("news", "press", "article")):
        return "press"
    return "web_research"


def _universe_elements(con, property_id: str | None) -> dict[str, str]:
    """The property's own identity elements, for the corroborated content-binding check."""
    if not (property_id and table_exists(con, "properties")):
        return {}
    row = con.execute("SELECT property_name, street_address, city, state, county, zip, "
                      "parcel_number FROM properties WHERE property_id = ?",
                      [property_id]).fetchone()
    return binding.elements_from_row(row) if row else {}


_PERIOD_END_RE = re.compile(r"period_end[:=\s]+(\d{4}-\d{2}-\d{2})", re.I)
_PERIOD_MONTHS_RE = re.compile(r"period_months[:=\s]+(\d+(?:\.\d+)?)", re.I)


def _stated_period(notes: str | None) -> tuple[str | None, float | None]:
    """(period_end, period_months) the agent stated in its notes, or (None, None)."""
    if not notes:
        return None, None
    end = _PERIOD_END_RE.search(notes)
    months = _PERIOD_MONTHS_RE.search(notes)
    return (end.group(1) if end else None, float(months.group(1)) if months else None)


def _compatible(old, new) -> bool:
    """Only same-period, same-scope, same-units figures can contradict. old/new are
    (period_end, period_months, units). Differing or unstated period ends, durations or
    units are different contexts, not disagreements: an annual figure and a quarterly
    figure ending the same date never 'disagree', and an unknown duration can contradict
    nothing."""
    o_end, o_months, o_units = old
    n_end, n_months, n_units = new
    if not (o_end and n_end) or str(o_end) != str(n_end):
        return False
    if not (o_months and n_months) or float(o_months) != float(n_months):
        return False
    return (o_units or None) == (n_units or None)


def _open_contradiction(con, *, property_key: str, field_name: str, new_id: str,
                        new_value: float, new_source: str, new_period_end, new_months,
                        new_units) -> None:
    """A bridged figure that disagrees with a COMPATIBLE stored one opens a conflict: both
    sides and their provenance, held (no-score) until a person resolves it.
    A conflict a person RESOLVED stays resolved while the underlying evidence is unchanged
    (a weekly retry re-syncs the same rows); only genuinely changed evidence re-opens it."""
    rows = con.execute(
        "SELECT evidence_id, value_number, source_name, dataset_vintage, period_end, "
        "period_months, units FROM research_evidence WHERE property_key = ? "
        "AND claim_type = 'financial_fact' AND field_name = ? "
        "AND value_number IS NOT NULL AND evidence_id <> ?",
        [property_key, field_name, new_id]).fetchall()
    for old_id, old_value, old_source, old_vintage, old_period_end, old_months, old_units in rows:
        if not _compatible((old_period_end, old_months, old_units),
                           (new_period_end, new_months, new_units)):
            continue
        if abs(old_value - new_value) <= max(1e-9, 0.001 * abs(old_value)):
            continue
        detail = (f"{field_name} for period ending {new_period_end}: {old_value} per "
                  f"{old_source} ({old_vintage}, {old_id}) vs {new_value} per {new_source} "
                  f"(agent run, {new_id}); same property, field, units and period - a person "
                  f"must reconcile; the property is held, never scored over")
        conflict_id = f"bridge:{property_key}:{field_name}"
        existing = con.execute("SELECT status, detail, resolution FROM research_conflicts "
                               "WHERE conflict_id = ?", [conflict_id]).fetchone()
        if existing and existing[0] == "resolved":
            if existing[1] == detail:
                continue            # unchanged evidence: a person's resolution stands
            detail += (f" (re-opened: the evidence changed after the earlier resolution "
                       f"'{existing[2]}')")
        ev.set_conflict(con, conflict_id=conflict_id, topic="value", detail=detail,
                        property_key=property_key)


def sync_run(con, run_id: str, *, property_key: str | None, page_text_fn=None) -> tuple[int, int]:
    """Carry one agent run's evidence + searches into the research tables.

    property_key is the goal marker (routing). page_text_fn(file_id) -> saved page text or
    None; pass agent.tools' stored-page reader in production, a stub in tests.
    Returns (evidence_rows, search_rows) written."""
    create_research_tables(con)
    elements = _universe_elements(con, property_key)
    written_e = written_s = 0
    for (eid, claim, value, source_kind, file_id, source_url, quote, observed_at, notes) in con.execute(
            "SELECT evidence_id, claim, value, source_kind, file_id, source_url, quote, observed_at, "
            "notes FROM evidence WHERE run_id = ?", [run_id]).fetchall():
        # Independent content binding: the saved page or the quote must mention the
        # property. Without it the fact stays unattached and can never clear a gate.
        page_text = page_text_fn(file_id) if (file_id and page_text_fn) else None
        bound = bool(elements) and binding.corroborated_bind(elements, page_text, quote)
        if bound:
            # Content binding attaches the fact to the property, but a web page's
            # corroborated mention is NOT a record-key identity: identity_method stays
            # name_only_unverified, so a fetched page can never clear identity_verified.
            # Verified identity comes from record-key lanes (CCN / parcel / county
            # address - e.g. a qualified record_research_check).
            method, ikey = "name_only_unverified", None
            key, limit = property_key, ("attached by corroborated content binding (strong element "
                                        "plus a second detail on the fetched page); single source "
                                        "as fetched by the agent; one page is not the whole record")
        else:
            method, ikey = "unmatched", None
            key, limit = None, UNBOUND_LIMIT + f"; goal routing: property_id={property_key}"
        financial = claim in FINANCIAL_CLAIMS
        try:
            number = float(value) if value is not None else None
        except (TypeError, ValueError):
            number = None
        period_end, period_months = _stated_period(notes)
        caveat = ((AGENT_FINANCIAL_CAVEAT if financial else AGENT_DESCRIPTIVE_CAVEAT)
                  if source_kind == "web_page" else AGENT_DB_CAVEAT)
        if financial and period_end is None:
            caveat += "; " + PERIOD_UNKNOWN_CAVEAT
        ev_id = f"agent:{eid}"
        ev.insert_evidence(con, ev.Evidence(
            ev_id,
            claim_type="financial_fact" if financial else "descriptive_signal",
            source_name="agent_research", field_name=claim,
            property_key=key, direction="neutral",
            source_url=source_url, file_id=file_id,
            dataset_vintage=f"agent run {run_id}",
            as_of=observed_at.date() if observed_at else None,
            identity_method=method, identity_key=ikey,
            period_end=period_end, period_months=period_months,
            units=CLAIM_UNITS.get(claim, "count"),
            unit_confidence="unconfirmed",
            value_number=number if financial else None,
            value_text=(quote or "")[:500] or (f"{claim}: {value}" if value is not None else claim),
            caveat=caveat, coverage_limit=limit))
        written_e += 1
        if bound and financial and number is not None:
            _open_contradiction(con, property_key=property_key, field_name=claim,
                                new_id=ev_id, new_value=number, new_source="agent_research",
                                new_period_end=period_end, new_months=period_months,
                                new_units=CLAIM_UNITS.get(claim))
    for (step, name, detail) in con.execute(
            "SELECT step, name, detail FROM agent_steps WHERE run_id = ? AND kind = 'tool_call' "
            "AND name = 'web_search' ORDER BY step", [run_id]).fetchall():
        query = (detail or "").strip()
        if not query:
            continue
        ev.record_search(con, property_key=property_key, identity_key=None,
                         lane=f"attempt:{_lane_for(query)}", query=query, result_count=None,
                         coverage_limit=ATTEMPT_LIMIT,
                         notes=f"agent run {run_id} step {step}")
        written_s += 1
    for (step, detail) in con.execute(
            "SELECT step, detail FROM agent_steps WHERE run_id = ? AND kind = 'tool_result' "
            "AND name = 'fetch_page' ORDER BY step", [run_id]).fetchall():
        if '"ok": false' not in (detail or "") and '"ok":false' not in (detail or ""):
            continue
        ev.record_search(con, property_key=property_key, identity_key=None,
                         lane="attempt:fetch", query=(detail or "")[:200], result_count=None,
                         coverage_limit=ATTEMPT_LIMIT,
                         notes=f"agent run {run_id} step {step}: fetch FAILED - the result is "
                               "unknown, and unknown never clears a gate")
        written_s += 1
    return written_e, written_s


def sync_pass(con, *, since, page_text_fn=None) -> dict:
    """Carry every agent run started at/after `since` (one weekly pass) into the research
    tables. Returns counts for the pass summary."""
    create_research_tables(con)
    totals = {"runs": 0, "evidence": 0, "searches": 0}
    for run_id, goal in con.execute(
            "SELECT run_id, goal FROM agent_runs WHERE started_at >= ?", [since]).fetchall():
        e, s = sync_run(con, run_id, property_key=_marker(goal), page_text_fn=page_text_fn)
        totals["runs"] += 1
        totals["evidence"] += e
        totals["searches"] += s
    log.info("agent bridge: %d runs -> %d evidence rows, %d search attempt rows",
             totals["runs"], totals["evidence"], totals["searches"])
    return totals
