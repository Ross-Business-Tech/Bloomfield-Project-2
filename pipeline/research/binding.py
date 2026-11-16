"""
binding.py - corroborated content binding: deciding whether a fetched page is actually
about a given universe property.

A bare mention is not identity. Facilities share names across chains and streets repeat
across towns, so ONE matching element (even the property's name) is not enough. A page
binds to a property only when the text CORROBORATES it:

  * at least one STRONG element appears - the property's name, street address, or parcel
    number; AND
  * at least one SECOND independent element appears - any of name, street, parcel, city,
    county, or ZIP.

Anything less stays unattached: the fact is kept, but under no property, and it can never
satisfy a gate. This is the same discipline as the matcher's corroborated linking, applied
to free web text the agent fetched.
"""
from __future__ import annotations

import re

STRONG = ("name", "street", "parcel")
ALL = ("name", "street", "parcel", "city", "county", "zip")
_MIN_LEN = {"name": 5, "street": 5, "parcel": 3, "city": 3, "county": 3, "zip": 3}


def squash(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def elements_from_row(row) -> dict[str, str]:
    """The checkable identity elements from a properties-table row
    (property_name, street_address, city, state, county, zip, parcel_number order)."""
    name, street, city, _state, county, zip_, parcel = row
    return {"name": name, "street": street, "city": city,
            "county": county, "zip": zip_, "parcel": parcel}


def present_elements(elements: dict[str, str | None], *texts: str | None) -> set[str]:
    """Which of the property's elements appear in the texts (squashed substring match)."""
    hay = squash(" ".join(t for t in texts if t))
    if not hay:
        return set()
    return {k for k in ALL
            if elements.get(k) and len(squash(elements[k])) >= _MIN_LEN[k]
            and squash(elements[k]) in hay}


def corroborated_bind(elements: dict[str, str | None], *texts: str | None) -> bool:
    """True only when the texts corroborate the property: a strong element (name, street
    or parcel) PLUS a second independent element. A single generic mention never binds."""
    present = present_elements(elements, *texts)
    return bool(present & set(STRONG)) and len(present) >= 2
