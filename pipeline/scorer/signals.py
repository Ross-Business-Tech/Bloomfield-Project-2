"""
signals.py - the warning signs the scorer looks for, one small function per family.

In plain English: each function looks at one property's paperwork and returns a list of
Signals. A Signal is "what we saw, how many points it's worth, and which record proves it".
The points come from config/scoring.yaml. Nothing here talks to the internet or an AI.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime

from rapidfuzz import fuzz

from .group import Property


@dataclass
class Signal:
    name: str
    family: str
    points: float
    detail: str
    evidence_table: str | None = None
    evidence_id: str | None = None
    evidence_date: date | None = None
    quote: str | None = None
    ages: bool = False           # True = old evidence gets discounted (events, not states)
    unconfirmed: bool = False    # True = came from a Reader row marked needs_review


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def months_between(start: date, end: date) -> float:
    return (end - start).days / 30.44


def money(x) -> str:
    if x is None:
        return "an unknown amount"
    if x >= 1_000_000:
        return f"${x / 1_000_000:.1f}M"
    return f"${x:,.0f}"


def as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        parsed = json.loads(v)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def as_dict(v) -> dict:
    if isinstance(v, dict):
        return v
    try:
        parsed = json.loads(v) if v else {}
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def quote_for(filing: dict, *fields: str) -> str | None:
    quotes = as_dict(filing.get("quotes"))
    for f in fields:
        if quotes.get(f):
            return str(quotes[f])
    return None


def filing_kind(f: dict) -> str:
    """The Reader's filing_type, falling back to Part A's doc_category label."""
    return (f.get("filing_type") or f.get("doc_category") or "other").lower()


def filing_date(f: dict) -> date | None:
    return as_date(f.get("recording_date") or f.get("filing_date"))


def _evidence_from_filing(f: dict) -> dict:
    return {"evidence_table": "filing_fields", "evidence_id": f.get("file_id"),
            "evidence_date": filing_date(f), "unconfirmed": f.get("status") == "needs_review"}


# ---------------------------------------------------------------------------
# 1. Construction loans that never got paid off (Part A recorded_loans)
# ---------------------------------------------------------------------------
def construction_loan_signals(p: Property, cfg: dict, as_of: date) -> list[Signal]:
    s_cfg = cfg["signals"]
    loans = sorted(p.recorded_loans, key=lambda r: as_date(r.get("recording_date")) or date.min)
    constructions = [r for r in loans if (r.get("loan_kind") or "").lower() == "construction"]
    if not constructions:
        return []
    c = constructions[-1]                        # the most recent construction loan
    c_date = as_date(c.get("recording_date"))
    if not c_date:
        return []
    later = [r for r in loans if (as_date(r.get("recording_date")) or date.min) > c_date]
    paid_off = any(
        (r.get("loan_kind") or "").lower() == "permanent"
        or ((r.get("loan_kind") or "").lower() == "release"
            and (not r.get("related_instrument") or r.get("related_instrument") == c.get("instrument_number")))
        for r in later)

    out = []
    age = months_between(c_date, as_of)
    rule = s_cfg["construction_loan_no_takeout"]
    if not paid_off and age >= rule["min_age_months"]:
        out.append(Signal(
            "construction_loan_no_takeout", rule["family"], rule["points"],
            f"{money(c.get('amount'))} construction loan from {c.get('grantee') or 'an unknown lender'} "
            f"recorded {age / 12:.1f} years ago with no permanent loan or payoff since",
            "recorded_loans", c.get("record_id"), c_date))

    rule = s_cfg["construction_loan_modified"]
    mods = [r for r in later if (r.get("loan_kind") or "").lower() in ("modification", "extension")]
    for r in mods[: rule["max_count"]]:
        out.append(Signal(
            "construction_loan_modified", rule["family"], rule["points"],
            f"Loan {(r.get('loan_kind') or '').lower()} recorded {as_date(r.get('recording_date'))} "
            f"(instrument {r.get('instrument_number')})",
            "recorded_loans", r.get("record_id"), as_date(r.get("recording_date"))))
    return out


# ---------------------------------------------------------------------------
# 2. Permits and certificates of occupancy (Part B filing_fields)
# ---------------------------------------------------------------------------
def permit_signals(p: Property, cfg: dict, as_of: date) -> list[Signal]:
    s_cfg = cfg["signals"]
    permits = [f for f in p.filings if filing_kind(f) == "building_permit"]
    has_co = any(filing_kind(f) == "certificate_of_occupancy" for f in p.filings)
    if not permits or has_co:
        return []                                # finished buildings have a CO
    out = []

    rule = s_cfg["permit_extended"]
    extended = [f for f in permits
                if "permit_extended" in as_list(f.get("distress_tags"))
                or "extend" in (f.get("permit_status") or "").lower()]
    for f in extended[: rule["max_count"]]:
        out.append(Signal("permit_extended", rule["family"], rule["points"],
                          f"Building permit {f.get('permit_number') or ''} has been extended "
                          f"(status: {f.get('permit_status') or 'n/a'})".replace("  ", " "),
                          quote=quote_for(f, "permit_status", "permit_expiration_date"),
                          **_evidence_from_filing(f)))

    rule = s_cfg["permit_expired"]
    for f in permits:
        exp = as_date(f.get("permit_expiration_date"))
        if "permit_expired" in as_list(f.get("distress_tags")) or (exp and exp < as_of):
            out.append(Signal("permit_expired", rule["family"], rule["points"],
                              f"Building permit {f.get('permit_number') or ''} expired"
                              + (f" on {exp}" if exp else "") + " with no certificate of occupancy",
                              quote=quote_for(f, "permit_expiration_date"),
                              **_evidence_from_filing(f)))
            break

    rule = s_cfg["permit_without_co"]
    dated = [(filing_date(f), f) for f in permits if filing_date(f)]
    if dated:
        first_date, f = min(dated, key=lambda t: t[0])
        age = months_between(first_date, as_of)
        if age >= rule["min_age_months"]:
            out.append(Signal("permit_without_co", rule["family"], rule["points"],
                              f"Permit issued {age / 12:.1f} years ago and still no certificate of occupancy",
                              **_evidence_from_filing(f)))
    return out


# ---------------------------------------------------------------------------
# 3. Mechanic's liens (Part B filing_fields)
# ---------------------------------------------------------------------------
def lien_signals(p: Property, cfg: dict, as_of: date, loan_amount: float | None) -> list[Signal]:
    rule = cfg["signals"]["mechanics_lien"]
    out = []
    for f in p.filings:
        if filing_kind(f) != "mechanics_lien" and "contractor_unpaid" not in as_list(f.get("distress_tags")):
            continue
        d = filing_date(f)
        points = rule["points"]
        recent = d is not None and (as_of - d).days <= rule["recent_days"]
        if not recent:
            points /= 2
        amount = f.get("lien_amount")
        big = bool(amount and loan_amount and amount >= rule["big_lien_share"] * loan_amount)
        if big:
            points += rule["big_lien_bonus"]
        who = f.get("lien_claimant") or "a contractor"
        detail = f"Mechanic's lien for {money(amount)} by {who}"
        if f.get("lien_work"):
            detail += f" ({f['lien_work']})"
        detail += f", filed {d}" if d else ""
        if big:
            detail += f" - {amount / loan_amount:.1%} of the loan"
        out.append(Signal("mechanics_lien", rule["family"], points, detail,
                          quote=quote_for(f, "lien_amount", "lien_claimant"),
                          **_evidence_from_filing(f)))
    return out


# ---------------------------------------------------------------------------
# 4. The lender's health (Part A bank_stats)
# ---------------------------------------------------------------------------
_BANK_NOISE = re.compile(r"\b(the|n\.?a\.?|national association|inc|corp|corporation|co|company|fsb)\b")


def norm_bank(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]", " ", _BANK_NOISE.sub(" ", name.lower()))
    return " ".join(n.split())


class BankBook:
    """The latest quarter for every bank, plus a cautious name lookup."""

    def __init__(self, con):
        self.latest: dict[str, dict] = {}
        try:
            cur = con.execute("""
                SELECT * FROM bank_stats
                QUALIFY row_number() OVER (PARTITION BY rssd_id ORDER BY report_date DESC) = 1""")
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                r = dict(zip(cols, row))
                self.latest[r["rssd_id"]] = r
        except Exception:      # no bank_stats table yet
            pass
        self._names = {rssd: norm_bank(r.get("bank_name") or "") for rssd, r in self.latest.items()}
        # Part C's decisions (lender_links), when it has run: trusted over our own guess,
        # including "no match" (rssd_id NULL) and a person's manual answers.
        self.part_c: dict[str, str | None] = {}
        try:
            self.part_c = {n.strip(): r for n, r in con.execute(
                "SELECT lender_name, rssd_id FROM lender_links").fetchall()}
        except Exception:      # Part C hasn't run
            pass

    def find(self, lender: str | None, threshold: int) -> dict | None:
        if not lender:
            return None
        if lender.strip() in self.part_c:
            rssd = self.part_c[lender.strip()]
            return self.latest.get(rssd) if rssd else None
        target = norm_bank(lender)
        best, best_score = None, 0
        for rssd, name in self._names.items():
            score = fuzz.token_sort_ratio(target, name)
            if score > best_score:
                best, best_score = rssd, score
        return self.latest[best] if best and best_score >= threshold else None


def lender_names(p: Property) -> list[str]:
    names = [r.get("grantee") for r in p.recorded_loans
             if (r.get("loan_kind") or "").lower() in ("construction", "permanent", "modification", "extension")]
    names += [f.get("lender_name") for f in p.filings]
    seen, out = set(), []
    for n in names:
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def lender_signals(p: Property, cfg: dict, banks: BankBook) -> list[Signal]:
    s_cfg = cfg["signals"]
    out, done = [], set()
    for name in lender_names(p):
        bank = banks.find(name, s_cfg["lender_name_match_threshold"])
        if not bank or bank["rssd_id"] in done:
            continue
        done.add(bank["rssd_id"])
        ratio = bank.get("cre_nonaccrual_ratio")
        when = as_date(bank.get("report_date"))
        if bank.get("rising_two_quarters"):
            rule = s_cfg["lender_nonaccruals_rising"]
            out.append(Signal("lender_nonaccruals_rising", rule["family"], rule["points"],
                              f"Lender {bank['bank_name']}'s CRE nonaccruals have risen two quarters in a row"
                              + (f" (now {ratio:.1%})" if ratio is not None else ""),
                              "bank_stats", bank["rssd_id"], when))
        rule = s_cfg["lender_high_nonaccrual"]
        if ratio is not None and ratio >= rule["ratio"]:
            out.append(Signal("lender_high_nonaccrual", rule["family"], rule["points"],
                              f"Lender {bank['bank_name']} has {ratio:.1%} of CRE loans on nonaccrual",
                              "bank_stats", bank["rssd_id"], when))
    return out


# ---------------------------------------------------------------------------
# 5. CMBS loan data (Part A loans: EDGAR and Trepp)
# ---------------------------------------------------------------------------
_DELINQUENT = re.compile(r"(30|60|90|120)\s*\+?\s*(day|dpd)|delinq|default|foreclos|\breo\b|non-?perform|late|bankrupt|matured", re.I)


def cmbs_signals(p: Property, cfg: dict, as_of: date) -> list[Signal]:
    s = cfg["signals"]
    out = []
    # One loan reported by two sources (Part C's loan_group_id) counts once: newest report
    # first, and each warning sign is kept only the first time we see it for that loan.
    loans = sorted(p.cmbs_loans, key=lambda ln: str(ln.get("report_period") or ""), reverse=True)
    seen: set[tuple[str, str]] = set()
    for ln in loans:
        group = ln.get("loan_group_id") or ln.get("loan_key")
        for x in _one_cmbs_loan(ln, s, as_of):
            if (group, x.name) not in seen:
                seen.add((group, x.name))
                out.append(x)
    return out


def _one_cmbs_loan(ln: dict, s: dict, as_of: date) -> list[Signal]:
    out = []
    ev = {"evidence_table": "loans", "evidence_id": ln.get("loan_key"),
          "evidence_date": as_date(ln.get("report_period"))}
    label = ln.get("deal_name") or "CMBS deal"
    dscr, occ = ln.get("dscr"), ln.get("occupancy")
    if dscr is not None and dscr < 1:
        r = s["cmbs_dscr_below_1"]
        out.append(Signal("cmbs_dscr_below_1", r["family"], r["points"],
                          f"DSCR is {dscr:.2f} in {label}: the property doesn't cover its debt payments", **ev))
    if occ is not None:
        occ_frac = occ / 100 if occ > 1 else occ
        r = s["cmbs_low_occupancy"]
        if occ_frac < r["below"]:
            out.append(Signal("cmbs_low_occupancy", r["family"], r["points"],
                              f"Occupancy is {occ_frac:.0%} in {label}", **ev))
    if ln.get("watchlist_flag"):
        r = s["cmbs_watchlist"]
        out.append(Signal("cmbs_watchlist", r["family"], r["points"],
                          f"On the servicer's watchlist in {label}", **ev,
                          quote=ln.get("servicer_commentary")))
    status = ln.get("payment_status") or ""
    if status and _DELINQUENT.search(status):
        r = s["cmbs_delinquent"]
        out.append(Signal("cmbs_delinquent", r["family"], r["points"],
                          f"Payment status '{status}' in {label}", **ev))
    mat = as_date(ln.get("maturity_date"))
    if mat:
        balance = ln.get("current_balance")
        if mat < as_of and (balance is None or balance > 0):
            r = s["cmbs_past_maturity"]
            out.append(Signal("cmbs_past_maturity", r["family"], r["points"],
                              f"Loan matured {mat} and still shows {money(balance)} outstanding", **ev))
        elif mat >= as_of and months_between(as_of, mat) <= s["cmbs_maturity_near"]["within_months"]:
            r = s["cmbs_maturity_near"]
            out.append(Signal("cmbs_maturity_near", r["family"], r["points"],
                              f"Loan of {money(balance)} matures {mat}: refinancing risk", **ev))
    return out


# ---------------------------------------------------------------------------
# 6. Legal and distress events (Part B filing_fields)
# ---------------------------------------------------------------------------
TAG_FROM_FILING_TYPE = {
    "foreclosure_notice": "foreclosure_scheduled",
    "receivership": "receiver_appointed",
    "lis_pendens": "lis_pendens",
    "lender_lawsuit": "lender_lawsuit",
    "ucc_sale_notice": "ucc_sale_scheduled",
    "auction_listing": "listed_for_auction",
    "tax_delinquency": "tax_delinquent",
}

TAG_WORDS = {
    "foreclosure_scheduled": "Foreclosure notice", "receiver_appointed": "Receiver appointed",
    "lis_pendens": "Lis pendens filed", "lender_lawsuit": "Lender lawsuit",
    "ucc_sale_scheduled": "UCC sale of the ownership interests scheduled",
    "listed_for_auction": "Listed for auction", "maturity_default": "Maturity default",
    "payment_default": "Payment default", "special_servicing_transfer": "Sent to special servicing",
    "tax_delinquent": "Property taxes delinquent", "license_suspended": "Operating license suspended",
    "license_violation": "License violation", "cash_management_triggered": "Cash management triggered",
    "major_tenant_leaving": "Major tenant leaving", "occupancy_decline": "Occupancy falling",
    "construction_stalled": "Construction stalled",
}


def legal_signals(p: Property, cfg: dict) -> list[Signal]:
    rule = cfg["signals"]["legal_event"]
    table = rule["points_by_tag"]
    # One document = one event: a lis pendens that the Reader also tagged "lender_lawsuit"
    # counts once, at its strongest tag. Then one signal per tag, keeping the newest document.
    best: dict[str, tuple[dict, date | None]] = {}
    for f in p.filings:
        tags = set(as_list(f.get("distress_tags")))
        if (t := TAG_FROM_FILING_TYPE.get(filing_kind(f))):
            tags.add(t)
        tags &= set(table)
        if not tags:
            continue
        tag = max(sorted(tags), key=lambda t: table[t])
        d = filing_date(f)
        if tag not in best or (d and (best[tag][1] or date.min) < d):
            best[tag] = (f, d)
    out = []
    for tag, (f, d) in best.items():
        detail = TAG_WORDS.get(tag, tag.replace("_", " ").capitalize())
        when = as_date(f.get("auction_date") or f.get("sale_date"))
        if when:
            detail += f", sale/auction date {when}"
        if f.get("case_number"):
            detail += f" (case {f['case_number']})"
        if d:
            detail += f", filed {d}"
        out.append(Signal(tag, rule["family"], table[tag], detail, ages=True,
                          quote=quote_for(f, "filing_type", "auction_date", "sale_date", "case_number"),
                          **_evidence_from_filing(f)))
    return out


# ---------------------------------------------------------------------------
# 7. The Scout's opening-date tracking (Part B projects)
# ---------------------------------------------------------------------------
def opening_signals(p: Property, cfg: dict) -> list[Signal]:
    s = cfg["signals"]
    out = []
    for pr in p.projects:
        ev = {"evidence_table": "projects", "evidence_id": pr.get("project_id"),
              "evidence_date": as_date(pr.get("last_checked_at"))}
        name = pr.get("project_name") or "The project"
        if pr.get("stale_candidate"):
            r = s["stale_opening"]
            months = pr.get("months_overdue") or 0
            extra = min(r["max_extra"], r["per_month_overdue"] * months)
            out.append(Signal("stale_opening", r["family"], r["points"] + extra,
                              f"{name} said it would open '{pr.get('stated_opening_phrase')}' but the site "
                              f"still says coming soon ({months:.0f} months overdue)",
                              quote=pr.get("stated_opening_phrase"), **ev))
        if pr.get("missed_lease_up"):
            r = s["missed_lease_up"]
            out.append(Signal("missed_lease_up", r["family"], r["points"],
                              f"{name} missed its student move-in season", **ev))
        changes = pr.get("date_change_count") or 0
        if changes:
            r = s["opening_date_moved"]
            n = min(changes, r["max_count"])
            out.append(Signal("opening_date_moved", r["family"], r["points"] * n,
                              f"Opening date changed {changes} time(s) (first said "
                              f"'{pr.get('first_stated_phrase')}')", **ev))
    return out
