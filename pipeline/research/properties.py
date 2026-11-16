"""
properties.py - link research-layer identity keys to the universe's real property IDs.

The matcher (matcher/) assigns every building a stable property_id (prop-...) from parcel
and address records. Research evidence is attached by identity key (ccn:..., parcel:...,
addr:...). This module resolves those keys to the matcher's property_id, so a facility's
CMS rows and its county tax rows land in ONE property group instead of one group per key,
and research_status is keyed by the same property_id the rest of the app uses.

Resolution is deliberately narrow - the same matches the method trusts, and every match
needs corroboration beyond the bare identity string:

  * parcel  -> properties.parcel_number, same state AND same county (parcel numbers repeat
               across counties, so a parcel without county agreement stays unresolved)
  * address -> properties.street_address, same state AND same county (the key carries the
               county in its own spelling)
  * ccn     -> licensing_records gives the facility's address, city and ZIP; the property
               must agree on street AND on city or ZIP. The same street address in another
               city is NOT a match, and when the source or the universe lacks the city/ZIP
               context to corroborate, the key stays unresolved - never guessed.

Ambiguity (more than one candidate) also resolves to nothing. Name-only and unmatched
keys never resolve. Rows that already carry a DIFFERENT property_key are preserved as
they are and the disagreement is opened as a conflict - linking never overwrites a
preassigned property, it only fills in rows that had none.

If the matcher has not run (no properties table), linking is a no-op and groups stay
keyed by identity key.
"""
from __future__ import annotations

from collectors.common.log import get_logger
from matcher import normalize as nz
from matcher.schema import table_exists

from . import evidence as ev
from .identity import normalize_ccn
from .schema import create_research_tables

log = get_logger("research.properties")

_TABLES = ("research_evidence", "research_searches", "research_conflicts")


def _county_key(county: str | None) -> str | None:
    c = " ".join(str(county or "").upper().split())
    return c or None


def _load_properties(con) -> list[dict]:
    if not table_exists(con, "properties"):
        return []
    rows = con.execute(
        "SELECT property_id, street_address, city, state, zip, county, parcel_number "
        "FROM properties").fetchall()
    return [{
        "property_id": pid,
        "street_key": nz.street_key(street),
        "city_key": nz.city_key(city),
        "state": nz.state_key(state),
        "zip": nz.zip5(zp),
        "county": _county_key(county),
        "parcel_key": nz.parcel_key(parcel),
    } for pid, street, city, state, zp, county, parcel in rows]


def _find(props: list[dict], *, state: str | None, county: str | None = None,
          street_key: str | None = None, parcel_key: str | None = None,
          city: str | None = None, zip_: str | None = None) -> str | None:
    """Exactly one corroborated universe property, or None. Never a guess."""
    st = nz.state_key(state)
    hits = set()
    for p in props:
        if p["state"] != st:
            continue
        if parcel_key:
            # Parcel + state + county: the county must exist on BOTH sides and agree.
            if not (county and p["county"]) or _county_key(county) != p["county"]:
                continue
            if p["parcel_key"] and p["parcel_key"] == parcel_key:
                hits.add(p["property_id"])
        elif street_key:
            if not (p["street_key"] and p["street_key"] == street_key):
                continue
            if county:
                # The source named a county: both sides must know it and agree. That
                # agreement is the corroboration for county-keyed addresses.
                if not p["county"] or _county_key(county) != p["county"]:
                    continue
                hits.add(p["property_id"])
            else:
                # No county in the source (the CCN path): the same street address in
                # another city is not a match - require positive city OR ZIP agreement.
                if city and p["city_key"] and nz.city_key(city) == p["city_key"]:
                    hits.add(p["property_id"])
                elif zip_ and p["zip"] and nz.zip5(zip_) == p["zip"]:
                    hits.add(p["property_id"])
    return hits.pop() if len(hits) == 1 else None


def _ccn_addresses(con) -> dict[str, list[tuple[str, str | None, str | None, str | None]]]:
    """ccn -> [(address, city, state, zip)] from the stored licensing/CMS records."""
    if not table_exists(con, "licensing_records"):
        return {}
    out: dict[str, list[tuple]] = {}
    for lic, address, city, state, zp in con.execute(
            "SELECT license_number, address, city, state, zip FROM licensing_records "
            "WHERE source = 'cms' AND address IS NOT NULL").fetchall():
        ccn = normalize_ccn(lic)
        if ccn and address:
            out.setdefault(ccn, []).append((address, city, state, zp))
    return out


def _resolve_key(props: list[dict], ccn_addresses: dict, identity_key: str) -> str | None:
    if not identity_key or ":" not in identity_key:
        return None
    kind, rest = identity_key.split(":", 1)
    if kind == "parcel":
        parts = rest.split(":", 2)          # state : county : parcel
        if len(parts) == 3:
            return _find(props, state=parts[0], county=parts[1],
                         parcel_key=nz.parcel_key(parts[2]))
    elif kind == "addr":
        parts = rest.split(":", 2)          # state : county : address (county spelling)
        if len(parts) == 3:
            return _find(props, state=parts[0], county=parts[1],
                         street_key=nz.street_key(parts[2]))
    elif kind == "ccn":
        for address, city, state, zp in ccn_addresses.get(normalize_ccn(rest) or "", []):
            pid = _find(props, state=state, street_key=nz.street_key(address),
                        city=city, zip_=zp)
            if pid:
                return pid
    return None


def resolve_all(con) -> int:
    """
    Set property_key on research rows whose identity key resolves to exactly one
    corroborated universe property. Rows already carrying a DIFFERENT property_key are
    preserved and the disagreement opened as a conflict. Returns the number of identity
    keys linked. Idempotent.
    """
    create_research_tables(con)
    props = _load_properties(con)
    if not props:
        log.info("no matcher properties table yet; research groups stay keyed by identity")
        return 0
    ccn_addresses = _ccn_addresses(con)
    linked = 0
    for table in _TABLES:
        keys = sorted({r[0] for r in con.execute(
            f"SELECT DISTINCT identity_key FROM {table} WHERE identity_key IS NOT NULL")
            .fetchall()})
        for key in keys:
            pid = _resolve_key(props, ccn_addresses, key)
            if not pid:
                continue
            con.execute(f"UPDATE {table} SET property_key = ? "
                        f"WHERE identity_key = ? AND property_key IS NULL", [pid, key])
            for (other,) in con.execute(
                    f"SELECT DISTINCT property_key FROM {table} WHERE identity_key = ? "
                    f"AND property_key IS NOT NULL AND property_key <> ?", [key, pid]).fetchall():
                ev.set_conflict(
                    con, conflict_id=f"link:{key}:preassigned:{other}", topic="identity",
                    identity_key=key,
                    detail=f"research linking resolves {key} to {pid} by corroborated "
                           f"address/county, but some {table} rows were pre-assigned to {other}; "
                           "both kept - a person must decide which is right")
            linked += 1
            log.info("linked %s -> %s", key, pid)
    return linked
