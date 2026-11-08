"""
loan_history.py - tell each recorded loan's story from the county recorder index.

In plain English: on one property we might see, in date order:
    construction mortgage -> modification -> extension -> permanent mortgage -> discharge
This file turns that pile into one row per original loan that says: how many times it was
modified, whether a permanent loan replaced it ("refinanced"), whether it was paid off
("released"), and how long it has been open.

How follow-up documents find their loan:
  1. the follow-up names the original instrument number (related_instrument) -> that loan
  2. otherwise a modification/extension/assignment goes to the newest open loan before it,
     and a release goes to the oldest open loan before it (the old loan is the one paid off).
"""
from __future__ import annotations

from datetime import date, datetime

ORIGINAL = {"construction", "permanent", "unknown"}
FOLLOW_UP = {"modification", "extension", "assignment", "release"}


def _d(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def build_histories(recorded: list[dict], record_property: dict[str, str],
                    lender_rssd: dict[str, str], as_of: date) -> list[dict]:
    # Group by property (records the property matcher couldn't place stand alone).
    groups: dict[str, list[dict]] = {}
    for r in recorded:
        pid = record_property.get(f"recorded_loans:{r['record_id']}") or f"none:{r['record_id']}"
        groups.setdefault(pid, []).append(r)

    out = []
    for pid, recs in groups.items():
        recs.sort(key=lambda r: (_d(r.get("recording_date")) or date.min, r["record_id"]))
        stories: list[dict] = []
        by_inst: dict[str, dict] = {}

        def open_before(d):
            return [s for s in stories if s["status"] == "open" and (s["recording_date"] or date.min) <= (d or date.max)]

        for r in recs:
            kind = (r.get("loan_kind") or "unknown").lower()
            d = _d(r.get("recording_date"))
            if kind in ORIGINAL:
                # A new permanent loan takes out every open construction loan before it.
                if kind == "permanent":
                    for s in open_before(d):
                        if s["loan_kind"] == "construction":
                            s.update(status="refinanced", takeout_record_id=r["record_id"], takeout_date=d)
                s = {"history_id": r["record_id"],
                     "property_id": None if pid.startswith("none:") else pid,
                     "instrument_number": r.get("instrument_number"), "loan_kind": kind,
                     "recording_date": d, "amount": r.get("amount"),
                     "borrower_name": r.get("grantor"), "lender_name": r.get("grantee"),
                     "lender_rssd_id": lender_rssd.get((r.get("grantee") or "").strip()),
                     "modification_count": 0, "last_modification_date": None, "assignment_count": 0,
                     "takeout_record_id": None, "takeout_date": None,
                     "release_record_id": None, "release_date": None,
                     "status": "open", "record_ids": [r["record_id"]]}
                stories.append(s)
                if r.get("instrument_number"):
                    by_inst[r["instrument_number"]] = s
                continue
            if kind not in FOLLOW_UP:
                continue
            target = by_inst.get(r.get("related_instrument") or "")
            if target is None:
                candidates = [s for s in stories if (s["recording_date"] or date.min) <= (d or date.max)
                              and (kind != "release" or s["status"] != "released")]
                open_ones = [s for s in candidates if s["status"] == "open"] or candidates
                if not open_ones:
                    continue
                target = open_ones[0] if kind == "release" else open_ones[-1]
            target["record_ids"].append(r["record_id"])
            if kind in ("modification", "extension"):
                target["modification_count"] += 1
                target["last_modification_date"] = d
            elif kind == "assignment":
                target["assignment_count"] += 1
            elif kind == "release":
                target.update(status="released", release_record_id=r["record_id"], release_date=d)

        for s in stories:
            end = s["release_date"] or s["takeout_date"] or as_of
            s["months_open"] = round((end - s["recording_date"]).days / 30.44, 1) if s["recording_date"] else None
            s["record_ids"] = ", ".join(s["record_ids"])
            out.append(s)
    return out
