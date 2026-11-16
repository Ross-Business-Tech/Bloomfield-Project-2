"""
evidence.py - the fact record every research lane writes, plus the guardrail caveats.

The method in one paragraph: every fact is stored with the source it was read from
(source URL + dataset vintage), the identity key used to attach it, the exact period it
covers, its units and how confident we are in them, the source's own row id, the limit of
what the source can tell us, and the caveat that must travel with the fact. Missing data
is stored as unknown - and unknown is never treated as a healthy sign.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date

from collectors.common.db import utc_now

from .identity import IDENTITY_METHODS

CLAIM_TYPES = (
    "descriptive_signal",   # CMS quality/enforcement/utilization facts: they describe, they never prove financial distress
    "financial_fact",       # cost-report figures, tax paid/delinquent: dated primary financial records
    "entity_link",          # ownership/role record entries: links on file, nothing more
    "allegation_unread",    # a case or complaint known to exist but not yet read
    "lead_unqualified",     # press/firm allegations or name matches not yet tied to a universe property
)

DIRECTIONS = (
    "distress",             # points toward distress (with its caveat attached)
    "counter",              # points the OTHER way - real counter-evidence, e.g. taxes paid current
    "neutral",              # context only
)

UNIT_CONFIDENCE = ("confirmed", "unconfirmed")

# --- The guardrails: what each kind of fact means, and what it must never be said to mean.
# Every lane uses these exact sentences as caveats, so the discipline survives copy-paste.
FINE_AMOUNT_CAVEAT = ("assessed dollars exactly as published; CMS gives no paid or appeal status, "
                      "so this is never described as unpaid debt")
PENALTY_DATE_CAVEAT = "the penalty date is the date of the inspection that triggered the penalty"
DENIAL_DAYS_CAVEAT = ("days Medicare/Medicaid payment for NEW admissions was suspended; "
                      "not lost revenue and not a dollar figure")
UTILIZATION_CAVEAT = ("average residents per day divided by certified beds; descriptive only - "
                      "not staffed occupancy, and cost-report bed-days can be inconsistent with bed counts")
STARS_CAVEAT = "CMS quality rating; a care-quality signal, not evidence of financial distress"
OWNERSHIP_SINCE_CAVEAT = ("the 'since' date is when the role was listed, not proof of a transfer "
                          "or of entity continuity")
SHARED_NAME_CAVEAT = "a shared listed person, role or mailing address is not common ownership"
COST_REPORT_CAVEAT = ("as-reported historical figures; a reported loss is not cash distress, and a "
                      "historical report is not evidence of current distress")
PARTIAL_YEAR_CAVEAT = "partial-year period kept exactly as stated; never annualized"
ONE_ROW_CHECK_CAVEAT = ("one row per facility per file kept as a recorded check; the file has no "
                        "final/amended status field, so this is not proof the row is the final report")
TEMPORAL_GROUNDING_CAVEAT = ("historical event as recorded; not assigned to the current owner or "
                             "operator without temporal entity grounding")
QUERY_BOUNDED_CAVEAT = "the result is bounded to the exact query string used"
DOCKET_UNREAD_CAVEAT = "case existence only; the complaint has not been read"
LEAD_UNQUALIFIED_CAVEAT = "unattributed lead until a source ties the entity to a universe property"
MISSING_IS_UNKNOWN = "missing data is recorded as unknown; unknown is never treated as healthy"


def months_between(start: date, end: date) -> float:
    """Length of a stated period in months, rounded to one decimal. Never an annualization."""
    return round((end - start).days / 30.44, 1)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60]


@dataclass
class Evidence:
    """One fact, with everything needed to judge it. See the table definition for field meanings."""
    evidence_id: str
    claim_type: str
    source_name: str
    field_name: str
    property_key: str | None = None
    direction: str = "neutral"
    source_url: str | None = None
    dataset_vintage: str | None = None
    as_of: date | None = None
    identity_method: str = "unmatched"
    identity_key: str | None = None
    period_begin: date | None = None
    period_end: date | None = None
    period_months: float | None = None
    units: str | None = None
    unit_confidence: str = "unconfirmed"
    raw_record_id: str | None = None
    worksheet_ref: str | None = None
    value_number: float | None = None
    value_text: str | None = None
    caveat: str | None = None
    coverage_limit: str | None = None
    file_id: str | None = None

    def __post_init__(self):
        if self.period_months is None and self.period_begin and self.period_end:
            self.period_months = months_between(self.period_begin, self.period_end)

    def validate(self) -> list[str]:
        """What is wrong with this record, in plain words. An empty list means it is storable."""
        problems = []
        if self.claim_type not in CLAIM_TYPES:
            problems.append(f"claim_type must be one of {CLAIM_TYPES}")
        if self.direction not in DIRECTIONS:
            problems.append(f"direction must be one of {DIRECTIONS}")
        if self.identity_method not in IDENTITY_METHODS:
            problems.append(f"identity_method must be one of {IDENTITY_METHODS}")
        if not (self.source_url or self.file_id or self.dataset_vintage):
            problems.append("no provenance: a fact needs a source URL, a stored file, or a dataset vintage")
        if self.claim_type == "financial_fact" and not (self.units and self.caveat):
            problems.append("a financial fact needs its units and its caveat stored with it")
        if self.claim_type in ("allegation_unread", "lead_unqualified") and not self.coverage_limit:
            problems.append("an unread allegation or unqualified lead needs its coverage limit stated")
        if self.value_number is None and self.value_text is None:
            problems.append("no value stored")
        return problems


_COLUMNS = ["evidence_id", "property_key", "claim_type", "direction", "source_name", "source_url",
            "dataset_vintage", "as_of", "identity_method", "identity_key", "period_begin", "period_end",
            "period_months", "units", "unit_confidence", "raw_record_id", "field_name", "worksheet_ref",
            "value_number", "value_text", "caveat", "coverage_limit", "file_id", "collected_at"]


def insert_evidence(con, e: Evidence) -> None:
    """Store one fact, refusing records that break the method (see validate())."""
    problems = e.validate()
    if problems:
        raise ValueError(f"evidence {e.evidence_id!r}: " + "; ".join(problems))
    values = [getattr(e, c) if c != "collected_at" else utc_now() for c in _COLUMNS]
    con.execute(
        f"INSERT OR REPLACE INTO research_evidence ({', '.join(_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _COLUMNS)})", values)


def record_search(con, *, property_key=None, identity_key=None, lane: str, query: str,
                  result_count: int | None, coverage_limit: str = QUERY_BOUNDED_CAVEAT,
                  notes: str | None = None) -> str:
    """
    Record that a lookup happened - including the ones that found nothing.
    "We searched for counter-evidence and found none" is a fact with a row here.
    """
    search_id = f"search-{utc_now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
    con.execute(
        "INSERT INTO research_searches (search_id, property_key, identity_key, lane, query, "
        "result_count, coverage_limit, searched_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [search_id, property_key, identity_key, lane, query, result_count, coverage_limit,
         utc_now(), notes])
    return search_id


def set_conflict(con, *, conflict_id: str, topic: str, detail: str, property_key=None,
                 identity_key=None, resolution: str | None = None) -> None:
    """
    Open a conflict (or re-open it with fresh detail). Pass resolution to mark it resolved.
    An open conflict holds its property out of review-ready research (see gates.py).
    """
    status = "resolved" if resolution else "open"
    con.execute(
        "INSERT OR REPLACE INTO research_conflicts (conflict_id, property_key, identity_key, topic, "
        "detail, status, resolution, opened_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [conflict_id, property_key, identity_key, topic, detail, status, resolution,
         utc_now(), utc_now() if resolution else None])


def resolve_conflict(con, conflict_id: str, resolution: str) -> bool:
    """Mark an open conflict resolved. Returns True if there was an open one to resolve."""
    cur = con.execute(
        "UPDATE research_conflicts SET status = 'resolved', resolution = ?, resolved_at = ? "
        "WHERE conflict_id = ? AND status = 'open'", [resolution, utc_now(), conflict_id])
    return (cur.fetchone() is None) and True  # duckdb rowcount not exposed; assume attempted


def slug(text: str) -> str:
    return _slug(text)
