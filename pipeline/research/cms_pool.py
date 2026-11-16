"""
cms_pool.py - the CMS pool lanes: Provider Information, Penalties and Ownership.

WHAT THIS DATA IS - read before trusting it:
  * All three files are public CMS Provider Data downloads, already fetched unchanged into
    raw_files by collectors/state.py. This module reads the stored copies; it never downloads.
  * Everything here is DESCRIPTIVE. A fine is assessed dollars with no paid/appeal status
    (never "unpaid debt"). A payment denial is days of suspended payment for NEW admissions
    (never lost revenue). Utilization is average residents / certified beds (never staffed
    occupancy). An ownership row is a record entry (never proof of control or continuity,
    and its "since" date is when the role was listed, not a transfer).
  * Matching is by CCN. Name/city-only matches stay flagged name_only_unverified (identity.py).
  * The reconciliation compares each facility's Provider Information totals (fines count,
    fines amount, payment denials) against the summed Penalties rows. A mismatch is stored
    as an OPEN CONFLICT (gates.py holds the facility out of review-ready research until a
    person resolves it). The claim covers only the facilities reconciled in that run.

Run it with:   python -m research ingest     (or import and call collect(con))
"""
from __future__ import annotations

import csv
import io
from datetime import date

from collectors import state as state_collector
from collectors.common import config
from collectors.common.log import RunTracker, get_logger
from collectors.common.parse import clean_str, to_date, to_float

from . import evidence as ev
from .identity import key_for_ccn, normalize_ccn
from .schema import create_research_tables

log = get_logger("research.cms_pool")
COLLECTOR = "research:cms_pool"
csv.field_size_limit(10_000_000)

PROVIDER_INFO = "cms_nursing_home_provider_info"
PENALTIES = "cms_nursing_home_penalties"
OWNERSHIP = "cms_nursing_home_ownership"

PENALTIES_WINDOW = "the penalties file covers roughly a 3-year window per the CMS data dictionary"

CCN_COLS = ["CMS Certification Number (CCN)", "Federal Provider Number", "CCN", "Provider CCN"]
PROVIDER_COLS = {
    "ccn": CCN_COLS,
    "fines_count": ["Number of Fines"],
    "fines_amount": ["Total Amount of Fines", "Total Amount of Fines in Dollars"],
    "denials_count": ["Number of Payment Denials"],
    "rating": ["Overall Rating"],
    "beds": ["Number of Certified Beds"],
    "residents": ["Average Number of Residents per Day"],
    "as_of": ["Processing Date"],
}
PENALTY_COLS = {
    "ccn": CCN_COLS,
    "type": ["Penalty Type"],
    "date": ["Penalty Date"],
    "fine": ["Fine Amount"],
    "denial_days": ["Payment Denial Length in Days"],
    "as_of": ["Processing Date"],
}
OWNERSHIP_COLS = {
    "ccn": CCN_COLS,
    "role": ["Role played by Owner or Manager in Facility"],
    "owner": ["Owner Name"],
    "pct": ["Ownership Percentage"],
    "since": ["Association Date"],
    "as_of": ["Processing Date"],
}


def _read_csv(text: str):
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None) or []
    return {state_collector._norm(h): i for i, h in enumerate(header)}, list(reader)


def _stored(con, source_record_id: str) -> tuple[str, str, str | None] | None:
    """The newest stored CSV for one dataset, as (file_id, text, url)."""
    row = con.execute("SELECT file_id, file_path, url FROM raw_files WHERE source = 'cms' AND "
                      "source_record_id = ? ORDER BY collected_at DESC LIMIT 1", [source_record_id]).fetchone()
    if not row:
        return None
    return row[0], config.from_stored_path(row[1]).read_text(errors="ignore"), row[2]


def _getter(pos: dict, parts: list[str]):
    def get(k: str):
        p = pos.get(k)
        return clean_str(parts[p]) if p is not None and p < len(parts) else None
    return get


