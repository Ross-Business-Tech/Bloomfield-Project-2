"""
county_tax.py - the county tax lane, plus the small "other lanes" recorders (liens, dockets, leads).

The method:
  * County tax status is primary financial evidence. Taxes PAID CURRENT are counter-evidence:
    the method searches for reasons the distress reading is wrong, not only reasons it is
    right. Every check is recorded as a search row (evidence.record_search), including the
    ones that find nothing - "we searched and found nothing" is a fact, not a memory.
  * Historical tax-sale and redemption events stay attached to the parcel and the date they
    happened. They are never assigned to the current owner or operator without temporal
    entity grounding (the caveat travels with the row).
  * A lien-search result is bounded to the exact query string used: 0 results for one query
    is not "no liens exist".
  * A court docket proves a case exists. Until the complaint is read, that is all it proves.
  * Press or plaintiff-firm allegations stay unattributed leads until a source ties the
    entity to a universe property.

The county sites themselves are reached like every other county source (screened first,
manual inbox until checked - see the project README). This module turns the records into
evidence; it does not fetch them.
"""
from __future__ import annotations

from datetime import date

from . import evidence as ev
from .identity import identity_key

SOURCE = "county_tax"


def _identity(state, county, parcel, address):
    """Parcel first (strongest for county records), then the county's own address spelling."""
    if parcel:
        return "parcel", identity_key("parcel", state, county, parcel)
    if address:
        return "address_county_spelling", identity_key("address_county_spelling", state, county, address)
    return "unmatched", None


def record_tax_year(con, *, state: str, county: str, tax_year: int, parcel=None, address=None,
                    property_key=None, billed: float | None = None, paid: float | None = None,
                    status: str | None = None, source_url: str | None = None, file_id: str | None = None,
                    dataset_vintage: str | None = None) -> str:
    """
    Store one tax year's billed/paid status. Paid-in-full is counter-evidence; delinquent is
    a distress financial fact; anything unknown stays neutral (missing is never healthy).
    Also records the search itself (lane counter_evidence), so the gates can see the check ran.
    Returns the evidence id.
    """
    method, key = _identity(state, county, parcel, address)
    # paid UNKNOWN is not paid 0: a billed-only row stays neutral (missing is unknown),
    # never a false delinquency. Delinquent needs an explicit status or both figures.
    status_l = (status or "").strip().lower()
    explicit_delinquent = status_l in {"delinquent", "unpaid", "past due"}
    explicit_paid = status_l in {"paid", "paid in full", "current"}
    figures_paid = billed is not None and paid is not None and paid >= billed and billed > 0
    figures_short = billed is not None and paid is not None and paid < billed
    # An explicit delinquent status ALWAYS wins over computed figures: a record whose status
    # says past due is never read as paid current. A paid-status word contradicted by short
    # figures is a contradiction: recorded as unknown, never flipped to either direction.
    contradictory = explicit_paid and figures_short
    delinquent = explicit_delinquent or (figures_short and not explicit_paid)
    paid_current = figures_paid and not explicit_delinquent
    if delinquent:
        direction = "distress"
    elif paid_current:
        direction = "counter"
    else:
        direction = "neutral"
    bits = []
    if billed is not None:
        bits.append(f"billed {billed:,.0f}")
    if paid is not None:
        bits.append(f"paid {paid:,.0f}")
    if status:
        bits.append(f"status {status}")
    ev_id = f"{SOURCE}:{key or 'unmatched'}:{tax_year}"
    ev.insert_evidence(con, ev.Evidence(
        ev_id, claim_type="financial_fact", source_name=SOURCE, field_name="property_tax",
        property_key=property_key, direction=direction, identity_method=method, identity_key=key,
        raw_record_id=parcel, as_of=date(int(tax_year), 12, 31),
        units="usd_unconfirmed", unit_confidence="unconfirmed",
        value_number=paid if paid is not None else billed,
        value_text=f"tax year {tax_year}: " + ", ".join(bits) if bits else f"tax year {tax_year}",
        caveat="county tax record as billed/paid for the stated tax year; "
               + ("the stated status and the stated figures contradict, so this is recorded "
                  "as unknown - " + ev.MISSING_IS_UNKNOWN if contradictory else
                  "taxes paid current are counter-evidence" if paid_current else
                  "a delinquency is a distress fact, not proof of broader distress" if delinquent
                  else ev.MISSING_IS_UNKNOWN),
        source_url=source_url, file_id=file_id,
        dataset_vintage=dataset_vintage or f"county tax {county},{state}"))
    ev.record_search(con, property_key=property_key, identity_key=key, lane="county_tax",
                     query=f"county tax {county},{state} {parcel or address or ''} tax year {tax_year}".strip(),
                     result_count=1 if (billed is not None or paid is not None or status) else 0,
                     notes="paid current" if paid_current else None)
    return ev_id


