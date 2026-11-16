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
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from collectors.common import config, db

from . import creative as cr
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
    concession_months DOUBLE,  -- free rent offered per lease, in months (0 if none)
    lease_term_months DOUBLE,  -- length of that lease (NULL = config default, 12)
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
    stage          TEXT,      -- early / ready_to_act
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
                  "creative": "Standout creative signals",
                  "rent_decline": "Standout rent decline", "inspection_gap": "Standout site inactivity"}

COURT_KINDS = {"foreclosure_notice", "receivership", "lis_pendens", "lender_lawsuit", "ucc_sale_notice",
               "tax_delinquency", "default_notice"}
COURT_TAGS = {"foreclosure_scheduled", "receiver_appointed", "lis_pendens", "lender_lawsuit",
              "ucc_sale_scheduled", "tax_delinquent", "payment_default", "maturity_default",
              "technical_default", "covenant_default", "forbearance"}


def load_equation_config(path: str | Path | None = None) -> dict:
    with open(path or EQUATION_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def create_equation_tables(con) -> None:
    con.execute(INPUT_SCHEMA)
    con.execute(cr.INPUT_SCHEMA)
    con.execute("ALTER TABLE rent_observations ADD COLUMN IF NOT EXISTS concession_months DOUBLE")
    con.execute("ALTER TABLE rent_observations ADD COLUMN IF NOT EXISTS lease_term_months DOUBLE")
    con.execute("ALTER TABLE operating_metrics ADD COLUMN IF NOT EXISTS disclosed_on DATE")
    con.execute("ALTER TABLE market_occupancy ADD COLUMN IF NOT EXISTS rent_per_unit_year DOUBLE")


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
    C: Part = field(default_factory=Part)          # all creative signals combined
    creative: list = field(default_factory=list)   # the individual creative signals (creative.Sig)
    showing: int = 0
    corroboration: float = 0.0
    early: float = 0.0
    late: float = 0.0
    late_why: list = field(default_factory=list)
    standouts: list = field(default_factory=list)
    boosts: dict = field(default_factory=dict)
    applicable: dict = field(default_factory=dict)
    rescale: float = 1.0
    counters: list = field(default_factory=list)
    score: float = 0.0
    tier: str = "low"
    stage: str = "early"


# ---------------------------------------------------------------------------
# L: construction loan on the clock (Part A recorded_loans)
# ---------------------------------------------------------------------------
def has_opened(p: Property) -> bool:
    """Certificate of occupancy issued, or a tracked project whose site no longer says coming soon."""
    if any(filing_kind(f) == "certificate_of_occupancy" for f in p.filings):
        return True
    return any(pr.get("stale_candidate") is False and pr.get("page_still_unopened") is False for pr in p.projects)


CHANGE_KINDS = ("extension", "modification")
NOT_A_LOAN = ("extension", "modification", "release", "assignment", "permanent_takeout")


def loan_records(p: Property) -> list[dict]:
    """Every recorded loan we know about for a property, in one shape.

    Part A's recorded_loans table (index rows from county recorder searches) AND the loans the AI Reader
    read out of recorder documents (filing_fields rows with filing_type 'recorded_loan'). Until Part A's
    recorded_loans collector exists, the Reader's rows are the ONLY real loan data, so both must count.
    """
    out = list(p.recorded_loans)
    seen = {(str(r.get("instrument_number") or ""), str(as_date(r.get("recording_date")))) for r in out}
    for f in p.filings:
        if filing_kind(f) != "recorded_loan":
            continue
        d = as_date(f.get("recording_date")) or filing_date(f)
        key = (str(f.get("case_number") or ""), str(d))
        if key in seen and key[0]:
            continue                                 # same instrument already in recorded_loans
        out.append({"record_id": f.get("file_id"), "file_id": f.get("file_id"), "loan_kind": f.get("loan_kind"),
                    "recording_date": d, "amount": f.get("loan_amount"), "grantee": f.get("lender_name"),
                    "grantor": f.get("borrower_name"), "instrument_number": f.get("case_number"),
                    "property_address": f.get("property_address"), "state": f.get("property_state")})
    # Public CMBS loan filings (SEC Schedule AL): a loan the servicer reports as MODIFIED counts like a
    # recorded modification on that loan, dated at the report that first shows it (or the report itself).
    for ln in p.cmbs_loans:
        if not ln.get("modified_indicator") or not as_date(ln.get("origination_date")):
            continue
        base = {"record_id": ln.get("loan_key"), "loan_kind": "unknown", "recording_date": as_date(ln["origination_date"]),
                "amount": ln.get("original_balance"), "grantee": ln.get("servicer_name"), "source": "sec_edgar"}
        out.append(base)
        out.append({**base, "record_id": f"{ln.get('loan_key')}:modified", "loan_kind": "modification",
                    "recording_date": as_date(ln.get("report_period")) or as_date(ln["origination_date"])})
    return out


def past_maturity_loan(p: Property, as_of: date):
    """A public CMBS loan that is past its maturity date and still outstanding (balance > 0)."""
    for ln in p.cmbs_loans:
        m = as_date(ln.get("maturity_date"))
        reported = as_date(ln.get("report_period"))
        if m and m < as_of and (ln.get("current_balance") or 0) > 0 and (not reported or reported >= m):
            return ln, m
    return None


def clock_loan(p: Property, as_of: date | None = None):
    """The loan whose clock is running: the latest construction loan, or else a short-term ("transitional")
    loan - any other mortgage that has needed an extension or modification since it was recorded."""
    loans = sorted([r for r in loan_records(p) if as_date(r.get("recording_date"))
                    and (as_of is None or as_date(r["recording_date"]) <= as_of)],   # nothing recorded after as_of
                   key=lambda r: as_date(r["recording_date"]))
    kind = lambda r: (r.get("loan_kind") or "").lower()
    built = [r for r in loans if kind(r) == "construction"]
    if built:
        return built[-1], "construction loan"
    for r in reversed(loans):
        if kind(r) in NOT_A_LOAN:
            continue
        d = as_date(r["recording_date"])
        if any(kind(x) in CHANGE_KINDS and as_date(x["recording_date"]) > d for x in loans):
            return r, "short-term loan (extended)"
    return None


def loan_clock(p: Property, c: dict, as_of: date) -> Part:
    pm = past_maturity_loan(p, as_of)
    if pm:
        # Public loan filing still shows a balance after the maturity date: the loan was never refinanced
        # or paid off. That is the loan clock run out, whatever kind of loan it is.
        ln, m = pm
        return Part(1.0, True, f"public loan filing: past its maturity date ({m}) and still outstanding "
                               f"(${(ln.get('current_balance') or 0) / 1e6:.1f}M)")
    loan = clock_loan(p, as_of)
    if not loan:
        return Part()
    con_loan, label = loan
    loans = sorted([r for r in loan_records(p) if (as_date(r.get("recording_date")) or date.min) <= as_of],
                   key=lambda r: as_date(r.get("recording_date")) or date.min)
    start = as_date(con_loan.get("recording_date"))
    later = [r for r in loans if (as_date(r.get("recording_date")) or date.min) > start]
    if any((r.get("loan_kind") or "").lower() in ("permanent", "release") for r in later):
        return Part(0.0, True, f"{label} was paid off by a permanent loan or release")
    months = months_between(start, as_of)
    ext = sum(1 for r in later if (r.get("loan_kind") or "").lower() in ("extension", "modification"))
    base = ramp(months, c["start_months"], c["full_months"])
    bonus = min(c["max_extension_bonus"], c["per_extension"] * ext) if base > 0 else 0.0
    opened = label == "construction loan" and has_opened(p) and months >= c.get("opened_min_months", 24)
    if opened:
        bonus += c.get("opened_still_unpaid_bonus", 0.0)
    value = min(1.0, base + bonus)
    why = f"{label} recorded {months:.0f} months ago, still not paid off"
    if ext:
        why += f"; {ext} extension(s)/modification(s)"
    if opened:
        why += "; the building has OPENED but the construction loan is still outstanding"
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
    if best.known:
        return best
    return permit_opening_delay(p, c, as_of)


NEW_CONSTRUCTION = re.compile(r"\bnew\b|new (building|construction|commercial|multi)", re.I)


def permit_opening_delay(p: Property, c: dict, as_of: date) -> Part:
    """Public fallback when no project website date is tracked: a NEW-CONSTRUCTION building permit with no
    certificate of occupancy. Months overdue = months since the permit was issued - expected build time."""
    if any(filing_kind(f) == "certificate_of_occupancy" and (filing_date(f) or date.min) <= as_of for f in p.filings):
        return Part()
    best = Part()
    for f in p.filings:
        if filing_kind(f) != "building_permit" or not NEW_CONSTRUCTION.search(str(f.get("permit_status") or "")):
            continue
        issued = filing_date(f)
        if not issued or issued > as_of:
            continue
        overdue = max(0.0, months_between(issued, as_of) - c.get("expected_build_months", 24))
        value = ramp(overdue, c["start_months"], c["full_months"])
        why = (f"public permit {f.get('permit_number') or ''}: new construction issued {issued}, no certificate of "
               f"occupancy; {overdue:.0f} months past a typical {c.get('expected_build_months', 24)}-month build")
        if value >= best.value:
            best = Part(value, True, why.replace("permit  ", "permit "))
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
def inspection_gap(p: Property, c: dict, inspections: dict, as_of: date, cfg_counter: dict | None = None,
                   state: str | None = None, coverage: set | None = None) -> Part:
    cfg_counter = cfg_counter or {"inspected_within_days": -1}
    if any(filing_kind(f) == "certificate_of_occupancy" for f in p.filings):
        return Part(0.0, True, "certificate of occupancy issued: building is finished")
    permits = [f for f in p.filings if filing_kind(f) == "building_permit"]
    if not permits:
        return Part()
    # Absent inspection records are only evidence when the market actually publishes readable
    # inspections (coverage). Otherwise permit age alone cannot establish inactivity: stay unknown.
    own = {str(f.get("permit_number") or "") for f in permits}
    has_real_dates = any(d for num in own for d in inspections.get(num, []) if d and d <= as_of)
    covered = coverage is not None and (state or "").upper() in {str(s).upper() for s in coverage}
    if not has_real_dates and not covered:
        return Part(0.0, False, "no readable inspection records for this market; inactivity unknown")
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

    def known_by(f):          # a filing dated after as_of didn't exist yet (matters for backtests)
        d = filing_date(f)
        return d is None or d <= as_of

    why, court = [], 0.0
    for f in p.filings:
        if not known_by(f):
            continue
        tags = set(f.get("distress_tags") or []) if isinstance(f.get("distress_tags"), list) else set(
            json.loads(f["distress_tags"]) if isinstance(f.get("distress_tags"), str) and f["distress_tags"].startswith("[") else [])
        if filing_kind(f) in COURT_KINDS or tags & COURT_TAGS:
            pts = c["court_points"] * weight(f)
            if pts > court:
                court = pts
                kind = sorted(tags & COURT_TAGS)[0] if tags & COURT_TAGS else filing_kind(f)
                why = [f"court/legal filing: {kind.replace('_', ' ')}" + ("" if weight(f) == 1 else " (old: counted at half)")]
    liens = 0.0
    for f in p.filings:
        if filing_kind(f) != "mechanics_lien" or not known_by(f):
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
def creative_signals(p: Property, facts: dict, cfg: dict, as_of: date, rents, market, inspections, extra) -> list:
    from .weekly import property_type          # (weekly imports this module; import here to avoid a loop)
    c = cfg["creative"]
    R = rent_decline(p, facts, cfg["rent_decline"], rents, market, as_of, cfg["counter_signal"])
    I = inspection_gap(p, cfg["inspection_gap"], inspections, as_of, cfg["counter_signal"],
                       state=facts.get("state"), coverage=extra.get("inspection_coverage"))
    ptype = property_type(p)
    return [
        cr.Sig("R", R.value, R.known, R.why, cfg["rent_decline"]["reliability"], R.counter),
        cr.Sig("I", I.value, I.known, I.why, cfg["inspection_gap"]["reliability"], I.counter),
        cr.occupancy_slide(p, ptype, facts.get("state"), c["occupancy_slide"], extra.get("ops", {}),
                           extra.get("mkt_occ", {}), as_of),
        cr.income_erosion(p, c["income_erosion"], extra.get("ops", {}), as_of),
        cr.cost_squeeze(p, c["cost_squeeze"], extra.get("ops", {}), as_of),
        cr.concession_creep(p, c["concession_creep"], extra.get("concessions", {}), as_of),
        cr.licensing_quality(p, c["licensing_quality"], extra.get("licenses", {}), as_of),
        cr.value_cut(p, c["value_cut"], as_of),
    ]


def applicable_terms(p: Property, cfg: dict, as_of: date) -> dict:
    """Which early terms CAN apply to this property (any industry).

    Not applicable is different from "no data yet": a construction-loan clock cannot apply to a building that
    was finished long ago with no construction financing, and an opening date cannot apply to a building that
    has been open for years. Unknown-but-possible terms stay applicable (and simply score 0 until data arrives).
    """
    a = cfg["applicability"]
    kinds = {(r.get("loan_kind") or "").lower() for r in loan_records(p)}
    pre_opening = any(pr.get("stale_candidate") or pr.get("page_still_unopened") or (pr.get("months_overdue") or 0) > 0
                      for pr in p.projects)
    permits = [f for f in p.filings if filing_kind(f) in ("building_permit", "certificate_of_occupancy")]
    recent_build = any((d := filing_date(f)) and months_between(d, as_of) <= a["recent_construction_months"]
                       for f in permits)
    deadlines = [as_date(pr.get("opening_deadline")) for pr in p.projects if pr.get("opening_deadline")]
    recent_opening = any(d and months_between(d, as_of) <= a["recent_opening_months"] for d in deadlines)
    return {
        "loan_clock": clock_loan(p, as_of) is not None or past_maturity_loan(p, as_of) is not None
                      or pre_opening or recent_build,
        # a construction loan means something is being built: its opening date is unknown, not inapplicable
        "opening_delay": pre_opening or recent_opening or "construction" in kinds or recent_build,
        "creative": True,
    }


def strength_boost(s: float, so: dict) -> float:
    """x1.0 below boost_starts_at, rising in a straight line to max_boost at boost_full_at."""
    start, full, top = so.get("boost_starts_at", 0.6), so.get("boost_full_at", 1.0), so.get("max_boost", 1.5)
    if s <= start:
        return 1.0
    return 1.0 + (top - 1.0) * min(1.0, (s - start) / (full - start))


def score_property(p: Property, cfg: dict, as_of: date, rents: dict, market: dict, inspections: dict,
                   extra: dict | None = None) -> Result:
    facts = property_facts(p)
    r = Result(p.property_id, facts)
    r.L = loan_clock(p, cfg["loan_clock"], as_of)
    r.O = opening_delay(p, cfg["opening_delay"], as_of)
    r.creative = creative_signals(p, facts, cfg, as_of, rents, market, inspections, extra or {})
    by = {s.code: s for s in r.creative}
    r.R = Part(by["R"].value, by["R"].known, by["R"].why, by["R"].counter)
    r.I = Part(by["I"].value, by["I"].known, by["I"].why, by["I"].counter)
    cval, cshow, cwhy = cr.combine(r.creative, cfg["creative"])
    r.C = Part(cval, any(s.known for s in r.creative), cwhy)
    counters = [s.counter for s in r.creative if s.counter]
    w = cfg["weights"]
    parts = {"loan_clock": r.L, "opening_delay": r.O, "creative": r.C}
    r.showing = (sum(1 for part in (r.L, r.O) if part.value >= cfg["shows_at"])
                 + min(cfg["creative"]["max_confirmations"], cshow))
    r.corroboration = float(cfg["corroboration"][min(r.showing, 4)])
    so = cfg["standout"]
    early = 0.0
    # Rescale for terms that cannot apply (e.g. no construction financing on a long-open building), at most x max_rescale.
    r.applicable = applicable_terms(p, cfg, as_of)
    w_all = sum(w[k] for k in parts)
    w_app = sum(w[k] for k in parts if r.applicable[k])
    r.rescale = min(cfg["applicability"]["max_rescale"], w_all / w_app) if w_app else 1.0
    strongest_creative = max([s.value for s in r.creative if s.known] or [0.0])
    r.boosts = {}
    for name, part in parts.items():
        # Strength boost: a strong signal counts MORE, even on its own. For C the strongest single creative
        # signal decides (C's value itself already includes each source's reliability).
        s_for_boost = strongest_creative if name == "creative" else part.value
        boost = strength_boost(s_for_boost, so)
        r.boosts[name] = boost
        if boost > 1.0:
            # The boost and the applicability rescale do NOT stack: the larger one is used.
            early += w[name] * part.value * max(boost, r.rescale)
        else:
            early += w[name] * part.value * r.corroboration * r.rescale
        if s_for_boost >= so["at_strength"] and part.value > 0:
            r.standouts.append(f"{STANDOUT_LABEL[name]}: {part.why}")
        if part.counter:
            r.counters.append(part.counter)
    r.counters += counters
    # Downside extreme: each counter-signal cuts the early points.
    r.early = early * cfg["counter_signal"]["factor"] ** len(r.counters)
    r.late, r.late_why = late_points(p, facts, cfg["late"], as_of)
    r.score = round(min(cfg["max_score"], r.early + r.late), 1)
    t = cfg["tiers"]
    r.tier = "hot" if r.score >= t["hot"] else "watch" if r.score >= t["watch"] else "low"
    r.stage = "ready_to_act" if r.late > 0 else "early"   # court/lien filings confirm it's actionable
    return r


def load_inputs(con):
    """Returns (rents, market, inspections, extra) where extra holds the other creative-signal inputs."""
    rents, market, inspections = {}, {}, {}
    extra = {"ops": {}, "mkt_occ": {}, "concessions": {}, "licenses": {}, "inspection_coverage": set()}
    if table_exists(con, "rent_observations"):
        cols = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                          "WHERE table_name = 'rent_observations'").fetchall()}
        conc = "concession_months" if "concession_months" in cols else "NULL"
        term = "lease_term_months" if "lease_term_months" in cols else "NULL"
        for pid, d, rent, cm, tm in con.execute(
                f"SELECT project_id, observed_at, asking_rent, {conc}, {term} FROM rent_observations").fetchall():
            if rent is not None:
                rents.setdefault(str(pid), []).append((as_date(d), rent))
            if cm is not None:
                extra["concessions"].setdefault(str(pid), []).append((as_date(d), cm, tm))
    if table_exists(con, "operating_metrics"):
        cur = con.execute("SELECT * FROM operating_metrics")
        names = [x[0] for x in cur.description]
        for row in cur.fetchall():
            d = dict(zip(names, row))
            extra["ops"].setdefault(str(d["link_id"]), []).append(d)
    if table_exists(con, "market_occupancy"):
        from .weekly import clean_type
        mcols = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                            "WHERE table_name = 'market_occupancy'").fetchall()}
        rent = "rent_per_unit_year" if "rent_per_unit_year" in mcols else "NULL"
        for st, pt, d, occ, mr in con.execute(
                f"SELECT state, property_type, period, occupancy, {rent} FROM market_occupancy").fetchall():
            base = clean_type(pt)
            low = (pt or "").lower()                    # keep the benchmark's real subtype in the key:
            if "nursing" in low or "skilled" in low:    # nursing-facility averages never serve assisted/
                base = f"{base} (nursing)"              # independent-living properties, and vice versa
            elif "assisted" in low or "independent living" in low:
                base = f"{base} (assisted)"
            extra["mkt_occ"].setdefault(((st or "").upper(), base), []).append((as_date(d), occ, mr))
    if table_exists(con, "licensing_records"):
        cur = con.execute("SELECT * FROM licensing_records")
        names = [x[0] for x in cur.description]
        for row in cur.fetchall():
            d = dict(zip(names, row))
            k = cr.license_key(d.get("address"), d.get("state"))
            if k:
                extra["licenses"].setdefault(k, []).append(d)
    if table_exists(con, "market_rents"):
        for city, st, d, idx in con.execute("SELECT city, state, period, rent_index FROM market_rents").fetchall():
            market.setdefault(((city or "").strip().lower(), (st or "").strip().upper()), []).append((as_date(d), idx))
    if table_exists(con, "permit_inspections"):
        for num, d in con.execute("SELECT permit_number, inspection_date FROM permit_inspections").fetchall():
            inspections.setdefault(str(num), []).append(as_date(d))
        # Coverage evidence: which states actually have readable inspection records (filing side of
        # the same permits). A state with none means "we can't see inspections there", NOT "no
        # inspections happened" - I must stay unknown for those properties.
        coverage_rows = [] if not table_exists(con, "filing_fields") else con.execute(
                "SELECT DISTINCT property_state FROM filing_fields WHERE filing_type = 'building_permit' "
                "AND permit_number IN (SELECT permit_number FROM permit_inspections)").fetchall()
        for (st,) in coverage_rows:
            if st:
                extra["inspection_coverage"].add(str(st).upper())
    return rents, market, inspections, extra