# ---------------------------------------------------------------------------
# Provider Information (dataset 4pq5-n9py): one descriptive snapshot per facility
# ---------------------------------------------------------------------------
def parse_provider_info(text: str, *, file_id: str, source_url: str | None):
    """Returns (as_of, totals_by_ccn, evidence). Totals feed the reconciliation below."""
    idx, rows = _read_csv(text)
    pos = {k: state_collector._col(idx, v) for k, v in PROVIDER_COLS.items()}
    if pos["ccn"] is None:
        raise ValueError(f"[{PROVIDER_INFO}] no CCN column found; the CMS headers changed?")
    as_of: date | None = None
    totals: dict[str, dict] = {}
    out: list[ev.Evidence] = []
    for parts in rows:
        get = _getter(pos, parts)
        ccn = normalize_ccn(get("ccn"))
        if not ccn:
            continue
        as_of = as_of or to_date(get("as_of"))
        vintage = f"{PROVIDER_INFO} (processing {as_of or 'unknown'})"
        key = key_for_ccn(ccn)
        base = dict(claim_type="descriptive_signal", source_name=PROVIDER_INFO,
                    identity_method="ccn", identity_key=key, raw_record_id=ccn,
                    as_of=as_of, source_url=source_url, file_id=file_id, dataset_vintage=vintage)
        eid = lambda field: f"{PROVIDER_INFO}:{ccn}:{field}:{as_of or 'undated'}"   # noqa: E731
        rating = to_float(get("rating"))
        if rating is not None:
            out.append(ev.Evidence(eid("overall_rating"), field_name="overall_rating",
                                   value_number=rating, units="stars", unit_confidence="confirmed",
                                   direction="distress" if rating <= 2 else "neutral",
                                   caveat=ev.STARS_CAVEAT, **base))
        fines_n, fines_amt = to_float(get("fines_count")), to_float(get("fines_amount"))
        denials = to_float(get("denials_count"))
        totals[ccn] = {"fines_count": fines_n or 0.0, "fines_amount": fines_amt or 0.0,
                       "denials_count": denials or 0.0}
        if fines_n or fines_amt:
            out.append(ev.Evidence(eid("fines"), field_name="fines_total",
                                   value_number=fines_amt, units="usd_unconfirmed",
                                   direction="distress",
                                   value_text=f"{fines_n:.0f} fine(s), {fines_amt:,.0f} assessed"
                                              if fines_n is not None and fines_amt is not None else None,
                                   caveat=ev.FINE_AMOUNT_CAVEAT,
                                   coverage_limit=PENALTIES_WINDOW, **base))
        if denials:
            out.append(ev.Evidence(eid("payment_denials"), field_name="payment_denials_count",
                                   value_number=denials, units="count", unit_confidence="confirmed",
                                   direction="distress", caveat=ev.DENIAL_DAYS_CAVEAT,
                                   coverage_limit=PENALTIES_WINDOW, **base))
        beds, residents = to_float(get("beds")), to_float(get("residents"))
        if beds and beds > 0 and residents is not None:
            out.append(ev.Evidence(eid("utilization"), field_name="utilization",
                                   value_number=min(residents / beds, 1.5), units="ratio",
                                   unit_confidence="confirmed", caveat=ev.UTILIZATION_CAVEAT, **base))
    return as_of, totals, out


