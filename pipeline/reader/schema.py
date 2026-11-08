"""
schema.py - the exact shape of what the Reader pulls out of each document.

In plain English: the AI returns a block of JSON. This file defines every field that block
may contain, which values are allowed for the "pick one" fields (filing type, property
type, ...), and cleans up values the AI formats loosely - "$1,250,000.00" becomes
1250000, "March 3, 2026" becomes 2026-03-03, "Michigan" becomes MI.

ExtractionOutput = what the AI returns.
FilingRecord     = that plus our bookkeeping (file_id, status, cost...), one filing_fields row.

If the AI's JSON breaks these rules, validation raises an error whose message is easy to
read - check.py sends it back to the AI once so it can fix its answer.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Allowed values
# ---------------------------------------------------------------------------
class FilingType(str, Enum):
    foreclosure_notice = "foreclosure_notice"
    lis_pendens = "lis_pendens"
    receivership = "receivership"
    lender_lawsuit = "lender_lawsuit"
    recorded_loan = "recorded_loan"
    mechanics_lien = "mechanics_lien"
    ucc_sale_notice = "ucc_sale_notice"
    auction_listing = "auction_listing"
    tax_delinquency = "tax_delinquency"
    license_action = "license_action"
    servicer_commentary = "servicer_commentary"
    building_permit = "building_permit"
    certificate_of_occupancy = "certificate_of_occupancy"
    project_website = "project_website"
    development_announcement = "development_announcement"
    news_article = "news_article"
    other = "other"


class PropertyType(str, Enum):
    multifamily = "multifamily"
    student_housing = "student_housing"
    office = "office"
    retail = "retail"
    industrial = "industrial"
    hotel = "hotel"
    senior_housing = "senior_housing"
    behavioral_health = "behavioral_health"
    mixed_use = "mixed_use"
    land = "land"
    other = "other"
    unknown = "unknown"


class LoanKind(str, Enum):
    construction = "construction"
    permanent = "permanent"
    extension = "extension"
    modification = "modification"
    release = "release"
    assignment = "assignment"
    unknown = "unknown"


class DistressTag(str, Enum):
    opening_delayed = "opening_delayed"
    stale_opening_date = "stale_opening_date"
    missed_lease_up = "missed_lease_up"
    construction_stalled = "construction_stalled"
    construction_loan_extended = "construction_loan_extended"
    construction_loan_past_maturity = "construction_loan_past_maturity"
    permit_expired = "permit_expired"
    permit_extended = "permit_extended"
    contractor_unpaid = "contractor_unpaid"
    listed_for_auction = "listed_for_auction"
    maturity_default = "maturity_default"
    payment_default = "payment_default"
    cash_management_triggered = "cash_management_triggered"
    dscr_below_1 = "dscr_below_1"
    major_tenant_leaving = "major_tenant_leaving"
    occupancy_decline = "occupancy_decline"
    special_servicing_transfer = "special_servicing_transfer"
    receiver_appointed = "receiver_appointed"
    foreclosure_scheduled = "foreclosure_scheduled"
    ucc_sale_scheduled = "ucc_sale_scheduled"
    tax_delinquent = "tax_delinquent"
    license_suspended = "license_suspended"
    license_violation = "license_violation"
    loan_modification = "loan_modification"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class ReviewStatus(str, Enum):
    ok = "ok"
    needs_review = "needs_review"
    failed = "failed"


def values(enum_cls) -> list[str]:
    """The allowed words for an enum, as plain strings (used to build the prompt)."""
    return [e.value for e in enum_cls]


# ---------------------------------------------------------------------------
# Cleaning helpers
# ---------------------------------------------------------------------------
_NULL_WORDS = {"", "null", "none", "n/a", "na", "unknown_value"}

_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
_STATE_CODES = set(_STATES.values())

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
                 "%d %B %Y")


def _blank_to_none(v: Any) -> Any:
    """Treat '', 'null', 'none', 'n/a' as missing; trim spaces; turn numbers into text."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        return None if s.lower() in _NULL_WORDS else s
    return v


