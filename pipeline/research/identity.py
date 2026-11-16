"""
identity.py - how a record gets attached to a property, and how much that attachment is trusted.

The method:
  * CMS records attach by CCN (the 6-digit certification number), zero-padded. Facility names
    in cost reports can lag rebrandings, so the CCN is the key and the name is just a label.
  * County records attach by parcel, using the county's own spelling of the address. When the
    county spelling and the CMS spelling disagree, both are kept (the disagreement is data).
  * A name/city-only match is flagged name_only_unverified and can never, on its own,
    carry a property to verified identity.
  * An ownership role, a shared person or a shared mailing address is NOT common ownership,
    and a role's "since" date is NOT a transfer (see the caveats in evidence.py).
"""
from __future__ import annotations

import re

IDENTITY_METHODS = (
    "ccn",                      # CMS Certification Number - strongest for care facilities
    "parcel",                   # county parcel number - strongest for county records
    "address_county_spelling",  # street address in the county's own spelling
    "name_only_unverified",     # name/city match a person has NOT confirmed
    "unmatched",                # stored under its own source key only
)

# Only these methods count as a verified identity for the research gates (gates.py).
VERIFIED_METHODS = ("ccn", "parcel")


def normalize_ccn(value) -> str | None:
    """'45987' -> '045987'; '145987' -> '145987'; anything without 1-6 digits -> None."""
    digits = re.sub(r"\D", "", str(value or ""))
    if not 1 <= len(digits) <= 6:
        return None
    return digits.zfill(6)


def _norm_text(value) -> str:
    return " ".join(str(value or "").upper().split())


def key_for_ccn(ccn) -> str | None:
    n = normalize_ccn(ccn)
    return f"ccn:{n}" if n else None


def key_for_parcel(state, county, parcel) -> str:
    return f"parcel:{_norm_text(state)}:{_norm_text(county)}:{_norm_text(parcel)}"


def key_for_address(state, county, address) -> str:
    """The county's own spelling is kept exactly as the county wrote it (just normalized case/spacing)."""
    return f"addr:{_norm_text(state)}:{_norm_text(county)}:{_norm_text(address)}"


def key_for_name(name, city=None) -> str:
    """A name-only key. Always paired with identity_method 'name_only_unverified'."""
    base = _norm_text(name)
    if city:
        base += f"|{_norm_text(city)}"
    return f"name:{base}"


def identity_key(method: str, *parts) -> str | None:
    """Build the identity key for a method, or None if the parts don't support it."""
    if method == "ccn":
        return key_for_ccn(parts[0] if parts else None)
    if method == "parcel" and len(parts) >= 3:
        return key_for_parcel(*parts[:3])
    if method == "address_county_spelling" and len(parts) >= 3:
        return key_for_address(*parts[:3])
    if method == "name_only_unverified" and parts:
        return key_for_name(*parts[:2])
    if method == "unmatched" and parts:
        return f"unmatched:{_norm_text(parts[0])}"
    return None
