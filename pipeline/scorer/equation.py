"""
equation.py - the Bloomfield Opportunity Score (0-100), built from Bloomfield's feedback.

    Score = min(100,  C(k) x (50 L + 20 O + 15 R + 15 I)  +  Late)

  L  loan clock        construction loan still unpaid, 30 -> 60 months      (Part A recorded_loans)
  O  opening delay     months past the latest reasonable opening, 6 -> 18  (Part B projects)
  R  rent decline      property rent change vs. its city's, 0 -> 15 pts    (Part B rent_observations
                                                                            + Part A market_rents)
  I  inspection gap    days with no city inspection on an open permit,    (Part A permit_inspections
                       60 -> 180                                           + Part B filing_fields permits)
  C(k) anti-fluke      k = how many of L, O, R, I show (>= 0.25):  1 -> 0.5, 2 -> 0.8, 3+ -> 1.0
  Late                 court/UCC/foreclosure filings and contractor liens, at most 10 points

Records are grouped into properties by Part C (scorer.group uses Part C's property_links).
Every number comes from config/equation.yaml. The plain-English version is scorer/EQUATION.md.

Run:   python -m scorer.equation                 (score everything, print the top 15)
       python -m scorer.equation --explain Kestrel
       python -m scorer.equation --as-of 2026-11-10
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from collectors.common import config, db

from .group import Property, group_properties
from .schema import open_db, table_exists
from .score import property_facts
from .signals import as_date, filing_date, filing_kind, months_between

EQUATION_YAML = config.CONFIG_DIR / "equation.yaml"

# New inputs the equation needs. Nobody else defines these tables, so creating them is safe.
INPUT_SCHEMA = """
-- Part B (scout): asking rent read off a project's leasing page, one row per reading.
CREATE TABLE IF NOT EXISTS rent_observations (
    project_id    TEXT,        -- links to projects
    observed_at   DATE,
    asking_rent   DOUBLE,      -- lowest advertised rent, $/month
    unit_basis    TEXT,        -- unit / bed / sqft
    source_url    TEXT,
    file_id       TEXT         -- the saved page in raw_files
);
-- Part A: a market rent index per city (e.g. HUD Fair Market Rents, Census ACS, Zillow ZORI).
CREATE TABLE IF NOT EXISTS market_rents (
    city          TEXT,
    state         TEXT,
    period        DATE,
    rent_index    DOUBLE,
    source        TEXT
);
-- Part A (permits.py): every inspection recorded on a building permit.
CREATE TABLE IF NOT EXISTS permit_inspections (
    permit_number    TEXT,
    state            TEXT,
    city             TEXT,
    inspection_date  DATE,
    inspection_type  TEXT,
    result           TEXT,
    file_id          TEXT
);
-- Output: one row per property per run.
CREATE TABLE IF NOT EXISTS equation_scores (
    property_id    TEXT PRIMARY KEY,
    property_name  TEXT,
    city           TEXT,
    state          TEXT,
    loan_amount    DOUBLE,
    score          DOUBLE,
    tier           TEXT,
    stage          TEXT,      -- early / actionable
    flags          TEXT,      -- e.g. "STANDOUT" / "COUNTER-SIGNAL"
    l_loan_clock   DOUBLE,
    o_opening      DOUBLE,
    r_rent         DOUBLE,
    i_inspection   DOUBLE,
    signals_showing INTEGER,
    corroboration  DOUBLE,
    early_points   DOUBLE,
    late_points    DOUBLE,
    explanation    JSON,
    as_of          DATE,
    version        TEXT,
    scored_at      TIMESTAMP
);
"""

STANDOUT_LABEL = {"loan_clock": "Standout loan clock", "opening_delay": "Standout opening delay",
                  "rent_decline": "Standout rent decline", "inspection_gap": "Standout site inactivity"}

COURT_KINDS = {"foreclosure_notice", "receivership", "lis_pendens", "lender_lawsuit", "ucc_sale_notice",
               "tax_delinquency"}
COURT_TAGS = {"foreclosure_scheduled", "receiver_appointed", "lis_pendens", "lender_lawsuit",
              "ucc_sale_scheduled", "tax_delinquent"}


def load_equation_config(path: str | Path | None = None) -> dict:
    with open(path or EQUATION_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def create_equation_tables(con) -> None:
    con.execute(INPUT_SCHEMA)


def ramp(x: float | None, start: float, full: float) -> float:
    """0 at `start`, rising in a straight line to 1 at `full`."""
    if x is None or x <= start:
        return 0.0
    return min(1.0, (x - start) / (full - start))


@dataclass
class Part:
    value: float = 0.0             # strength 0..1
    known: bool = False            # False = no data, so it can't confirm anything
    why: str = "no data"
    counter: str | None = None     # strong evidence the OTHER way (downside extreme)


@dataclass
class Result:
    property_id: str
    facts: dict
    L: Part = field(default_factory=Part)
    O: Part = field(default_factory=Part)
    R: Part = field(default_factory=Part)
    I: Part = field(default_factory=Part)
    showing: int = 0
    corroboration: float = 0.0
    early: float = 0.0
    late: float = 0.0
    late_why: list = field(default_factory=list)
    standouts: list = field(default_factory=list)
    counters: list = field(default_factory=list)
    score: float = 0.0
    tier: str = "low"
    stage: str = "early"


# ---------------------------------------------------------------------------
# L: construction loan on the clock (Part A recorded_loans)
# ---------------------------------------------------------------------------
def loan_clock(p: Property, c: dict, as_of: date) -> Part:
    loans = sorted(p.recorded_loans, key=lambda r: as_date(r.get("recording_date")) or date.min)
    built = [r for r in loans if (r.get("loan_kind") or "").lower() == "construction"]
    if not built:
        return Part()
    con_loan = built[-1]
    start = as_date(con_loan.get("recording_date"))
    if not start:
        return Part()
    later = [r for r in loans if (as_date(r.get("recording_date")) or date.min) > start]
    if any((r.get("loan_kind") or "").lower() in ("permanent", "release") for r in later):
        return Part(0.0, True, "construction loan was paid off by a permanent loan or release")
    months = months_between(start, as_of)
    ext = sum(1 for r in later if (r.get("loan_kind") or "").lower() in ("extension", "modification"))
    base = ramp(months, c["start_months"], c["full_months"])
    bonus = min(c["max_extension_bonus"], c["per_extension"] * ext) if base > 0 else 0.0
    value = min(1.0, base + bonus)
    why = f"construction loan recorded {months:.0f} months ago, still not paid off"
    if ext:
        why += f"; {ext} extension(s)/modification(s)"
    return Part(value, True, why)


# ---------------------------------------------------------------------------
# O: opening date watch (Part B projects)
# ---------------------------------------------------------------------------
def opening_delay(p: Property, c: dict, as_of: date) -> Part:
    best = Part()
    for pr in p.projects:
        overdue = pr.get("months_overdue")
        deadline = as_date(pr.get("opening_deadline"))
        if overdue is None and deadline:
            overdue = max(0.0, months_between(deadline, as_of))
        if overdue is None:
            continue
        if not pr.get("stale_candidate") and not pr.get("page_still_unopened"):
            overdue = 0.0                    # the project says it's open
        base = ramp(overdue, c["start_months"], c["full_months"])
        bonus = 0.0
        if overdue > 0:
            if pr.get("missed_lease_up"):
                bonus += c["missed_lease_up_bonus"]
            bonus += min(c["max_slip_bonus"], c["per_slip"] * (pr.get("date_change_count") or 0))
        value = min(1.0, base + bonus)
        why = f"'{pr.get('stated_opening_phrase') or 'opening date'}' is {overdue:.0f} months overdue"
        if overdue < c["start_months"]:
            why += f" (under {c['start_months']} months is normal)"
        if value >= best.value:
            best = Part(value, True, why)
    return best


# ---------------------------------------------------------------------------
# R: relative rent decline (Part B rent_observations vs. Part A market_rents)
# ---------------------------------------------------------------------------
def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2) if xs else None


def rent_decline(p: Property, facts: dict, c: dict, rents: dict, market: dict, as_of: date,
                 cfg_counter: dict | None = None) -> Part:
    cfg_counter = cfg_counter or {"rent_beats_city_by": 9e9}
    obs = []
    for pr in p.projects:
        obs += rents.get(str(pr.get("project_id")), [])
    obs = [(d, r) for d, r in obs if d and r and (as_of - d).days <= c["window_days"] and d <= as_of]
    if len(obs) < c["min_observations"]:
        return Part()
    obs.sort()
    first_d, last_d = obs[0][0], obs[-1][0]
    if (last_d - first_d).days < c["min_days_between"]:
        return Part()
    # Medians of the early and late halves, so one odd reading can't swing the result.
    half = len(obs) // 2 or 1
    then = _median([r for _, r in obs[:half]])
    now = _median([r for _, r in obs[half:]] or [obs[-1][1]])
    prop_change = (now - then) / then
    key = ((facts.get("city") or "").strip().lower(), (facts.get("state") or "").strip().upper())
    series = market.get(key)
    if not series:
        return Part(0.0, False, f"property rent changed {prop_change:+.1%}, but no market rents for its city")
    m_then = _closest(series, first_d)
    m_now = _closest(series, last_d)
    mkt_change = (m_now - m_then) / m_then if m_then else 0.0
    gap = mkt_change - prop_change
    value = max(0.0, min(1.0, gap / c["full_gap"]))
    counter = (f"rent beating its city by {-gap * 100:.1f} points"
               if gap <= -cfg_counter["rent_beats_city_by"] else None)
    return Part(value, True, counter=counter, why= f"property rent {prop_change:+.1%} vs. its city {mkt_change:+.1%} "
                             f"({gap * 100:.1f} points worse)" if gap > 0 else
                f"property rent {prop_change:+.1%} vs. its city {mkt_change:+.1%} (keeping up)")


def _closest(series, d):
    return min(series, key=lambda t: abs((t[0] - d).days))[1]


# ---------------------------------------------------------------------------
# I: construction inspection inactivity (Part A permit_inspections + Part B permits)
# ---------------------------------------------------------------------------
def inspection_gap(p: Property, c: dict, inspections: dict, as_of: date, cfg_counter: dict | None = None) -> Part:
    cfg_counter = cfg_counter or {"inspected_within_days": -1}
    if any(filing_kind(f) == "certificate_of_occupancy" for f in p.filings):
        return Part(0.0, True, "certificate of occupancy issued: building is finished")
    permits = [f for f in p.filings if filing_kind(f) == "building_permit"]
    if not permits:
        return Part()
    best = Part()
    for f in permits:
        num = str(f.get("permit_number") or "")
        dates = [d for d in inspections.get(num, []) if d and d <= as_of]
        last = max(dates) if dates else filing_date(f)
        if not last:
            continue
        days = (as_of - last).days
        value = ramp(days, c["start_days"], c["full_days"])
        what = "last inspection" if dates else "permit issued (no inspections on record)"
        part = Part(value, True, f"permit {num or '?'}: {days} days since {what}")
        if dates and days <= cfg_counter["inspected_within_days"]:
            part.counter = f"city inspected the site {days} days ago (construction is active)"
        if part.value > best.value or (part.value == best.value and part.counter):
            best = part
    return best


# ---------------------------------------------------------------------------
# Late: already happened, so a small add-on (Part B filing_fields)
# ---------------------------------------------------------------------------
def late_points(p: Property, facts: dict, c: dict, as_of: date) -> tuple[float, list[str]]:
    def weight(f):
        d = filing_date(f)
        return 1.0 if d and (as_of - d).days <= c["recent_days"] else 0.5

    why, court = [], 0.0
    for f in p.filings:
        tags = set(f.get("distress_tags") or []) if isinstance(f.get("distress_tags"), list) else set(
            json.loads(f["distress_tags"]) if isinstance(f.get("distress_tags"), str) and f["distress_tags"].startswith("[") else [])
        if filing_kind(f) in COURT_KINDS or tags & COURT_TAGS:
            pts = c["court_points"] * weight(f)
            if pts > court:
                court = pts
                why = [f"court/legal filing: {filing_kind(f).replace('_', ' ')}"]
    liens = 0.0
    for f in p.filings:
        if filing_kind(f) != "mechanics_lien":
            continue
        n = 2 if (f.get("lien_amount") and facts.get("loan_amount")
                  and f["lien_amount"] >= c["big_lien_share"] * facts["loan_amount"]) else 1
        liens += c["lien_points"] * n * weight(f)
        why.append(f"contractor lien by {f.get('lien_claimant') or 'a contractor'}")
    liens = min(c["max_lien_points"], liens)
    return min(c["max_points"], court + liens), why


# ---------------------------------------------------------------------------
# The equation
# ---------------------------------------------------------------------------
def score_property(p: Property, cfg: dict, as_of: date, rents: dict, market: dict, inspections: dict) -> Result:
    facts = property_facts(p)
    r = Result(p.property_id, facts)
    r.L = loan_clock(p, cfg["loan_clock"], as_of)
    r.O = opening_delay(p, cfg["opening_delay"], as_of)
    r.R = rent_decline(p, facts, cfg["rent_decline"], rents, market, as_of, cfg["counter_signal"])
    r.I = inspection_gap(p, cfg["inspection_gap"], inspections, as_of, cfg["counter_signal"])
    w = cfg["weights"]
    parts = {"loan_clock": r.L, "opening_delay": r.O, "rent_decline": r.R, "inspection_gap": r.I}
    r.showing = sum(1 for part in parts.values() if part.value >= cfg["shows_at"])
    r.corroboration = float(cfg["corroboration"][min(r.showing, 4)])
    so = cfg["standout"]
    early = 0.0
    for name, part in parts.items():
        if part.value >= so["at_strength"]:
            # Upside extreme: counts in full, plus a boost, whatever the other signals say.
            early += w[name] * part.value * so["boost"]
            r.standouts.append(f"{STANDOUT_LABEL[name]}: {part.why}")
        else:
            early += w[name] * part.value * r.corroboration
        if part.counter:
            r.counters.append(part.counter)
    # Downside extreme: each counter-signal cuts the early points.
    r.early = early * cfg["counter_signal"]["factor"] ** len(r.counters)
    r.late, r.late_why = late_points(p, facts, cfg["late"], as_of)
    r.score = round(min(cfg["max_score"], r.early + r.late), 1)
    t = cfg["tiers"]
    r.tier = "hot" if r.score >= t["hot"] else "watch" if r.score >= t["watch"] else "low"
    r.stage = "actionable" if r.late > 0 else "early"
    return r


def load_inputs(con):
    rents, market, inspections = {}, {}, {}
    if table_exists(con, "rent_observations"):
        for pid, d, rent in con.execute("SELECT project_id, observed_at, asking_rent FROM rent_observations").fetchall():
            rents.setdefault(str(pid), []).append((as_date(d), rent))
    if table_exists(con, "market_rents"):
        for city, st, d, idx in con.execute("SELECT city, state, period, rent_index FROM market_rents").fetchall():
            market.setdefault(((city or "").strip().lower(), (st or "").strip().upper()), []).append((as_date(d), idx))
    if table_exists(con, "permit_inspections"):
        for num, d in con.execute("SELECT permit_number, inspection_date FROM permit_inspections").fetchall():
            inspections.setdefault(str(num), []).append(as_date(d))
    return rents, market, inspections


def score_all(con, cfg: dict | None = None, as_of: date | None = None) -> list[Result]:
    cfg = cfg or load_equation_config()
    as_of = as_of or date.today()
    rents, market, inspections = load_inputs(con)
    out = [score_property(p, cfg, as_of, rents, market, inspections) for p in group_properties(con)]
    out.sort(key=lambda r: (-r.score, -(r.facts.get("loan_amount") or 0), r.property_id))
    return out


def explain(r: Result, cfg: dict) -> dict:
    w = cfg["weights"]
    return {
        "L_loan_clock": {"strength": round(r.L.value, 3), "points": round(w["loan_clock"] * r.L.value, 1), "why": r.L.why},
        "O_opening": {"strength": round(r.O.value, 3), "points": round(w["opening_delay"] * r.O.value, 1), "why": r.O.why},
        "R_rent": {"strength": round(r.R.value, 3), "points": round(w["rent_decline"] * r.R.value, 1), "why": r.R.why},
        "I_inspection": {"strength": round(r.I.value, 3), "points": round(w["inspection_gap"] * r.I.value, 1), "why": r.I.why},
        "anti_fluke": f"{r.showing} early signal(s) showing -> keeps {r.corroboration:.0%} of early points",
        "standouts": r.standouts,
        "counter_signals": r.counters,
        "early_points": round(r.early, 1),
        "late_points": round(r.late, 1),
        "late_why": r.late_why,
        "score": r.score,
    }


def flag_text(r: Result) -> str:
    out = []
    if r.standouts:
        out.append("STANDOUT")
    if r.counters:
        out.append("COUNTER-SIGNAL")
    return " ".join(out)


def write_scores(con, results: list[Result], cfg: dict, as_of: date) -> None:
    con.execute("DROP TABLE IF EXISTS equation_scores")   # output only: rebuilt each run so new columns apply
    create_equation_tables(con)
    now = db.utc_now()
    for r in results:
        con.execute("INSERT INTO equation_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            r.property_id, r.facts.get("property_name") or r.facts.get("property_address"), r.facts.get("city"),
            r.facts.get("state"), r.facts.get("loan_amount"), r.score, r.tier, r.stage, flag_text(r), r.L.value, r.O.value,
            r.R.value, r.I.value, r.showing, r.corroboration, round(r.early, 2), round(r.late, 2),
            json.dumps(explain(r, cfg)), as_of, cfg["version"], now])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bloomfield Opportunity Score")
    ap.add_argument("--as-of", help="score as if it were this day (YYYY-MM-DD)")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--explain", help="property id or part of its name")
    args = ap.parse_args(argv)
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    cfg = load_equation_config()
    con = open_db()
    create_equation_tables(con)
    results = score_all(con, cfg, as_of)
    write_scores(con, results, cfg, as_of)
    if args.explain:
        needle = args.explain.lower()
        for r in results:
            name = (r.facts.get("property_name") or "") + " " + r.property_id
            if needle in name.lower():
                print(json.dumps(explain(r, cfg), indent=2))
                return
        print("No property matched.")
        return
    print(f"\n{'#':>3}  score  tier   stage       L    O    R    I   property")
    for i, r in enumerate(results[: args.top], 1):
        print(f"{i:>3}  {r.score:>5.1f}  {r.tier:<5}  {r.stage:<10} {r.L.value:.2f} {r.O.value:.2f} "
              f"{r.R.value:.2f} {r.I.value:.2f}  {r.facts.get('property_name') or r.property_id}"
              + (f"  [{flag_text(r)}]" if flag_text(r) else ""))


if __name__ == "__main__":
    main()