def parse_amount(v: Any) -> Optional[float]:
    """'$1,250,000.00' -> 1250000.0. Missing -> None. Anything that is not one plain number is an error."""
    if v is None or isinstance(v, bool):
        if isinstance(v, bool):
            raise ValueError("an amount must be a number, not true/false")
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s.lower() in _NULL_WORDS:
        return None
    cleaned = re.sub(r"(?i)usd|dollars?|[$,\s]", "", s)
    if not re.fullmatch(r"-?\d+(\.\d+)?", cleaned):
        raise ValueError(f"'{v}' is not a plain dollar amount; write it as a number like 1250000")
    return float(cleaned)


def parse_date(v: Any) -> Optional[date]:
    """Many date styles -> a real date (shown as YYYY-MM-DD). Missing -> None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if s.lower() in _NULL_WORDS:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"'{v}' is not a date; write it as YYYY-MM-DD")


def _normalize_enum_word(v: Any) -> Any:
    if isinstance(v, str):
        return v.strip().lower().replace(" ", "_").replace("-", "_")
    return v


DATE_FIELDS = ["recording_date", "maturity_date", "filing_date", "sale_date", "auction_date",
               "permit_expiration_date"]
AMOUNT_FIELDS = ["loan_amount", "amount_owed", "lien_amount", "starting_bid"]
TEXT_FIELDS = ["borrower_name", "developer_name", "project_name", "property_address", "property_city",
               "property_zip", "parcel_number", "lender_name", "servicer_name", "lien_claimant",
               "lien_work", "permit_number", "permit_status", "stated_opening_phrase",
               "leasing_season", "case_number"]


# ---------------------------------------------------------------------------
# What the AI returns
# ---------------------------------------------------------------------------
class Party(BaseModel):
    name: str
    role: str


class ExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")          # ignore any extra keys the AI adds

    filing_type: FilingType
    borrower_name: Optional[str] = None
    developer_name: Optional[str] = None
    project_name: Optional[str] = None
    other_parties: list[Party] = Field(default_factory=list)
    property_address: Optional[str] = None
    property_city: Optional[str] = None
    property_state: Optional[str] = None
    property_zip: Optional[str] = None
    parcel_number: Optional[str] = None
    property_type: PropertyType = PropertyType.unknown
    lender_name: Optional[str] = None
    servicer_name: Optional[str] = None
    loan_kind: Optional[LoanKind] = None
    loan_amount: Optional[float] = None
    amount_owed: Optional[float] = None
    recording_date: Optional[date] = None
    maturity_date: Optional[date] = None
    filing_date: Optional[date] = None
    sale_date: Optional[date] = None
    auction_date: Optional[date] = None
    lien_claimant: Optional[str] = None
    lien_amount: Optional[float] = None
    lien_work: Optional[str] = None
    permit_number: Optional[str] = None
    permit_status: Optional[str] = None
    permit_expiration_date: Optional[date] = None
    stated_opening_phrase: Optional[str] = None
    page_still_unopened: Optional[bool] = None
    leasing_season: Optional[str] = None
    starting_bid: Optional[float] = None
    case_number: Optional[str] = None
    distress_tags: list[DistressTag] = Field(default_factory=list)
    quotes: dict[str, str] = Field(default_factory=dict)   # {field_name: exact text from the document}
    confidence: Confidence = Confidence.low                # if the AI forgets, assume the worst

    # ---- cleaning rules ----
    @field_validator(*TEXT_FIELDS, mode="before")
    @classmethod
    def _clean_text(cls, v):
        return _blank_to_none(v)

    @field_validator(*DATE_FIELDS, mode="before")
    @classmethod
    def _clean_date(cls, v):
        return parse_date(v)

    @field_validator(*AMOUNT_FIELDS, mode="before")
    @classmethod
    def _clean_amount(cls, v):
        return parse_amount(v)

    @field_validator("property_state", mode="before")
    @classmethod
    def _clean_state(cls, v):
        v = _blank_to_none(v)
        if v is None:
            return None
        if v.upper() in _STATE_CODES:
            return v.upper()
        code = _STATES.get(v.lower())
        if code:
            return code
        raise ValueError(f"'{v}' is not a U.S. state; use the 2-letter code like MI")

    @field_validator("property_zip", mode="after")
    @classmethod
    def _clean_zip(cls, v):
        if v is None:
            return None
        m = re.search(r"\d{5}(-\d{4})?", v)
        return m.group(0) if m else v

    @field_validator("page_still_unopened", mode="before")
    @classmethod
    def _clean_bool(cls, v):
        if isinstance(v, str):
            low = v.strip().lower()
            if low in ("true", "yes"):
                return True
            if low in ("false", "no"):
                return False
            if low in _NULL_WORDS:
                return None
        return v

    @field_validator("filing_type", mode="before")
    @classmethod
    def _clean_filing_type(cls, v):
        return _normalize_enum_word(v)          # required: a missing value is an error

    @field_validator("loan_kind", mode="before")
    @classmethod
    def _clean_loan_kind(cls, v):
        v = _normalize_enum_word(v)
        return None if v is None or v in _NULL_WORDS else v

    @field_validator("property_type", mode="before")
    @classmethod
    def _clean_property_type(cls, v):
        v = _normalize_enum_word(v)
        return "unknown" if v is None or v in _NULL_WORDS else v

    @field_validator("confidence", mode="before")
    @classmethod
    def _clean_confidence(cls, v):
        v = _normalize_enum_word(v)
        return "low" if v is None or v in _NULL_WORDS else v

    @field_validator("distress_tags", mode="before")
    @classmethod
    def _clean_tags(cls, v):
        if v is None:
            return []
        return [_normalize_enum_word(t) for t in v]

    @field_validator("other_parties", mode="before")
    @classmethod
    def _clean_parties(cls, v):
        return [] if v is None else v

    @field_validator("quotes", mode="before")
    @classmethod
    def _clean_quotes(cls, v):
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("quotes must be an object like {\"field_name\": \"exact text\"}")
        return {str(k): str(q) for k, q in v.items() if q is not None and str(q).strip()}


class FilingRecord(ExtractionOutput):
    """One row of the filing_fields table: the AI's fields plus our bookkeeping."""
    file_id: str
    status: ReviewStatus = ReviewStatus.ok
    review_reason: Optional[str] = None
    model_name: Optional[str] = None
    prompt_version: Optional[str] = None
    extracted_at: Optional[datetime] = None
    input_tokens: int = 0
    output_tokens: int = 0
    est_cost_usd: float = 0.0

    def to_db_row(self) -> dict:
        """A plain dict ready to insert into filing_fields (enums as text, lists as JSON text)."""
        row = self.model_dump()
        for k, v in list(row.items()):
            if isinstance(v, Enum):
                row[k] = v.value
        row["other_parties"] = json.dumps(row["other_parties"])
        row["distress_tags"] = json.dumps([t.value if isinstance(t, Enum) else t for t in row["distress_tags"]])
        row["quotes"] = json.dumps(row["quotes"])
        return row