def record_tax_event(con, *, state: str, county: str, event_kind: str, event_date: date,
                     parcel=None, address=None, property_key=None, certificate: str | None = None,
                     source_url: str | None = None, file_id: str | None = None) -> str:
    """
    Store a historical tax-sale or redemption event. Direction stays NEUTRAL: the event is
    not assigned to the current owner or operator without temporal entity grounding.
    """
    if event_kind not in ("tax_sale", "redemption"):
        raise ValueError("event_kind is tax_sale or redemption")
    method, key = _identity(state, county, parcel, address)
    ev_id = f"{SOURCE}:{key or 'unmatched'}:{event_kind}:{event_date}"
    ev.insert_evidence(con, ev.Evidence(
        ev_id, claim_type="financial_fact", source_name=SOURCE, field_name=event_kind,
        property_key=property_key, direction="neutral", identity_method=method, identity_key=key,
        raw_record_id=parcel, as_of=event_date, units="count", unit_confidence="confirmed",
        value_text=(f"{event_kind.replace('_', ' ')} recorded {event_date}"
                    + (f", certificate {certificate}" if certificate else "")),
        caveat=ev.TEMPORAL_GROUNDING_CAVEAT, source_url=source_url, file_id=file_id,
        dataset_vintage=f"county tax {county},{state}"))
    return ev_id


def record_lien_search(con, *, query: str, result_count: int, property_key=None, identity=None,
                       source_name: str = "state_sos_tax_liens", notes: str | None = None,
                       coverage_limit: str | None = None) -> str:
    """Record a lien search. A 0-result is bounded to the exact query string used (plus any
    further coverage limit the caller states - jurisdiction, period, identity)."""
    return ev.record_search(con, property_key=property_key, identity_key=identity, lane="liens",
                            query=query, result_count=result_count,
                            coverage_limit=coverage_limit or ev.QUERY_BOUNDED_CAVEAT, notes=notes)


def record_docket(con, *, case_number: str, court: str | None = None, title: str | None = None,
                  url: str | None = None, property_key=None) -> str:
    """
    Store that a case exists. Until the complaint is read, that is ALL this proves:
    claim_type allegation_unread, direction neutral.
    """
    ev_id = f"docket:{ev.slug(case_number)}"
    ev.insert_evidence(con, ev.Evidence(
        ev_id, claim_type="allegation_unread", source_name="court_docket", field_name="case",
        property_key=property_key, direction="neutral", raw_record_id=case_number,
        value_text=" ".join(x for x in (title, f"({court})" if court else "",
                                          f"case {case_number}") if x),
        caveat=ev.DOCKET_UNREAD_CAVEAT, coverage_limit=ev.DOCKET_UNREAD_CAVEAT,
        source_url=url, dataset_vintage="court docket listing"))
    return ev_id


def record_press_lead(con, *, claim: str, outlet: str, url: str | None = None,
                      property_key=None) -> str:
    """Store a press / plaintiff-firm allegation as an unattributed lead."""
    ev_id = f"lead:{ev.slug(outlet)}:{ev.slug(claim)}"
    ev.insert_evidence(con, ev.Evidence(
        ev_id, claim_type="lead_unqualified", source_name="press", field_name="allegation",
        property_key=property_key, direction="neutral", value_text=f"{outlet}: {claim}",
        caveat=ev.LEAD_UNQUALIFIED_CAVEAT, coverage_limit=ev.LEAD_UNQUALIFIED_CAVEAT,
        source_url=url, dataset_vintage=f"press item, {outlet}"))
    return ev_id