def score_all(con, cfg: dict | None = None, as_of: date | None = None) -> list[Result]:
    cfg = cfg or load_equation_config()
    as_of = as_of or date.today()
    rents, market, inspections, extra = load_inputs(con)
    out = [score_property(p, cfg, as_of, rents, market, inspections, extra) for p in group_properties(con)]
    out.sort(key=lambda r: (-r.score, -(r.facts.get("loan_amount") or 0), r.property_id))
    return out


def explain(r: Result, cfg: dict) -> dict:
    w = cfg["weights"]
    return {
        "L_loan_clock": {"strength": round(r.L.value, 3), "points": round(w["loan_clock"] * r.L.value, 1), "why": r.L.why},
        "O_opening": {"strength": round(r.O.value, 3), "points": round(w["opening_delay"] * r.O.value, 1), "why": r.O.why},
        "C_creative": {"strength": round(r.C.value, 3), "points": round(w["creative"] * r.C.value, 1), "why": r.C.why},
        "creative_signals": [{"code": s.code, "name": cr.CREATIVE_LABELS[s.code], "strength": round(s.value, 3),
                              "reliability": s.q, "effective": round(s.effective, 3), "known": s.known, "why": s.why}
                             for s in r.creative],
        "anti_fluke": f"{r.showing} early signal(s) showing -> keeps {r.corroboration:.0%} of early points",
        "strength_boosts": {k: round(v, 2) for k, v in r.boosts.items()},
        "standouts": r.standouts,
        "counter_signals": r.counters,
        "not_applicable": [k for k, ok in r.applicable.items() if not ok],
        "rescale": round(r.rescale, 2),
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
    print(f"\n{'#':>3}  score  tier   stage          L    O    C   property")
    for i, r in enumerate(results[: args.top], 1):
        print(f"{i:>3}  {r.score:>5.1f}  {r.tier:<5}  {r.stage:<13} {r.L.value:.2f} {r.O.value:.2f} "
              f"{r.C.value:.2f}  {r.facts.get('property_name') or r.property_id}"
              + (f"  [{flag_text(r)}]" if flag_text(r) else ""))


if __name__ == "__main__":
    main()