# Fields (other than bookkeeping) that hold a value the AI should back up with a quote.
# loan_kind is a category worked out from the kind of document ("MORTGAGE" -> permanent), not text to copy.
QUOTE_EXEMPT_FIELDS = {"filing_type", "property_type", "confidence", "distress_tags", "quotes",
                       "other_parties", "page_still_unopened", "loan_kind"}
# City, state and zip are normally read from the address line, so the quote for property_address covers them.
ADDRESS_PARTS = ("property_city", "property_state", "property_zip")


def fields_missing_quotes(output: ExtractionOutput) -> list[str]:
    """Filled-in fields that have no supporting quote (the AI was told to quote every one)."""
    out = []
    for name in ExtractionOutput.model_fields:
        if name in QUOTE_EXEMPT_FIELDS:
            continue
        val = getattr(output, name)
        if val in (None, "", []) or name in output.quotes:
            continue
        if name in ADDRESS_PARTS and str(val).lower() in output.quotes.get("property_address", "").lower():
            continue                      # the address quote already shows it
        out.append(name)
    return out


# ---------------------------------------------------------------------------
# Turning the AI's text into a Python dict
# ---------------------------------------------------------------------------
def parse_llm_json(text: str) -> dict:
    """
    Pull the JSON object out of the AI's reply. Handles a ```json fence or a stray
    sentence around it. Raises ValueError with a readable message if there is none.
    """
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I)
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("the reply did not contain a JSON object")
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"the reply was not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("the JSON must be an object, not a list")
    return data
