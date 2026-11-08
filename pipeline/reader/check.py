"""
check.py - decides whether to trust what the AI returned.

In plain English: the AI can make mistakes or invent things, so every answer is checked:
  1. The JSON must be well-formed and follow the schema (schema.py).
  2. QUOTE CHECK - every quote must really appear in the document. This is our main
     protection against made-up values. A value whose quote is missing is never silently accepted.
  3. Other warning signs (low confidence, nothing identifying the property, a scan that was
     mostly OCR, impossible dollar amounts or dates, a document type that disagrees with
     Part A's label...) send the record to a person ("needs_review") instead of accepting it.

validate() returns (record, status, review_reason):
    status "ok"            - nothing suspicious
    status "needs_review"  - keep the data, but a person should look (review_reason says why)
    status "failed"        - the answer was unusable even after the schema check (record is None)
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from pydantic import ValidationError
from rapidfuzz import fuzz

from llm.settings import load_reader_config

from .schema import (AMOUNT_FIELDS, DATE_FIELDS, TEXT_FIELDS, ExtractionOutput, FilingType,
                     fields_missing_quotes, parse_llm_json)

MAX_AMOUNT = 5_000_000_000
DATE_MIN, DATE_MAX = date(1990, 1, 1), date(2040, 12, 31)

# What Part A's doc_category label may legitimately turn into. If the AI's filing_type is not in
# the set, we keep the AI's answer but flag it. Labels not listed here are not checked.
COMPATIBLE_FILING_TYPES = {
    "foreclosure_notice": {"foreclosure_notice", "ucc_sale_notice"},
    "receivership": {"receivership"},
    "lender_lawsuit": {"lender_lawsuit", "foreclosure_notice"},
    "lis_pendens": {"lis_pendens"},
    "recorded_loan": {"recorded_loan"},
    "mechanics_lien": {"mechanics_lien"},
    "tax_delinquency": {"tax_delinquency"},
    "building_permit": {"building_permit"},
    "certificate_of_occupancy": {"certificate_of_occupancy"},
    "license_action": {"license_action"},
    "project_website": {"project_website", "development_announcement"},
    "news_article": {"news_article", "development_announcement"},
    "auction_listing": {"auction_listing"},
    "ucc_sale_notice": {"ucc_sale_notice", "foreclosure_notice"},
}


# ---------------------------------------------------------------------------
# Comparing quotes with the document
# ---------------------------------------------------------------------------
_PAGE_MARK = re.compile(r"\[PAGE \d+\]")


def normalize_for_quote(text: str) -> str:
    """
    Make text comparable: join words split by a hyphen at a line break, straighten curly
    quotes and fancy dashes, ignore case, and squeeze all whitespace to single spaces.
    """
    t = unicodedata.normalize("NFKC", text or "")
    t = _PAGE_MARK.sub(" ", t)
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)                       # hyphenation at line breaks
    t = t.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-").replace("−", "-")
    return re.sub(r"\s+", " ", t).strip().lower()


def _alnum(text: str) -> str:
    """Letters and digits only (for the looser value-versus-quote comparison)."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def find_missing_quotes(quotes: dict[str, str], document_text: str) -> list[str]:
    """Names of fields whose quote does NOT appear in the document."""
    doc = normalize_for_quote(document_text)
    return [field for field, quote in quotes.items() if normalize_for_quote(quote) not in doc]


# ---------------------------------------------------------------------------
# The main check
# ---------------------------------------------------------------------------
def parse_and_validate(output) -> tuple[ExtractionOutput | None, str | None]:
    """Step 1: turn the AI's reply (text or dict) into a validated record, or explain what is wrong."""
    try:
        data = parse_llm_json(output) if isinstance(output, str) else output
        return ExtractionOutput.model_validate(data), None
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:6])
        return None, f"the JSON did not match the schema ({problems})"
    except ValueError as e:
        return None, str(e)


def validate(output, document_text: str, *, doc_category: str | None = None, ocr_share: float = 0.0,
             cfg: dict | None = None) -> tuple[ExtractionOutput | None, str, str | None]:
    """
    Check one AI answer against the document. `output` is the AI's reply text or its parsed dict.
    Returns (record, status, review_reason). See the module notes for what the statuses mean.
    """
    cfg = cfg or load_reader_config()
    rcfg = cfg.get("reader") or {}

    record, error = parse_and_validate(output)                                     # step 1
    if record is None:
        return None, "failed", error

    reasons: list[str] = []

    # step 2: quotes
    missing = find_missing_quotes(record.quotes, document_text)
    for field in missing:
        reasons.append(f"quote not found for {field}")
    if rcfg.get("require_quotes", True):
        unquoted = fields_missing_quotes(record)
        if unquoted:
            reasons.append("no supporting quote for " + ", ".join(unquoted))
    min_score = float(rcfg.get("value_in_quote_min_score", 85))
    for field in TEXT_FIELDS:                                                       # value must match its quote
        value, quote = getattr(record, field), record.quotes.get(field)
        if value and quote and field not in missing:
            if fuzz.partial_ratio(_alnum(value), _alnum(quote)) < min_score:
                reasons.append(f"value for {field} does not match its quote")

    # step 3: warning signs
    if record.confidence.value == "low":
        reasons.append("confidence is low")
    if not any([record.borrower_name, record.developer_name, record.project_name, record.property_address]):
        reasons.append("no borrower, developer, project or property address found")
    if record.filing_type == FilingType.other:
        reasons.append("filing_type is other")
    if ocr_share > float(rcfg.get("ocr_review_share", 0.5)):
        reasons.append(f"{ocr_share:.0%} of pages needed OCR (poor scan quality)")

    # sanity checks
    for field in AMOUNT_FIELDS:
        v = getattr(record, field)
        if v is not None and not (0 < v < MAX_AMOUNT):
            reasons.append(f"{field} of {v:,.0f} is not a believable amount")
    for field in DATE_FIELDS:
        v = getattr(record, field)
        if v is not None and not (DATE_MIN <= v <= DATE_MAX):
            reasons.append(f"{field} {v} is outside 1990-2040")
    if record.maturity_date and record.recording_date and record.maturity_date <= record.recording_date:
        reasons.append("maturity_date is not after recording_date")
    if record.filing_type == FilingType.mechanics_lien and record.lien_amount is None:
        reasons.append("mechanic's lien without a lien_amount")

    # consistency with Part A's label (keep the AI's answer, but flag the disagreement)
    allowed = COMPATIBLE_FILING_TYPES.get(doc_category or "")
    if allowed and record.filing_type.value not in allowed:
        reasons.append(f"Part A labeled it '{doc_category}' but the reader says '{record.filing_type.value}'")

    if reasons:
        return record, "needs_review", "; ".join(reasons)
    return record, "ok", None