# ---------------------------------------------------------------------------
# Penalties (NH_Penalties_*): individual penalty rows, summed per facility
# ---------------------------------------------------------------------------
def parse_penalties(text: str, *, file_id: str, source_url: str | None):
    """Returns (as_of, totals_by_ccn, evidence)."""
    idx, rows = _read_csv(text)
    pos = {k: state_collector._col(idx, v) for k, v in PENALTY_COLS.items()}
    if pos["ccn"] is None:
        raise ValueError(f"[{PENALTIES}] no CCN column found; the CMS headers changed?")
    as_of: date | None = None
    totals: dict[str, dict] = {}
    for parts in rows:
        get = _getter(pos, parts)
        ccn = normalize_ccn(get("ccn"))
        if not ccn:
            continue
        as_of = as_of or to_date(get("as_of"))
        t = totals.setdefault(ccn, {"fine_count": 0.0, "fine_amount": 0.0,
                                    "denial_count": 0.0, "denial_days": 0.0})
        ptype = (get("type") or "").lower()
        fine = to_float(get("fine"))
        days = to_float(get("denial_days"))
        if fine is not None or "fine" in ptype:
            t["fine_count"] += 1
            t["fine_amount"] += fine or 0.0
        if days is not None or "denial" in ptype:
            t["denial_count"] += 1
            t["denial_days"] += days or 0.0
    out: list[ev.Evidence] = []
    vintage = f"{PENALTIES} (processing {as_of or 'unknown'})"
    for ccn, t in totals.items():
        key = key_for_ccn(ccn)
        base = dict(claim_type="descriptive_signal", source_name=PENALTIES,
                    identity_method="ccn", identity_key=key, raw_record_id=ccn, direction="distress",
                    as_of=as_of, source_url=source_url, file_id=file_id, dataset_vintage=vintage,
                    coverage_limit=PENALTIES_WINDOW)
        if t["fine_count"]:
            out.append(ev.Evidence(f"{PENALTIES}:{ccn}:penalty_fines:{as_of or 'undated'}",
                                   field_name="penalty_fines_total",
                                   value_number=t["fine_amount"], units="usd_unconfirmed",
                                   value_text=f"{t['fine_count']:.0f} fine row(s), "
                                              f"{t['fine_amount']:,.0f} assessed",
                                   caveat=ev.FINE_AMOUNT_CAVEAT + "; " + ev.PENALTY_DATE_CAVEAT, **base))
        if t["denial_count"]:
            out.append(ev.Evidence(f"{PENALTIES}:{ccn}:penalty_denial_days:{as_of or 'undated'}",
                                   field_name="penalty_denial_days_total",
                                   value_number=t["denial_days"], units="days",
                                   unit_confidence="confirmed",
                                   value_text=f"{t['denial_count']:.0f} denial(s), "
                                              f"{t['denial_days']:.0f} days total",
                                   caveat=ev.DENIAL_DAYS_CAVEAT + "; " + ev.PENALTY_DATE_CAVEAT, **base))
    return as_of, totals, out


# ---------------------------------------------------------------------------
# Ownership (NH_Ownership_*): record entries only
# ---------------------------------------------------------------------------
def parse_ownership(text: str, *, file_id: str, source_url: str | None):
    """Returns (as_of, evidence). One entity_link row per owner/role entry."""
    idx, rows = _read_csv(text)
    pos = {k: state_collector._col(idx, v) for k, v in OWNERSHIP_COLS.items()}
    if pos["ccn"] is None:
        raise ValueError(f"[{OWNERSHIP}] no CCN column found; the CMS headers changed?")
    as_of: date | None = None
    out: list[ev.Evidence] = []
    for parts in rows:
        get = _getter(pos, parts)
        ccn = normalize_ccn(get("ccn"))
        role, owner = get("role"), get("owner")
        if not ccn or not (role or owner):
            continue
        as_of = as_of or to_date(get("as_of"))
        pct, since = get("pct"), get("since")
        text_value = ": ".join(x for x in (role, owner) if x)
        text_value += f" ({pct})" if pct else ""
        text_value += f" since {since}" if since else ""
        out.append(ev.Evidence(
            f"{OWNERSHIP}:{ccn}:{ev.slug(role or 'role')}:{ev.slug(owner or 'owner')}",
            claim_type="entity_link", source_name=OWNERSHIP, field_name="ownership_role",
            value_text=text_value, identity_method="ccn", identity_key=key_for_ccn(ccn),
            raw_record_id=ccn, as_of=as_of, source_url=source_url, file_id=file_id,
            dataset_vintage=f"{OWNERSHIP} (processing {as_of or 'unknown'})",
            caveat=ev.OWNERSHIP_SINCE_CAVEAT + "; " + ev.SHARED_NAME_CAVEAT,
            coverage_limit="a record entry only; not proof of control, transfer or continuity"))
    return as_of, out


