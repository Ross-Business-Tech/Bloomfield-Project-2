"""
score.py - turn each property's warning signs into one 0-100 score and a ranked list.

In plain English:
  1. group.py puts each property's paperwork together.
  2. signals.py finds the warning signs and gives each one points (from scoring.yaml).
  3. Here we discount old or unconfirmed evidence, cap each family of signals so one story
     can't be counted five times, add it up (max 100), pick a tier, and write the
     property_scores and score_signals tables.

Each run replaces the previous scores, so the tables always show the latest picture.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from collectors.common import config, db

from . import signals as sig
from .group import Property, group_properties

SCORING_YAML = config.CONFIG_DIR / "scoring.yaml"


def load_scoring_config(path: str | Path | None = None) -> dict:
    with open(path or SCORING_YAML, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class ScoredProperty:
    prop: Property
    facts: dict
    signals: list[sig.Signal] = field(default_factory=list)
    counted: dict[int, float] = field(default_factory=dict)    # id(signal) -> counted points
    score: float = 0.0
    tier: str = "low"
    summary: str = ""


# ---------------------------------------------------------------------------
# Facts for the ranked list (name, address, borrower, lender, loan size)
# ---------------------------------------------------------------------------
def _first(*values):
    for v in values:
        if v not in (None, ""):
            return v
    return None


def property_facts(p: Property) -> dict:
    rl = sorted(p.recorded_loans, key=lambda r: str(r.get("recording_date") or ""))
    main_loans = [r for r in rl if (r.get("loan_kind") or "") in ("construction", "permanent")] or rl
    f, cm, pr = p.filings, p.cmbs_loans, p.projects
    amounts = ([r.get("amount") for r in rl] + [c.get("original_balance") for c in cm]
               + [c.get("current_balance") for c in cm] + [x.get("loan_amount") for x in f])
    amounts = [a for a in amounts if a]
    lenders = sig.lender_names(p)
    return {
        "property_name": _first(*[x.get("project_name") for x in pr], *[c.get("property_name") for c in cm],
                                *[x.get("project_name") for x in f]),
        "property_address": _first(*[r.get("property_address") for r in rl],
                                   *[x.get("property_address") for x in f],
                                   *[x.get("property_address") for x in pr],
                                   *[c.get("property_address") for c in cm]),
        "city": _first(*[x.get("property_city") or x.get("raw_city") for x in f],
                       *[x.get("city") for x in pr], *[c.get("property_city") for c in cm]),
        "state": _first(*[r.get("state") for r in rl], *[x.get("property_state") or x.get("raw_state") for x in f],
                        *[x.get("state") for x in pr], *[c.get("property_state") for c in cm]),
        "county": _first(*[r.get("county") for r in rl], *[x.get("raw_county") for x in f],
                         *[x.get("county") for x in pr]),
        "parcel_number": _first(*[r.get("parcel_number") for r in rl], *[x.get("parcel_number") for x in f]),
        "borrower_name": _first(*[r.get("grantor") for r in reversed(main_loans)],
                                *[x.get("borrower_name") for x in f], *[x.get("developer_name") for x in pr]),
        "lender_name": lenders[0] if lenders else None,
        "loan_amount": max(amounts) if amounts else None,
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def find_signals(p: Property, facts: dict, cfg: dict, as_of: date, banks: sig.BankBook) -> list[sig.Signal]:
    return (sig.construction_loan_signals(p, cfg, as_of)
            + sig.permit_signals(p, cfg, as_of)
            + sig.lien_signals(p, cfg, as_of, facts["loan_amount"])
            + sig.lender_signals(p, cfg, banks)
            + sig.cmbs_signals(p, cfg, as_of)
            + sig.legal_signals(p, cfg)
            + sig.opening_signals(p, cfg))


def adjust(s: sig.Signal, cfg: dict, as_of: date) -> None:
    """Discount old events and evidence a person hasn't confirmed yet (changes s.points)."""
    if s.ages and s.evidence_date and (as_of - s.evidence_date).days > cfg["stale_after_days"]:
        s.points /= 2
        s.detail += " (old - counted at half)"
    if s.unconfirmed:
        s.points *= cfg["needs_review_factor"]
        s.detail += " (not yet checked by a person)"


def score_property(p: Property, cfg: dict, as_of: date, banks: sig.BankBook) -> ScoredProperty:
    facts = property_facts(p)
    sp = ScoredProperty(p, facts, find_signals(p, facts, cfg, as_of, banks))
    for s in sp.signals:
        adjust(s, cfg, as_of)

    # Strongest first, then fill each family up to its cap.
    sp.signals.sort(key=lambda s: -s.points)
    used: dict[str, float] = defaultdict(float)
    caps = cfg["family_caps"]
    for s in sp.signals:
        room = max(0.0, caps.get(s.family, float("inf")) - used[s.family])
        sp.counted[id(s)] = min(s.points, room)
        used[s.family] += sp.counted[id(s)]

    sp.score = round(min(cfg["max_score"], sum(sp.counted.values())), 1)
    tiers = cfg["tiers"]
    sp.tier = "hot" if sp.score >= tiers["hot"] else "watch" if sp.score >= tiers["watch"] else "low"
    top = [s.detail for s in sp.signals if sp.counted[id(s)] > 0][:3]
    sp.summary = "; ".join(top) if top else "No warning signs found"
    return sp


def score_all(con, cfg: dict | None = None, as_of: date | None = None) -> list[ScoredProperty]:
    """Score every property. Returns them ranked, highest score first. Doesn't write anything."""
    cfg = cfg or load_scoring_config()
    as_of = as_of or date.today()
    banks = sig.BankBook(con)
    scored = [score_property(p, cfg, as_of, banks) for p in group_properties(con)]
    scored.sort(key=lambda sp: (-sp.score, -(sp.facts["loan_amount"] or 0), sp.prop.property_id))
    return scored


def write_scores(con, scored: list[ScoredProperty], cfg: dict, as_of: date) -> None:
    """Replace the contents of property_scores and score_signals with this run."""
    now = db.utc_now()
    version = cfg["version"]
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM property_scores")
        con.execute("DELETE FROM score_signals")
        for sp in scored:
            row = {"property_id": sp.prop.property_id, **sp.facts, "score": sp.score, "tier": sp.tier,
                   "signal_count": sum(1 for s in sp.signals if sp.counted[id(s)] > 0),
                   "summary": sp.summary, "grouped_by": sp.prop.grouped_by, "as_of": as_of,
                   "scoring_version": version, "scored_at": now}
            cols = list(row)
            con.execute(f"INSERT INTO property_scores ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [row[c] for c in cols])
            for s in sp.signals:
                con.execute("""INSERT INTO score_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", [
                    sp.prop.property_id, s.name, s.family, round(s.points, 2), round(sp.counted[id(s)], 2),
                    s.detail, s.evidence_table, s.evidence_id, s.evidence_date, s.quote, version, now])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def tier_counts(scored: list[ScoredProperty]) -> Counter:
    return Counter(sp.tier for sp in scored)
