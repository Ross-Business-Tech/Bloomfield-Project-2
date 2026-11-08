"""
rank.py - scores, filters and ranks this week's flags, and works out what changed since last week.

Run it once a week after Parts A, B and C have run:
    cd pipeline
    python -m scorer.rank                 # scores today
    python -m scorer.rank --date 2026-11-09

Two scores per property (0-100), as in the build plan:
  desperation  - how much pressure the owner is under
  deal_quality - how good the deal could be for Bloomfield
All weights and filters come from scorer/settings.yaml.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from .signals import Property, find_signals
from .store import connect

SETTINGS_PATH = Path(__file__).with_name("settings.yaml")
PUSHER_LABELS = {
    "court_or_tax": "a court or tax authority",
    "lender": "the lender",
    "contractor": "contractors",
    "market": "the market",
}


def load_settings(path: str | Path | None = None) -> dict:
    with open(path or SETTINGS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------
def desperation(prop: Property, s: dict, today: date) -> tuple[int, list[dict]]:
    d = s["desperation"]
    parts = []
    weights = d["who_is_pushing"]
    pushers = sorted({sig.pusher for sig in prop.signals if sig.pusher}, key=lambda p: -weights.get(p, 0))
    if pushers:
        top = pushers[0]
        parts.append({"part": "Who's pushing", "points": weights[top], "why": f"Pressure from {PUSHER_LABELS[top]}"})
        for other in pushers[1:]:
            parts.append({"part": "Who's pushing", "points": weights[other] / 2,
                          "why": f"Also pressure from {PUSHER_LABELS[other]}"})

    stages = sorted({sig.stage for sig in prop.signals if sig.stage})
    if len(stages) > 1:
        pts = min(d["escalation_max"], d["escalation_points_per_step"] * (len(stages) - 1))
        names = {1: "lien", 2: "lawsuit", 3: "judgment / foreclosure / sale"}
        parts.append({"part": "Escalation", "points": pts, "why": " → ".join(names[x] for x in stages)})

    nxt = next_deadline(prop, today)
    if nxt:
        days = (nxt[0] - today).days
        for step in d["time_pressure"]:
            if days <= step["within_days"]:
                parts.append({"part": "Time pressure", "points": step["points"],
                              "why": f"{nxt[1]} in {days} days ({nxt[0]:%b %d, %Y})"})
                break

    kinds = {sig.code for sig in prop.signals}
    if len(kinds) > 1:
        pts = min(d["stacking_max"], d["stacking_points_per_extra_signal"] * (len(kinds) - 1))
        parts.append({"part": "Stacking", "points": pts, "why": f"{len(kinds)} different warning signs on one property"})

    used = [sig for sig in prop.signals if sig.options_used_up]
    if used:
        parts.append({"part": "Options used up", "points": d["options_used_up_points"],
                      "why": "; ".join(sorted({u.label for u in used}))})

    total = round(min(100, max(0, sum(p["points"] for p in parts))))
    return total, parts


def deal_quality(prop: Property, s: dict) -> tuple[int, list[dict]]:
    q = s["deal_quality"]
    f = s["filters"]
    parts = [{"part": "Start", "points": q["starting_points"], "why": "Every flag starts here"}]
    ptype = prop.property_type
    if ptype and ptype not in f["property_types_excluded"]:
        parts.append({"part": "Property type", "points": q["property_type_fit"], "why": f"{ptype} is a type Bloomfield buys"})
    amt = prop.loan_amount
    if amt and f["min_loan_size"] <= amt <= f["max_loan_size"]:
        parts.append({"part": "Size", "points": q["loan_size_fit"], "why": f"${amt / 1e6:.1f}M loan fits the size range"})
    if any(sig.temporary for sig in prop.signals):
        parts.append({"part": "Fixable problem", "points": q["temporary_problem"],
                      "why": "Looks like a delay or a stuck refinance, not a lack of demand"})
    if any(sig.permanent for sig in prop.signals):
        parts.append({"part": "Demand problem", "points": q["permanent_problem"], "why": "Low occupancy in a finished building"})
    if any(sig.public for sig in prop.signals):
        parts.append({"part": "Visibility", "points": q["public_sale"], "why": "Already at a public sale or auction, so others can see it"})
    else:
        parts.append({"part": "Visibility", "points": q["still_off_market"], "why": "Still off-market"})
    if prop.dscr is not None:
        if 0.8 <= prop.dscr < 1.0:
            parts.append({"part": "Income", "points": q["fixable_dscr"], "why": f"DSCR {prop.dscr:.2f}x is close to covering the loan"})
        elif prop.dscr < 0.6:
            parts.append({"part": "Income", "points": q["weak_dscr"], "why": f"DSCR {prop.dscr:.2f}x is far from covering the loan"})
    return round(min(100, max(0, sum(p["points"] for p in parts)))), parts


def next_deadline(prop: Property, today: date):
    dated = [(sig.deadline, sig.deadline_what) for sig in prop.signals if sig.deadline and sig.deadline >= today]
    return min(dated) if dated else None


def passes_filters(prop: Property, s: dict) -> tuple[bool, str | None]:
    f = s["filters"]
    if prop.property_type in f["property_types_excluded"]:
        return False, f"{prop.property_type} is filtered out"
    if f["states"] and (prop.state or "").upper() not in [x.upper() for x in f["states"]]:
        return False, f"{prop.state or 'Unknown state'} is outside the chosen states"
    amt = prop.loan_amount
    if amt and not (f["min_loan_size"] <= amt <= f["max_loan_size"]):
        return False, f"${amt / 1e6:.1f}M loan is outside ${f['min_loan_size'] / 1e6:.0f}M–${f['max_loan_size'] / 1e6:.0f}M"
    return True, None


def deal_angles(prop: Property) -> list[str]:
    codes = {sig.code for sig in prop.signals}
    angles = []
    if codes & {"ucc_sale", "foreclosure"}:
        nxt = [sig for sig in prop.signals if sig.code in ("ucc_sale", "foreclosure") and sig.deadline]
        when = f" before the {nxt[0].deadline_what} on {nxt[0].deadline:%b %d}" if nxt else ""
        angles.append(f"Rescue loan to pay off the pushing lender{when}.")
    if codes & {"stuck_construction_loan", "loan_extended"}:
        angles.append("Bridge loan or preferred equity to take out the construction loan until the building stabilizes.")
    if "bank_stress" in codes:
        angles.append(f"Note purchase: ask {prop.lender or 'the bank'} about selling this loan at a discount.")
    if "mechanics_lien" in codes:
        angles.append("Include money to clear contractor liens so the project can finish.")
    if "stale_opening" in codes and prop.property_type == "Student housing":
        angles.append("Carry the interest until next August's move-in, when rent starts.")
    if "auction" in codes:
        angles.append("Bid at the auction, or finance a buyer who will.")
    if "low_dscr" in codes and not angles:
        angles.append("Gap or mezzanine financing while income catches up with the loan.")
    return angles


def summary(prop: Property, desp: int, parts: list[dict], today: date) -> str:
    who = next((p["why"] for p in parts if p["part"] == "Who's pushing"), "Several warning signs")
    labels = ", ".join(sorted({sig.label.lower() for sig in prop.signals}))
    text = f"{prop.name}: {who.lower()}. Signs: {labels}."
    nxt = next_deadline(prop, today)
    if nxt:
        text += f" Next hard date: {nxt[1]} on {nxt[0]:%b %d, %Y}."
    return text[0].upper() + text[1:]


# ---------------------------------------------------------------------------
# The weekly run
# ---------------------------------------------------------------------------
def run(con, settings: dict | None = None, today: date | None = None) -> str:
    settings = settings or load_settings()
    today = today or date.today()
    run_id = f"run-{today.isoformat()}"
    props = find_signals(con, settings, today)

    rows = []
    for p in props:
        desp, dparts = desperation(p, settings, today)
        qual, qparts = deal_quality(p, settings)
        shown, reason = passes_filters(p, settings)
        nxt = next_deadline(p, today)
        rows.append({
            "run_id": run_id, "property_key": p.key, "property_name": p.name, "address": p.address,
            "city": p.city, "state": p.state, "property_type": p.property_type, "sponsor": p.sponsor,
            "lender": p.lender, "loan_amount": p.loan_amount, "desperation": desp, "deal_quality": qual,
            "rank": None, "shown": shown, "filter_reason": reason,
            "next_deadline": nxt[0] if nxt else None, "next_deadline_what": nxt[1] if nxt else None,
            "signals_json": json.dumps([sig.as_dict() for sig in p.signals]),
            "score_parts_json": json.dumps({"desperation": dparts, "deal_quality": qparts}),
            "angles_json": json.dumps(deal_angles(p)),
            "summary": summary(p, desp, dparts, today),
        })
    # Rank: most desperate first, deal quality breaks ties.
    shown = sorted([r for r in rows if r["shown"]], key=lambda r: (-r["desperation"], -r["deal_quality"]))
    for i, r in enumerate(shown, 1):
        r["rank"] = i

    con.execute("DELETE FROM flags WHERE run_id = ?", [run_id])
    con.execute("DELETE FROM score_runs WHERE run_id = ?", [run_id])
    cols = list(rows[0].keys()) if rows else []
    for r in rows:
        con.execute(f"INSERT INTO flags ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [r[c] for c in cols])
    con.execute(
        "INSERT INTO score_runs VALUES (?, ?, ?, ?, ?)",
        [run_id, today, datetime.now(timezone.utc).replace(tzinfo=None), json.dumps(settings), len(shown)],
    )
    return run_id


def changes(con, run_id: str | None = None, settings: dict | None = None) -> dict:
    """Compares a run with the run before it: new flags, worse, better, dropped off, new signals."""
    settings = settings or load_settings()
    runs = [r[0] for r in con.execute("SELECT run_id FROM score_runs ORDER BY run_date").fetchall()]
    if not runs:
        return {"current": None, "previous": None, "new": [], "worse": [], "better": [], "dropped": [], "new_signals": []}
    current = run_id or runs[-1]
    idx = runs.index(current)
    previous = runs[idx - 1] if idx > 0 else None

    def load(rid):
        if not rid:
            return {}
        df = con.execute("SELECT * FROM flags WHERE run_id = ? AND shown", [rid]).df()
        return {r["property_key"]: r for r in df.to_dict("records")}

    now, before = load(current), load(previous)
    jump = settings["changes"]["score_jump"]
    out = {"current": current, "previous": previous, "new": [], "worse": [], "better": [], "dropped": [], "new_signals": []}
    for key, r in now.items():
        if key not in before:
            out["new"].append(r)
            continue
        b = before[key]
        delta = r["desperation"] - b["desperation"]
        r = {**r, "previous_desperation": b["desperation"], "delta": delta}
        if delta >= jump:
            out["worse"].append(r)
        elif delta <= -jump:
            out["better"].append(r)
        old_codes = {x["code"] for x in json.loads(b["signals_json"])}
        added = [x for x in json.loads(r["signals_json"]) if x["code"] not in old_codes]
        if added:
            out["new_signals"].append({**r, "added": added})
    out["dropped"] = [b for key, b in before.items() if key not in now]
    for k in ("new", "worse", "better"):
        out[k].sort(key=lambda r: -r["desperation"])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score and rank this week's flags.")
    ap.add_argument("--date", help="score as of this date (YYYY-MM-DD); default today")
    ap.add_argument("--db", help="database file; default pipeline/data/bloomfield.duckdb")
    ap.add_argument("--settings", help="settings file; default scorer/settings.yaml")
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.date) if args.date else date.today()
    con = connect(args.db)
    settings = load_settings(args.settings)
    run_id = run(con, settings, today)
    top = con.execute(
        "SELECT rank, property_name, desperation, deal_quality FROM flags WHERE run_id = ? AND shown ORDER BY rank LIMIT 10",
        [run_id],
    ).fetchall()
    hidden = con.execute("SELECT count(*) FROM flags WHERE run_id = ? AND NOT shown", [run_id]).fetchone()[0]
    print(f"Scored {run_id}: {len(top)} shown (top 10 below), {hidden} filtered out.")
    for rank, name, d, q in top:
        print(f"  {rank:>2}. {name:<40} desperation {d:>3}  deal quality {q:>3}")
    ch = changes(con, run_id, settings)
    if ch["previous"]:
        print(f"Since {ch['previous']}: {len(ch['new'])} new, {len(ch['worse'])} worse, "
              f"{len(ch['better'])} better, {len(ch['dropped'])} dropped off.")


if __name__ == "__main__":
    main()