# ---------------------------------------------------------------------------
# Reconciliation: Provider Information totals vs the summed Penalties rows
# ---------------------------------------------------------------------------
def reconcile(provider_totals: dict, penalty_totals: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Compare, per CCN: fines count, fines amount (to the cent) and payment-denial count.
    Returns (mismatches [(ccn, detail)], matched_ccns). The claim covers exactly the CCNs
    reconciled in this run - never extend it to the whole pool without rerunning.
    """
    mismatches, matched = [], []
    for ccn, p in sorted(provider_totals.items()):
        t = penalty_totals.get(ccn, {"fine_count": 0.0, "fine_amount": 0.0, "denial_count": 0.0})
        diffs = []
        if p["fines_count"] != t["fine_count"]:
            diffs.append(f"provider info says {p['fines_count']:.0f} fine(s) but the penalties "
                         f"file sums to {t['fine_count']:.0f}")
        if abs(p["fines_amount"] - t["fine_amount"]) > 0.01:
            diffs.append(f"provider info says {p['fines_amount']:,.2f} assessed but the penalties "
                         f"file sums to {t['fine_amount']:,.2f}")
        if p["denials_count"] != t["denial_count"]:
            diffs.append(f"provider info says {p['denials_count']:.0f} payment denial(s) but the "
                         f"penalties file holds {t['denial_count']:.0f}")
        if diffs:
            mismatches.append((ccn, "; ".join(diffs)))
        else:
            matched.append(ccn)
    return mismatches, matched


def collect(con) -> RunTracker:
    """Rebuild the CMS-pool evidence from the stored files. Reruns replace, never duplicate."""
    create_research_tables(con)
    with RunTracker(con, COLLECTOR) as run:
        parsed: dict[str, tuple] = {}
        for dataset, parser in ((PROVIDER_INFO, parse_provider_info),
                                (PENALTIES, parse_penalties),
                                (OWNERSHIP, parse_ownership)):
            stored = _stored(con, dataset)
            if not stored:
                run.manual_inbox(dataset, "no stored file; run collectors.state first or drop the "
                                          "CSV in data/inbox/cms/ with a sidecar naming the source")
                continue
            file_id, text, url = stored
            try:
                result = parser(text, file_id=file_id, source_url=url)
            except ValueError as e:
                run.error(str(e))
                continue
            con.execute("DELETE FROM research_evidence WHERE file_id = ?", [file_id])
            rows = result[-1]
            for e in rows:
                ev.insert_evidence(con, e)
            parsed[dataset] = result
            run.new_file()
            log.info("%s: %d evidence rows from %s", COLLECTOR, len(rows), dataset)
        if PROVIDER_INFO in parsed and PENALTIES in parsed:
            mismatches, matched = reconcile(parsed[PROVIDER_INFO][1], parsed[PENALTIES][1])
            for ccn, detail in mismatches:
                ev.set_conflict(con, conflict_id=f"cms-pool:{ccn}:provider_vs_penalties",
                                topic="value", identity_key=key_for_ccn(ccn),
                                detail="Provider Information totals disagree with the summed "
                                       f"penalty rows: {detail}")
            for ccn in matched:
                ev.resolve_conflict(con, f"cms-pool:{ccn}:provider_vs_penalties",
                                    "provider-info totals equal the summed penalty rows")
            if mismatches:
                run.error(f"{len(mismatches)} facilit(y/ies) where Provider Information totals "
                          "disagree with the summed penalty rows (stored as open conflicts)")
            log.info("%s: reconciliation %d matched, %d mismatched", COLLECTOR,
                     len(matched), len(mismatches))
    return run
