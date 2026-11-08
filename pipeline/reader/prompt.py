"""
prompt.py - the instructions we give the AI, and how we present each document to it.

In plain English: the AI is told to act like a careful clerk. It may only copy facts that are
written in the document, must give the exact quote for each fact, and must leave a field empty
(null) rather than guess. The lists of allowed words (filing types, property types...) are
filled in from schema.py so the prompt and the database can never disagree.

IMPORTANT: if you change ANY wording below, change PROMPT_VERSION too. The version is part of
the cache key, so a new version makes the reader re-read documents with the new instructions.
"""
from __future__ import annotations

from .schema import DistressTag, FilingType, LoanKind, PropertyType, values

PROMPT_VERSION = "v2"

INTRO = (
    "You extract facts from U.S. commercial real estate documents: foreclosure notices, lis pendens, "
    "receivership filings, lender lawsuits, recorded mortgages (construction and permanent), loan "
    "extensions, modifications, assignments and releases, mechanic's (contractor) liens, UCC "
    "foreclosure sale notices, auction listings, tax delinquency notices, state license actions, loan "
    "servicer commentary, building permits, certificates of occupancy, and project websites, news "
    "articles or announcements about new developments.\n\n"
    "Return ONLY a JSON object matching the schema below. No explanations, no markdown."
)

# The numbered rules. New sources later (photos, emails...) can append a rule here.
RULES = [
    "Only use information written in the document. Never use outside knowledge and never guess.",
    "If a field is not in the document, set it to null. Null is always better than a guess.",
    'For every field you fill in, add the exact supporting text to "quotes", copied word for word (up to 40 words).',
    "borrower_name is the property owner, developer or debtor (often an LLC), not the lender, attorney, trustee, contractor or court.",
    'lender_name is the mortgage or construction lender. For a UCC sale, it is the secured party running the sale. If a loan was assigned, use the current holder and list the original lender in other_parties with role "original_lender".',
    "For a recorded loan, set loan_kind to construction, permanent, extension, modification, assignment or release, and fill in loan_amount, recording_date and maturity_date if stated.",
    'For a mechanic\'s lien, put the contractor in lien_claimant, the amount in lien_amount, and the work done (for example "HVAC") in lien_work.',
    "For a building permit or certificate of occupancy, fill in permit_number, permit_status and permit_expiration_date if stated.",
    'For a project website, news article or announcement, fill in project_name, developer_name, stated_opening_phrase (exactly as written, for example "Opening Winter 2026") and leasing_season if stated. Set page_still_unopened to true if the page says coming soon, pre-leasing, join the waitlist, under construction or similar, and false if it says now open, move in today, or similar.',
    'For an auction listing, fill in auction_date and starting_bid, and add the seller to other_parties with role "seller" (note "bank-owned" or "REO" in the role if stated).',
    "Copy property_address exactly as written. Do not correct or complete it.",
    'Use only the allowed values for filing_type, property_type, loan_kind and distress_tags. Use "unknown" for property_type unless the document states it or clearly describes it (for example, "120-unit apartment complex" is multifamily; "beds" leased to students is student_housing).',
    "Only add a distress tag if the document directly supports it.",
    "Write dates as YYYY-MM-DD and dollar amounts as plain numbers.",
    'If the document is about a single-family home or condo unit, set filing_type to "other" and confidence to "low".',
    'confidence is "high" if the key fields are clearly stated, "medium" if one needs interpretation, and "low" if the document is unclear, partial or poorly scanned.',
]

SCHEMA_TEXT = (
    '{"filing_type", "borrower_name", "developer_name", "project_name", "other_parties": [{"name", "role"}], '
    '"property_address", "property_city", "property_state", "property_zip", "parcel_number", "property_type", '
    '"lender_name", "servicer_name", "loan_kind", "loan_amount", "amount_owed", "recording_date", '
    '"maturity_date", "lien_claimant", "lien_amount", "lien_work", "permit_number", "permit_status", '
    '"permit_expiration_date", "stated_opening_phrase", "page_still_unopened", "leasing_season", '
    '"auction_date", "starting_bid", "filing_date", "sale_date", "case_number", "distress_tags": [], '
    '"quotes": {"<field>": "<exact text>"}, "confidence"}'
)


def build_system_prompt() -> str:
    """Assemble the full instructions. Allowed-value lists come straight from schema.py."""
    rules = "\n".join(f"{i}. {r}" for i, r in enumerate(RULES, start=1))
    return (
        f"{INTRO}\n\nRules:\n{rules}\n\n"
        f"Allowed filing_type: {', '.join(values(FilingType))}\n\n"
        f"Allowed property_type: {', '.join(values(PropertyType))}\n\n"
        f"Allowed loan_kind: {', '.join(values(LoanKind))}\n\n"
        f"Allowed distress_tags: {', '.join(values(DistressTag))}\n\n"
        f"Schema:\n{SCHEMA_TEXT}"
    )


SYSTEM_PROMPT = build_system_prompt()

# Facts from OUR records (not from the document) that help the AI. Optional ones are
# shown only when present, so new columns added to raw_files later flow through automatically.
CONTEXT_FIELDS = ["source", "doc_category", "source_medium", "state", "county", "city"]


def build_user_text(raw_file_row: dict, text: str) -> str:
    """The document plus a small block of context from our own records."""
    ctx = [f"{k}: {raw_file_row[k]}" for k in CONTEXT_FIELDS if raw_file_row.get(k)]
    return (
        "CONTEXT FROM OUR RECORDS (not part of the document; never quote from this block):\n"
        + "\n".join(ctx)
        + "\n\n=== DOCUMENT TEXT STARTS ===\n"
        + text
        + "\n=== DOCUMENT TEXT ENDS ===\n\nReturn ONLY the JSON object."
    )


def build_retry_text(user_text: str, bad_reply: str, error: str) -> str:
    """Used once if the first answer was not valid: shows the AI what was wrong."""
    return (
        f"{user_text}\n\nYour previous reply could not be used.\nProblem: {error}\n"
        f"Previous reply (first 1500 characters):\n{bad_reply[:1500]}\n\n"
        "Reply again with ONLY a corrected JSON object that follows the schema and allowed values."
    )
