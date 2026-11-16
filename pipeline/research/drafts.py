"""
drafts.py - close the loop: turn gate outcomes into structured assessment DRAFTS in an
ISOLATED store (research_drafts), validated by the existing assessment lane's own contract
(agent/ingest.validate_assessment_row) but never written to it.

Why isolated: research_assessments is the live store the dashboard reads. An auto-draft
there could demote or bury a person's reviewed assessment, inflate counts, or look like a
reviewed judgment. So drafts land in the research layer's own table; promoting one into
research_assessments is a separate, person-reviewed step this layer does not perform.

What a draft is and is not:
  * It is an UNSCORED marker: ready_for_review properties get a "gates met" draft, HELD
    properties (open conflicts) get a held draft naming the conflict. No research_score,
    no Hot/Watch/Low tier, ever.
  * The tier judgment rubric itself is NOT recoverable from this repository: the code
    labels the lane "rubric v2" (agent/ingest.py) while the dashboard copy says "rubric
    v3.3", and neither version's actual criteria are recorded anywhere in the repo. That
    unknown is stated in every draft's coverage text instead of being guessed.
  * Properties only reachable by an identity key (no universe property) get nothing: the
    lane's contract requires a real property_id, and a draft for an unidentified building
    would be a misattribution risk.

Rerunning is safe: a property whose latest draft already says the same thing is skipped,
so weekly passes do not pile up identical markers.
"""
from __future__ import annotations

import json
import uuid

from collectors.common.db import utc_now
from collectors.common.log import get_logger

from .gates import GATE_NAMES
from .schema import create_research_tables

log = get_logger("research.drafts")

RUBRIC_UNKNOWN = ("the Hot/Watch/Low tier rubric is not codified in this repository (the lane "
                  "is labelled 'rubric v2' in agent/ingest.py and 'rubric v3.3' in the dashboard "
                  "copy; neither version's criteria are recorded), so no tier or score is "
                  "assigned - the judgment stays with a person")

READY_ONE_LINE = "Research gates met - ready for review (no score assigned)"
HELD_ONE_LINE = "Held: unresolved conflict between sources (no score)"


def _latest_draft(con, property_key: str):
    return con.execute(
        "SELECT tier_label, one_line FROM research_drafts WHERE property_key = ? "
        "ORDER BY created_at DESC LIMIT 1", [property_key]).fetchone()


def _draft_row(status_row) -> dict | None:
    """The assessment-shaped draft row for one research_status row, or None."""
    key, depth, hold_reason, notes, ev_count = status_row
    if key.startswith("identity:"):          # no universe property: the contract cannot take it
        return None
    if depth == "ready_for_review":
        kind, tier = "ready", "Unscored (research gates met - draft)"
        return {"property_id": key, "research_tier": tier, "_kind": kind,
                "confidence": "structural gates only",
                "coverage": (f"all {len(GATE_NAMES)} structural gates passed on {ev_count} stored "
                             f"facts; county collector coverage remains a person's routing; "
                             + RUBRIC_UNKNOWN),
                "score_reasons": [f"gate passed: {g}" for g in GATE_NAMES],
                "one_line": READY_ONE_LINE}
    if depth == "held":
        kind, tier = "held", "Unscored (held - open conflict)"
        return {"property_id": key, "research_tier": tier, "_kind": kind,
                "confidence": "held",
                "coverage": ("a conflict between sources is open; the property stays no-score "
                             "until a person reconciles it. " + RUBRIC_UNKNOWN),
                "score_reasons": [f"open conflict: {hold_reason}"] if hold_reason else [],
                "one_line": f"{HELD_ONE_LINE}: {hold_reason}" if hold_reason else HELD_ONE_LINE}
    return None


def prepare_drafts(con) -> dict:
    """Write Unscored assessment drafts for ready/held properties into the ISOLATED
    research_drafts store. Each row is validated against the existing assessment lane's
    contract (agent.ingest.validate_assessment_row); invalid rows are skipped, never
    forced. The live research_assessments table is never touched.
    Returns {"drafts": n, "skipped_unchanged": m, "invalid": k}."""
    create_research_tables(con)
    from agent.ingest import validate_assessment_row
    out = {"drafts": 0, "skipped_unchanged": 0, "invalid": 0}
    rows = con.execute(
        "SELECT property_key, depth, hold_reason, notes, evidence_count FROM research_status "
        "WHERE depth IN ('ready_for_review', 'held')").fetchall()
    for status_row in rows:
        row = _draft_row(status_row)
        if row is None:
            continue
        kind = row.pop("_kind")
        problems = validate_assessment_row(con, row)
        if problems:
            log.info("draft for %s invalid under the assessment contract: %s",
                     row["property_id"], "; ".join(problems))
            out["invalid"] += 1
            continue
        latest = _latest_draft(con, row["property_id"])
        if latest and latest[0] == row["research_tier"] and latest[1] == row["one_line"]:
            out["skipped_unchanged"] += 1
            continue
        con.execute(
            "INSERT INTO research_drafts (draft_id, property_key, draft_kind, tier_label, "
            "confidence, coverage, score_reasons, one_line, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [f"draft-{uuid.uuid4().hex[:12]}", row["property_id"], kind, row["research_tier"],
             row["confidence"], row["coverage"], json.dumps(row["score_reasons"]),
             row["one_line"], utc_now()])
        out["drafts"] += 1
    log.info("assessment drafts: %d written, %d unchanged, %d invalid",
             out["drafts"], out["skipped_unchanged"], out["invalid"])
    return out
