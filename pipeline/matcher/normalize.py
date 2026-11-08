"""
normalize.py - turn messy names, addresses and parcel numbers into comparable keys.

In plain English: county clerks, the SEC, Trepp and the AI Reader all write the same thing
differently ("Maize Row Holdings, L.L.C." vs "MAIZE ROW HOLDINGS LLC"; "12 North Main Street"
vs "12 N. Main St"). These helpers boil each one down to a plain key so they can be compared.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------
STREET_WORDS = {
    "street": "st", "str": "st", "avenue": "ave", "av": "ave", "road": "rd", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl", "parkway": "pkwy", "highway": "hwy",
    "circle": "cir", "terrace": "ter", "square": "sq", "trail": "trl", "way": "way",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    "suite": "ste", "apartment": "apt", "unit": "unit", "first": "1st", "second": "2nd",
    "third": "3rd", "fourth": "4th", "fifth": "5th",
}
_UNIT = re.compile(r"(?:\b(?:ste|apt|unit)|#)\s*\w+$")


def street_key(address: str | None) -> str | None:
    """'1000 Example Campus Way, Ann Arbor, MI' -> '1000 example campus way'. None if no street."""
    if not address:
        return None
    line = address.split(",")[0].lower().replace("#", " # ")
    line = re.sub(r"[^a-z0-9# ]", " ", line)
    words = [STREET_WORDS.get(w, w) for w in line.split()]
    # A street address needs a number and a name; "TBD" or a bare city is not enough.
    if len(words) < 2 or not any(ch.isdigit() for ch in words[0]):
        return None
    key = " ".join(words)
    key = _UNIT.sub("", key).strip()            # "10 main st ste 200" -> "10 main st"
    return key or None


def house_number(street: str | None) -> str | None:
    """'1000 example campus way' -> '1000'. Ranges like '100-110' keep the first number."""
    if not street:
        return None
    m = re.match(r"(\d+)", street)
    return m.group(1) if m else None


def street_name(street: str | None) -> str | None:
    """'1000 example campus way' -> 'example campus way'."""
    if not street:
        return None
    parts = street.split(" ", 1)
    return parts[1] if len(parts) > 1 else None


def parcel_key(parcel: str | None) -> str | None:
    """'09-12-345-001' and '0912345001' compare equal."""
    if not parcel:
        return None
    p = re.sub(r"[^a-z0-9]", "", parcel.lower())
    return p if len(p) >= 2 else None


def city_key(city: str | None) -> str | None:
    if not city:
        return None
    c = re.sub(r"[^a-z ]", " ", city.lower())
    c = re.sub(r"\b(city of|township|twp|village of)\b", " ", c)
    c = " ".join(c.split())
    return c or None


def state_key(state: str | None) -> str:
    return (state or "").strip().upper()[:2] or "??"


def zip5(z: str | None) -> str | None:
    m = re.match(r"\s*(\d{5})", str(z or ""))
    return m.group(1) if m and m.group(1) != "00000" else None


def split_address(address: str | None) -> tuple[str | None, str | None, str | None, str | None]:
    """'1000 Example Way, Ann Arbor, MI 48104' -> ('1000 Example Way', 'Ann Arbor', 'MI', '48104')."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    street = parts[0] if parts else None
    city = parts[1] if len(parts) > 2 else None
    st = zp = None
    if len(parts) >= 2:
        m = re.match(r"([A-Za-z]{2})\b\s*(\d{5})?", parts[-1])
        if m:
            st, zp = m.group(1).upper(), m.group(2)
    return street, city, st, zp


# ---------------------------------------------------------------------------
# Names of companies, people and banks
# ---------------------------------------------------------------------------
# Legal-form words that don't tell two companies apart.
ENTITY_SUFFIXES = {
    "llc", "l l c", "lc", "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "lp", "l p", "llp", "lllp", "plc", "pllc", "pc", "na", "n a", "fsb", "trust",
}
_SUFFIX_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(s) for s in ENTITY_SUFFIXES),
                                                  key=len, reverse=True)) + r")\b")
_KINDS = [
    ("bank", re.compile(r"\b(bank|bancorp|savings|credit union|national association|federal)\b", re.I)),
    ("llc", re.compile(r"\bl\.?\s?l\.?\s?c\b|\blc\b", re.I)),
    ("partnership", re.compile(r"\b(l\.?p|llp|lllp|partners|partnership)\b", re.I)),
    ("corporation", re.compile(r"\b(inc|incorporated|corp|corporation)\b", re.I)),
    ("trust", re.compile(r"\btrust\b", re.I)),
]


def entity_kind(name: str | None) -> str:
    """Rough type of a name: bank / llc / partnership / corporation / trust / other."""
    for kind, rx in _KINDS:
        if name and rx.search(name):
            return kind
    return "other"


def entity_key(name: str | None) -> str | None:
    """
    'Maize Row Holdings, L.L.C.' -> 'maize row holdings'
    'The Smith & Jones Co.'      -> 'smith and jones'
    Keeps words like 'holdings' or 'phase ii' because they DO tell entities apart.
    """
    if not name:
        return None
    n = name.lower().replace("&", " and ")
    n = re.sub(r"\(.*?\)", " ", n)                     # "(HVAC contractor)"
    n = re.sub(r"[.,']", "", n)                       # L.L.C. -> llc, O'Neil -> oneil
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b(the|a k a|aka|dba|d b a|fka)\b", " ", n)
    n = _SUFFIX_RE.sub(" ", n)
    n = " ".join(n.split())
    return n or None


_BANK_NOISE = re.compile(r"\b(the|national association|na|n a|inc|corp|corporation|co|company|fsb)\b")


def bank_key(name: str | None) -> str | None:
    """'First Example Bank, N.A.' -> 'first example bank' (keeps 'bank': it's informative)."""
    if not name:
        return None
    n = re.sub(r"[.,']", "", name.lower()).replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = _BANK_NOISE.sub(" ", n)
    return " ".join(n.split()) or None


def property_name_key(name: str | None) -> str | None:
    """'FAKE Maize Row Student Lofts - Phase I' -> 'maize row student lofts phase i' (drops FAKE tag)."""
    if not name:
        return None
    n = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    n = re.sub(r"\b(fake|the|apartments?|apts?)\b", " ", n)
    return " ".join(n.split()) or None
