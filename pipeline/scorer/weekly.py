"""
weekly.py - the weekly run behind the dashboard (Part D).

    Part A collectors -> Part B scout/reader -> Part C matcher -> THIS -> dashboard/app.py

1. Scores every property with the Bloomfield Opportunity Score (scorer/equation.py).
2. Applies Bloomfield's filters (office off, $5M-$50M, states) and ranks what's left.
3. Adds "how good the deal could be" and a suggested deal angle for each flag.
4. Saves the run, so the dashboard can show what changed since last week.
5. Owns research_log: what the researcher did with each flag.

Run:   python -m scorer.weekly                  (score today)
       python -m scorer.weekly --as-of 2026-11-09
All numbers are in config/equation.yaml.
"""
from __future__ import annotations

import argparse
import json
from datetime import date

from collectors.common import db

from . import equation as eq
from .schema import open_db
from .signals import filing_kind

SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_runs (
    run_id        TEXT PRIMARY KEY,
    as_of         DATE,
    created_at    TIMESTAMP,
    version       TEXT,
    settings_json TEXT,
    shown_count   INTEGER
);
-- One row per property per weekly run (kept, so weeks can be compared).
CREATE TABLE IF NOT EXISTS weekly_flags (
    run_id          TEXT,
    property_id     TEXT,
    property_name   TEXT,
    address         TEXT,
    city            TEXT,
    state           TEXT,
    property_type   TEXT,
    borrower        TEXT,
    lender          TEXT,
    loan_amount     DOUBLE,
    score           DOUBLE,
    tier            TEXT,       -- hot / watch / low
    stage           TEXT,       -- early / actionable
    flags           TEXT,       -- STANDOUT / COUNTER-SIGNAL
    l_loan_clock    DOUBLE,
    o_opening       DOUBLE,
    r_rent          DOUBLE,
    i_inspection    DOUBLE,
    signals_showing INTEGER,
    corroboration   DOUBLE,
    early_points    DOUBLE,
    late_points     DOUBLE,
    deal_quality    INTEGER,
    rank            INTEGER,    -- 1 = top; NULL when filtered out
    shown           BOOLEAN,
    filter_reason   TEXT,
    explanation     TEXT,       -- JSON: every term of the equation and why
    deal_parts      TEXT,       -- JSON: how deal quality was built
    angles          TEXT,       -- JSON: suggested deal angles
    context         TEXT,       -- JSON: facts shown but not scored (auctions, Trepp/CMBS)
    summary         TEXT,
    PRIMARY KEY (run_id, property_id)
);
-- What the researcher did. Never overwritten: every action is a new row.
CREATE TABLE IF NOT EXISTS research_log (
    log_id        TEXT PRIMARY KEY,
    property_id   TEXT,
    property_name TEXT,
    status        TEXT,   -- looked_into / called / meeting / passed / deal_closed
    note          TEXT,
    deal_amount   DOUBLE,
    researcher    TEXT,
    logged_at     TIMESTAMP
);
"""

RESEARCH_STATUSES = ["looked_into", "called", "meeting", "passed", "deal_closed"]
STATUS_LABELS = {"looked_into": "Looked into", "called": "Called", "meeting": "Meeting set",
                 "passed": "Passed", "deal_closed": "Deal closed"}

_TYPE_RULES = [
    ("student", "Student housing"), ("senior", "Senior housing"), ("assisted", "Senior housing"),
    ("nursing", "Senior housing"), ("multi", "Multifamily"), ("apartment", "Multifamily"),
    ("office", "Office"), ("hotel", "Hotel"), ("lodging", "Hotel"), ("industrial", "Industrial"),
    ("warehouse", "Industrial"), ("storage", "Self storage"), ("retail", "Retail"), ("mixed", "Mixed use"),
    ("medical", "Office"),
]
_CMBS_CODES = {"MF": "Multifamily", "OF": "Office", "RT": "Retail", "LO": "Hotel", "IN": "Industrial",
               "SS": "Self storage", "MU": "Mixed use", "HC": "Senior housing", "WH": "Industrial"}


def create_tables(con) -> None:
    eq.create_equation_tables(con)
    con.execute(SCHEMA)


def clean_type(raw) -> str | None:
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    if text.upper() in _CMBS_CODES:
        return _CMBS_CODES[text.upper()]
    low = text.lower().replace("_", " ")
    for needle, label in _TYPE_RULES:
        if needle in low:
            return label
    return text.replace("_", " ").title()


def property_type(p) -> str | None:
    for row in p.projects + p.filings + p.cmbs_loans:
        t = clean_type(row.get("property_type"))
        if t:
            return t
    name = " ".join(str(r.get("project_name") or r.get("property_name") or "") for r in p.projects + p.cmbs_loans + p.filings)
    return clean_type(name) if clean_type(name) in {v for _, v in _TYPE_RULES} else None


def passes_filters(ptype, state, amount, f) -> tuple[bool, str | None]:
    if ptype in f["property_types_excluded"]:
        return False, f"{ptype} is filtered out"
    if f["states"] and (state or "").upper() not in [s.upper() for s in f["states"]]:
        return False, f"{state or 'Unknown state'} is outside the chosen states"
    if amount and not (f["min_loan_size"] <= amount <= f["max_loan_size"]):
        return False, f"${amount / 1e6:.1f}M loan is outside ${f['min_loan_size'] / 1e6:.0f}M-${f['max_loan_size'] / 1e6:.0f}M"
    return True, None


def context_facts(p) -> list[str]:
    """Shown to the researcher but NOT scored (Bloomfield: public / everyone has it)."""
    out = []
    for f in p.filings:
        if filing_kind(f) == "auction_listing":
            out.append(f"Listed at auction{(' on ' + str(f.get('auction_date'))) if f.get('auction_date') else ''} (public)")
    for ln in p.cmbs_loans:
        bits = []
        if ln.get("dscr") is not None:
            bits.append(f"DSCR {ln['dscr']:.2f}x")
        if ln.get("watchlist_flag"):
            bits.append("on watchlist")
        if ln.get("payment_status"):
            bits.append(str(ln["payment_status"]))
        if bits:
            out.append(f"{ln.get('source') or 'CMBS'} loan data: " + ", ".join(bits) + " (everyone has this)")
    return out


def is_public(p) -> bool:
    return any(filing_kind(f) in ("auction_listing", "ucc_sale_notice", "foreclosure_notice") for f in p.filings)


def deal_quality(r: eq.Result, ptype, amount, public, cfg) -> tuple[int, list[dict]]:
    q, f = cfg["deal_quality"], cfg["filters"]
    parts = [{"part": "Start", "points": q["starting_points"], "why": "Every flag starts here"}]
    if ptype and ptype not in f["property_types_excluded"]:
        parts.append({"part": "Property type", "points": q["property_type_fit"], "why": f"{ptype} is a type Bloomfield buys"})
    if amount and f["min_loan_size"] <= amount <= f["max_loan_size"]:
        parts.append({"part": "Size", "points": q["loan_size_fit"], "why": f"${amount / 1e6:.1f}M fits the size range"})
    if r.L.value > 0 or r.O.value > 0:
        parts.append({"part": "Fixable problem", "points": q["fixable_problem"],
                      "why": "Loan on the clock / late opening: a delay or stuck refinance, not a lack of demand"})
    if r.R.value >= 0.5:
        parts.append({"part": "Demand", "points": q["demand_problem"], "why": "Rent is falling faster than its city"})
    if public:
        parts.append({"part": "Visibility", "points": q["public_sale"], "why": "Already public (auction / sale notice)"})
    else:
        parts.append({"part": "Visibility", "points": q["off_market"], "why": "Still off-market"})
    return max(0, min(100, round(sum(x["points"] for x in parts)))), parts


def deal_angles(r: eq.Result, p, ptype) -> list[str]:
    a = []
    if r.L.value > 0:
        a.append("Bridge loan or preferred equity to take out the construction loan until the building stabilizes.")
    if r.O.value > 0 and ptype == "Student housing":
        a.append("Carry the interest until next August's move-in, when rent starts.")
    kinds = {filing_kind(f) for f in p.filings}
    if kinds & {"ucc_sale_notice", "foreclosure_notice", "lis_pendens", "lender_lawsuit"}:
        a.append("Rescue loan to pay off the pushing lender before the sale date.")
    if "mechanics_lien" in kinds:
        a.append("Include money to clear contractor liens so the project can finish.")
    if r.I.value > 0.5 and r.L.value == 0:
        a.append("Site has gone quiet: completion financing may get it moving again.")
    if r.counters:
        a.append("Counter-signal present: confirm the distress before calling.")
    return a


def summary(r: eq.Result, name) -> str:
    terms = [("loan clock", r.L), ("opening delay", r.O), ("rent decline", r.R), ("site inactivity", r.I)]
    on = [f"{n} ({part.value:.0%})" for n, part in terms if part.value > 0]
    text = f"{name}: " + (", ".join(on) if on else "no early signals")
    if r.standouts:
        text += ". STANDOUT"
    if r.counters:
        text += ". COUNTER-SIGNAL: " + "; ".join(r.counters)
    if r.late_why:
        text += ". Already happened: " + "; ".join(r.late_why)
    return text + "."


def run(con, cfg: dict | None = None, as_of: date | None = None) -> str:
    cfg = cfg or eq.load_equation_config()
    as_of = as_of or date.today()
    create_tables(con)
    rents, market, inspections = eq.load_inputs(con)
    from .group import group_properties
    props = group_properties(con)
    run_id = f"week-{as_of.isoformat()}"
    rows = []
    for p in props:
        r = eq.score_property(p, cfg, as_of, rents, market, inspections)
        if r.score <= 0 and not context_facts(p):
            continue                              # nothing to show
        fx = r.facts
        ptype = property_type(p)
        amount = fx.get("loan_amount")
        shown, reason = passes_filters(ptype, fx.get("state"), amount, cfg["filters"])
        dq, dparts = deal_quality(r, ptype, amount, is_public(p), cfg)
        name = fx.get("property_name") or fx.get("property_address") or p.property_id
        rows.append({
            "run_id": run_id, "property_id": p.property_id, "property_name": name,
            "address": fx.get("property_address"), "city": fx.get("city"), "state": fx.get("state"),
            "property_type": ptype, "borrower": fx.get("borrower_name"), "lender": fx.get("lender_name"),
            "loan_amount": amount, "score": r.score, "tier": r.tier, "stage": r.stage, "flags": eq.flag_text(r),
            "l_loan_clock": r.L.value, "o_opening": r.O.value, "r_rent": r.R.value, "i_inspection": r.I.value,
            "signals_showing": r.showing, "corroboration": r.corroboration, "early_points": round(r.early, 2),
            "late_points": round(r.late, 2), "deal_quality": dq, "rank": None, "shown": shown,
            "filter_reason": reason, "explanation": json.dumps(eq.explain(r, cfg)), "deal_parts": json.dumps(dparts),
            "angles": json.dumps(deal_angles(r, p, ptype)), "context": json.dumps(context_facts(p)),
            "summary": summary(r, name),
        })
    ranked = sorted([x for x in rows if x["shown"] and x["score"] > 0],
                    key=lambda x: (-x["score"], -x["deal_quality"], x["property_id"]))
    for i, x in enumerate(ranked, 1):
        x["rank"] = i
    con.execute("DELETE FROM weekly_flags WHERE run_id = ?", [run_id])
    con.execute("DELETE FROM weekly_runs WHERE run_id = ?", [run_id])
    for x in rows:
        cols = list(x)
        con.execute(f"INSERT INTO weekly_flags ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                    [x[c] for c in cols])
    con.execute("INSERT INTO weekly_runs VALUES (?, ?, ?, ?, ?, ?)",
                [run_id, as_of, db.utc_now(), cfg["version"], json.dumps(cfg), len(ranked)])
    return run_id


def changes(con, run_id: str | None = None, cfg: dict | None = None) -> dict:
    """New flags, got worse / better, new signals, dropped off - compared with the run before."""
    cfg = cfg or eq.load_equation_config()
    runs = [x[0] for x in con.execute("SELECT run_id FROM weekly_runs ORDER BY as_of").fetchall()]
    empty = {"current": None, "previous": None, "new": [], "worse": [], "better": [], "dropped": [],
             "new_standouts": [], "new_late": []}
    if not runs:
        return empty
    current = run_id or runs[-1]
    i = runs.index(current)
    previous = runs[i - 1] if i > 0 else None

    def load(rid):
        if not rid:
            return {}
        df = con.execute("SELECT * FROM weekly_flags WHERE run_id = ? AND shown AND score > 0", [rid]).df()
        return {x["property_id"]: x for x in df.to_dict("records")}

    now, before = load(current), load(previous)
    out = {**empty, "current": current, "previous": previous}
    jump = cfg["changes"]["score_jump"]
    for pid, x in now.items():
        if pid not in before:
            out["new"].append(x)
            continue
        b = before[pid]
        x = {**x, "previous_score": b["score"], "delta": round(x["score"] - b["score"], 1)}
        if x["delta"] >= jump:
            out["worse"].append(x)
        elif x["delta"] <= -jump:
            out["better"].append(x)
        if "STANDOUT" in (x["flags"] or "") and "STANDOUT" not in (b["flags"] or ""):
            out["new_standouts"].append(x)
        if x["late_points"] > (b["late_points"] or 0):
            out["new_late"].append(x)
    out["dropped"] = [b for pid, b in before.items() if pid not in now]
    for k in ("new", "worse", "better"):
        out[k].sort(key=lambda x: -x["score"])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Weekly scoring run for the dashboard.")
    ap.add_argument("--as-of", help="YYYY-MM-DD (default today)")
    ap.add_argument("--db", help="database file (default data/bloomfield.duckdb)")
    args = ap.parse_args(argv)
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    con = open_db(args.db)
    run_id = run(con, None, as_of)
    top = con.execute("SELECT rank, property_name, score, tier, flags FROM weekly_flags "
                      "WHERE run_id = ? AND rank IS NOT NULL ORDER BY rank LIMIT 10", [run_id]).fetchall()
    print(f"{run_id}: {len(top)} ranked (top 10):")
    for rank, name, score, tier, flags in top:
        print(f"  {rank:>2}. {score:>5.1f} {tier:<5} {name}" + (f"  [{flags}]" if flags else ""))
    ch = changes(con, run_id)
    if ch["previous"]:
        print(f"Since {ch['previous']}: {len(ch['new'])} new, {len(ch['worse'])} worse, "
              f"{len(ch['better'])} better, {len(ch['dropped'])} dropped off.")


if __name__ == "__main__":
    main()
