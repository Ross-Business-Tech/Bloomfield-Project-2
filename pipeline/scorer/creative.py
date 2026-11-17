"""
creative.py - the CREATIVE signals: measurable early warnings that are not the loan clock or the opening date.

Nov 2026 rework: every creative signal is GENERAL. It can apply to any loan (CMBS, Fannie Mae / Freddie Mac,
bank, bridge, construction) and any property type, and every input is PUBLIC. Each signal has a short
ladder of public sources: the best one the property has is used, and when none has data the signal says
"don't have" (unknown) and scores 0. Source quality is in the reliability q.

    C = 1 - PRODUCT over j of (1 - q_j * s_j)          ("noisy-or": several weak signals add up, never past 1)

and C counts in the equation with weight 30:  Early = 50 L + 20 O + 30 C  (then boost / rescale).

    Signal                     Formula for s                                         Public sources, best first
    R  rent vs. city           (city rent change - property EFFECTIVE rent change)   listing / leasing-page rents
                               / 15 pts; effective rent = asking x (1 - free months  (any property) vs. HUD / Census /
                               / lease term), so free-rent concessions count here    market rents for its type
    Q  occupancy slide         (peak occ. in last 36 mo - occ. now - market drop)    1. CMBS SEC filings, Fannie Mae /
                               / 15 pts                                              Freddie Mac loan disclosures
                                                                                     2. share of the building listed
                                                                                        for lease (listing sites)
    V  income erosion          (best-year NOI - NOI now) / best-year NOI / 50%       CMBS / Fannie / Freddie
                               (NOI = revenue - expenses when not given, so rising   loan-level disclosures
                               costs count here)
    W  value cut               1. (value at securitization - latest) / 50%           1. CMBS appraisal (SEC)
                               2. (county's assessed-value change - property's) /    2. county assessor roll
                                  25 pts (a won tax appeal shows up as a cut)           (every property has one)
    T  unpaid property tax     any missed installment 0.5, rising to 1.0 at 12        county treasurer / tax
                               months past due; paid current = 0                     collector (every property)
    U  compliance trouble      open code / fire / health violations 0.25 each,       city code enforcement, fire /
                               unsafe / condemned / vacate order 1.0; CMS 1-star 0.8, health inspections, state
                               2-star 0.5, state license distress 0.8, abuse +0.2    licensing, CMS star ratings

Site inactivity (no city inspection at a construction site) is part of the opening delay O now
(scorer/equation.py), because it only applies to buildings that are not finished.
All thresholds live in config/equation.yaml under `creative:`.
"""
from __future__ import annotations

import re
from datetime import date

from .signals import as_date

CREATIVE_LABELS = {
    "R": "Rent vs. city", "Q": "Occupancy slide", "V": "Income erosion", "W": "Value cut",
    "T": "Unpaid property tax", "U": "Compliance trouble",
}

INPUT_SCHEMA = """
-- Operating results from public disclosures (CMBS SEC Schedule AL, Fannie Mae DUS / Freddie Mac K-deal
-- loan-level data, EMMA continuing disclosures, state cost reports).
-- link_id = any id the property's records carry (project_id, loan_key, file_id, record_id).
CREATE TABLE IF NOT EXISTS operating_metrics (
    link_id        TEXT,
    period_end     DATE,
    period_months  INTEGER,      -- 12 = a year / trailing twelve months; 3 = a quarter (annualized x4)
    occupancy      DOUBLE,       -- 0-1
    revenue        DOUBLE,
    expenses       DOUBLE,
    noi            DOUBLE,
    debt_service   DOUBLE,       -- kept for reference only: NOT scored (DSCR is not scored)
    source         TEXT,
    file_id        TEXT,
    disclosed_on   DATE          -- when it became public (e.g. EMMA posting date); NULL = period_end
);
-- Market occupancy for the same kind of property (e.g. NIC MAP, CMS state averages). state 'US' = national.
CREATE TABLE IF NOT EXISTS market_occupancy (
    state          TEXT,
    property_type  TEXT,
    period         DATE,
    occupancy      DOUBLE,
    source         TEXT,
    rent_per_unit_year DOUBLE    -- market rent per unit of space per year ($/SF/yr office, $/unit/yr apartments); optional
);
-- County treasurer / tax collector: one row per property per tax bill (public in every US county).
CREATE TABLE IF NOT EXISTS property_tax_status (
    address        TEXT,
    city           TEXT,
    state          TEXT,
    county         TEXT,
    parcel         TEXT,
    tax_year       INTEGER,
    amount_due     DOUBLE,       -- unpaid amount (0 = paid)
    due_date       DATE,         -- when the unpaid installment was due
    status         TEXT,         -- paid / delinquent / tax lien / certificate sold / tax sale scheduled
    as_of_date     DATE,         -- when the county's record was read
    source         TEXT,
    file_id        TEXT
);
-- City code enforcement, fire and health inspections (state licensing stays in licensing_records).
CREATE TABLE IF NOT EXISTS compliance_records (
    address        TEXT,
    city           TEXT,
    state          TEXT,
    record_date    DATE,         -- when the violation was cited
    kind           TEXT,         -- code / fire / health / building / license
    status         TEXT,         -- open / closed / complied ...
    description    TEXT,         -- e.g. "unsafe structure", "order to vacate"
    fine_amount    DOUBLE,
    source         TEXT,
    file_id        TEXT
);
-- County assessor roll: assessed or market value per tax year (public for every property).
CREATE TABLE IF NOT EXISTS assessed_values (
    address        TEXT,
    city           TEXT,
    state          TEXT,
    county         TEXT,
    parcel         TEXT,
    tax_year       INTEGER,
    value          DOUBLE,
    source         TEXT,
    file_id        TEXT
);
-- The whole county's assessed-value total per tax year (the assessor's published abstract / roll summary),
-- so a property is compared with its county, not with zero.
CREATE TABLE IF NOT EXISTS county_assessment_trend (
    state          TEXT,
    county         TEXT,
    tax_year       INTEGER,
    total_value    DOUBLE,
    source         TEXT
);
-- Space listed for lease on public listing sites (units, square feet or keys), one row per reading.
CREATE TABLE IF NOT EXISTS space_availability (
    address        TEXT,
    city           TEXT,
    state          TEXT,
    observed_at    DATE,
    available      DOUBLE,       -- units / SF listed as available
    total          DOUBLE,       -- the building's units / SF
    unit_basis     TEXT,         -- unit / sqft
    source_url     TEXT,
    file_id        TEXT
);
"""


def clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


class Sig:
    def __init__(self, code, value=0.0, known=False, why="don't have (no public data found)", q=1.0, counter=None):
        self.code, self.value, self.known, self.why, self.q, self.counter = code, value, known, why, q, counter

    @property
    def effective(self):
        return self.q * self.value


def link_ids(p) -> set[str]:
    ids = set()
    for r in p.projects:
        ids.add(str(r.get("project_id")))
    for r in p.cmbs_loans:
        ids.add(str(r.get("loan_key")))
    for r in p.filings:
        ids.add(str(r.get("file_id")))
    for r in p.recorded_loans:
        ids.add(str(r.get("record_id")))
    ids.add(str(p.property_id))
    return ids - {"None"}


# ---------------------------------------------------------------------------
# Matching public records (tax, code, assessor, listings, licenses) to a property by street address
# ---------------------------------------------------------------------------
def address_key(address, state) -> str | None:
    """'1234 Main St, Springfield' + 'il' -> '1234 main|IL' (house number + first street word + state)."""
    num = re.match(r"\s*(\d+)\s+(\w+)", (address or "").lower())
    return f"{num.group(1)} {num.group(2)}|{(state or '').upper()}" if num else None


def address_keys(p) -> set[str]:
    keys = set()
    for r in p.projects + p.filings + p.cmbs_loans + p.recorded_loans:
        k = address_key(r.get("property_address") or r.get("address"),
                        r.get("state") or r.get("property_state"))
        if k:
            keys.add(k)
    return keys


license_key = address_key            # older names, kept so other modules keep working
_license_keys = address_keys


def _rows_for(p, table: dict) -> list[dict]:
    rows = []
    for k in address_keys(p):
        rows += table.get(k, [])
    return rows


def _annual(row, field):
    v = row.get(field)
    if v is None:
        return None
    months = row.get("period_months") or 12
    return v * 12.0 / months


def _noi(row):
    """NOI as reported, or revenue - expenses when only those are given (so rising costs count)."""
    if row.get("noi") is not None:
        return _annual(row, "noi")
    if row.get("revenue") is not None and row.get("expenses") is not None:
        return (_annual(row, "revenue") or 0.0) - (_annual(row, "expenses") or 0.0)
    return None


def _ops(p, ops: dict, as_of: date) -> list[dict]:
    rows = []
    for i in link_ids(p):
        rows += ops.get(i, [])
    rows = [r for r in rows if as_date(r.get("period_end"))
            and (as_date(r.get("disclosed_on")) or as_date(r["period_end"])) <= as_of]
    return sorted(rows, key=lambda r: as_date(r["period_end"]))


def _ops_by_loan(p, ops: dict, as_of: date) -> dict[str, list[dict]]:
    """Operating history grouped per loan/link, each series sorted by period_end.

    Never merge loans into one series: pari passu/companion notes report separately (BMO
    2023-5C1 notes 1A-3/1A-5 file a literal 0.0 while 1A-1 files 0.9793 for the same
    property in the same period), and a merged series compared different notes against
    each other and fabricated trends. Whether a literal 0.0 is a non-report or true
    vacancy is unresolved source semantics, so zeros are preserved as observed and the
    cross-loan comparison is what is fixed here. One row per loan+period is enforced by the collector; if duplicates ever
    appear, the one from the later file (by file_id) wins, so results stay deterministic."""
    by_loan: dict[str, list[dict]] = {}
    for i in sorted(link_ids(p)):
        seen: dict[date, dict] = {}
        for r in ops.get(i, []):
            d = as_date(r.get("period_end"))
            if not d or (as_date(r.get("disclosed_on")) or d) > as_of:
                continue
            if d not in seen or str(r.get("file_id") or "") > str(seen[d].get("file_id") or ""):
                seen[d] = r
        rows = [seen[d] for d in sorted(seen)]
        if rows:
            by_loan[i] = rows
    return by_loan


def _listing_history(p, listings: dict | None, as_of: date) -> list[dict]:
    """Occupancy read off public listings: 1 - space listed for lease / total space, one row per reading."""
    rows = []
    for r in _rows_for(p, listings or {}):
        d = as_date(r.get("observed_at"))
        avail, total = r.get("available"), r.get("total")
        if d and d <= as_of and avail is not None and total:
            rows.append({"period_end": d, "occupancy": clamp(1 - avail / total)})
    return sorted(rows, key=lambda r: r["period_end"])


# ---------------------------------------------------------------------------
# Q: occupancy slide, measured against the market
# ---------------------------------------------------------------------------
def occupancy_slide(p, ptype, state, c, ops, mkt_occ, as_of, listings: dict | None = None) -> Sig:
    keys = [((state or "").upper(), ptype), ("US", ptype)]
    if ptype == "Senior housing":   # subtype matters: nursing-facility averages are not an
        raw = " ".join(str(r.get("property_type") or "")          # assisted-living benchmark
                       for r in p.projects + p.filings + p.cmbs_loans).lower()
        sub = ("nursing" if ("nursing" in raw or "skilled" in raw)
               else "assisted" if ("assisted" in raw or "independent living" in raw) else None)
        if sub:
            keys = [((state or "").upper(), f"Senior housing ({sub})"), ("US", f"Senior housing ({sub})")] + keys
    series, used = None, None
    for k in keys:
        if mkt_occ.get(k):
            series, used = mkt_occ[k], k[1]
            break
    # Source ladder: loan-level disclosures first (each loan trended on its own history only, see
    # _ops_by_loan); only when no loan reports occupancy, the share of the building listed for lease.
    histories = {k: [r for r in h if r.get("occupancy") is not None]
                 for k, h in _ops_by_loan(p, ops, as_of).items()}
    histories = {k: h for k, h in histories.items() if len(h) >= 2}
    from_listings = False
    if not histories:
        lst = _listing_history(p, listings, as_of)
        if len(lst) >= 2:
            histories, from_listings = {"listings": lst}, True
    # Literal 0.0 readings are kept as observed: whether they are non-reports or true
    # vacancy is unresolved source semantics, and dropping them could silently suppress real distress.
    best = None
    for rows in histories.values():
        now = rows[-1]
        now_d = as_date(now["period_end"])
        window = [r for r in rows if (now_d - as_date(r["period_end"])).days <= c["lookback_months"] * 30.5]
        peak = max(window, key=lambda r: r["occupancy"])
        drop = peak["occupancy"] - now["occupancy"]
        if series:
            m_then = min(series, key=lambda t: abs((t[0] - as_date(peak["period_end"])).days))[1]
            m_now = min(series, key=lambda t: abs((t[0] - now_d).days))[1]
            mkt_drop = m_then - m_now
        else:
            m_then = m_now = mkt_drop = 0.0
        gap = drop - mkt_drop
        if best is None or gap > best[0]:          # sorted loan order keeps ties deterministic
            best = (gap, drop, peak, now, m_then, m_now)
    if best is None:
        return Sig("Q")
    gap, drop, peak, now, m_then, m_now = best
    q = c["reliability"]
    src = " (from space listed for lease)" if from_listings else ""
    if from_listings:
        q *= c.get("listing_penalty", 0.7)
    if series:
        why = (f"occupancy {peak['occupancy']:.1%} -> {now['occupancy']:.1%} ({drop * 100:.1f} pts) vs. {used or ''} market "
               f"{m_then:.1%} -> {m_now:.1%} ({(m_then - m_now) * 100:.1f} pts){src}")
    else:
        q *= c["no_market_penalty"]
        why = (f"occupancy {peak['occupancy']:.1%} -> {now['occupancy']:.1%} ({drop * 100:.1f} pts); "
               f"no market data (trusted less){src}")
    counter = (f"occupancy up {-drop * 100:.1f} pts" if -drop >= c["counter_rise"] else None)
    return Sig("Q", clamp(gap / c["full_gap"]), True, why, q, counter)


# ---------------------------------------------------------------------------
# V: income erosion (NOI now vs. its best year in the lookback) - no DSCR. Absorbs the old cost squeeze:
#    when expenses grow faster than revenue, NOI falls, so counting both would count it twice.
# ---------------------------------------------------------------------------
def income_erosion(p, c, ops, as_of) -> Sig:
    best = None
    for history in _ops_by_loan(p, ops, as_of).values():   # per loan, never across loans
        rows = [r for r in history if _noi(r) is not None]
        if not rows:
            continue
        now = rows[-1]
        now_d = as_date(now["period_end"])
        noi_now = _noi(now)
        annual = [_noi(r) for r in rows if (r.get("period_months") or 12) >= 12
                  and (now_d - as_date(r["period_end"])).days <= c["lookback_months"] * 30.5]
        peak = max(annual) if annual else None
        if not peak or peak <= 0:
            continue
        erosion = (peak - noi_now) / peak
        if best is None or erosion > best[0]:
            best = (erosion, noi_now, peak)
    if best is None:
        return Sig("V")
    erosion, noi_now, peak = best
    return Sig("V", clamp(erosion / c["full_noi_drop"]), True,
               f"NOI ${noi_now / 1e6:.2f}M/yr vs. best year ${peak / 1e6:.2f}M ({-erosion:+.0%})", c["reliability"])


# ---------------------------------------------------------------------------
# W: value cut. 1) CMBS appraisal (SEC Schedule AL)  2) county assessor value vs. the county's own change
# ---------------------------------------------------------------------------
def value_cut(p, c, as_of, assessed: dict | None = None, county_trend: dict | None = None) -> Sig:
    best = Sig("W")
    for ln in p.cmbs_loans:
        v0, v1 = ln.get("value_securitization"), ln.get("value_recent")
        d1 = as_date(ln.get("value_recent_date")) or as_date(ln.get("report_period"))
        if not v0 or v0 <= 0 or v1 is None or (d1 and d1 > as_of):
            continue
        drop = (v0 - v1) / v0
        s = Sig("W", clamp(drop / c["full_drop"]), True,
                f"appraisal ${v0 / 1e6:.1f}M at securitization -> ${v1 / 1e6:.1f}M most recent ({-drop:+.0%})",
                c["reliability"])
        if not best.known or s.value >= best.value:
            best = s
    if best.known:
        return best                     # the appraisal is the better source; the assessor is the fallback
    return assessor_value_cut(p, c, as_of, assessed, county_trend)


def assessor_value_cut(p, c, as_of, assessed, county_trend) -> Sig:
    a = c.get("assessor", {})
    years: dict[int, dict] = {}
    for r in _rows_for(p, assessed or {}):
        y, v = r.get("tax_year"), r.get("value")
        if y is None or v is None or int(y) > as_of.year:
            continue
        years[int(y)] = r                # one value per tax year
    if len(years) < 2:
        return Sig("W")
    now_y = max(years)
    window = [y for y in years if now_y - a.get("lookback_years", 3) <= y < now_y]
    if not window:
        return Sig("W")
    peak_y = max(window, key=lambda y: years[y]["value"])
    v_then, v_now = years[peak_y]["value"], years[now_y]["value"]
    if not v_then or v_then <= 0:
        return Sig("W")
    drop = (v_then - v_now) / v_then
    q = a.get("reliability", 0.6)
    ref = years[now_y]
    trend = (county_trend or {}).get(((ref.get("state") or "").upper(), (ref.get("county") or "").strip().lower()), {})
    if peak_y in trend and now_y in trend and trend[peak_y]:
        mkt_drop = (trend[peak_y] - trend[now_y]) / trend[peak_y]
        why = (f"assessed value {peak_y} ${v_then / 1e6:.1f}M -> {now_y} ${v_now / 1e6:.1f}M ({-drop:+.0%}) "
               f"vs. county {-mkt_drop:+.0%}")
    else:
        mkt_drop = 0.0
        q *= a.get("no_county_penalty", 0.7)
        why = f"assessed value {peak_y} ${v_then / 1e6:.1f}M -> {now_y} ${v_now / 1e6:.1f}M ({-drop:+.0%}); no county trend"
    return Sig("W", clamp((drop - mkt_drop) / a.get("full_gap", 0.25)), True, why, q)


# ---------------------------------------------------------------------------
# T: unpaid property tax (county treasurer / tax collector, or a tax-delinquency notice the Reader read)
# ---------------------------------------------------------------------------
_PAID = re.compile(r"\b(paid|current)\b", re.I)
TAX_LIEN = re.compile(r"\b(lien|certificate|tax sale|tax deed)\b", re.I)


def _tags(f) -> list[str]:
    tags = f.get("distress_tags") or []
    if isinstance(tags, str):
        tags = [t.strip(' "[]') for t in tags.split(",")]
    return list(tags)


def unpaid_tax(p, c, as_of, tax_rows: dict | None = None) -> Sig:
    """s = first_missed (0.5) at any missed installment, rising in a straight line to 1.0 at full_months late.
    A tax LIEN / certificate sale / tax sale is past the early stage: it counts in the late score instead."""
    rows = [r for r in _rows_for(p, tax_rows or {})
            if (as_date(r.get("as_of_date")) or as_date(r.get("due_date")) or date.min) <= as_of]
    unpaid_since, known, latest_paid = [], bool(rows), None
    for r in rows:
        status = str(r.get("status") or "")
        due = as_date(r.get("due_date"))
        owed = r.get("amount_due")
        unpaid = (owed is not None and owed > 0) or bool(status and not _PAID.search(status))
        if unpaid and due and due <= as_of:
            unpaid_since.append(due)
        elif not unpaid:
            d = as_date(r.get("as_of_date")) or due
            if d:
                latest_paid = max(latest_paid, d) if latest_paid else d
    for f in p.filings:                  # a delinquent-tax notice in the county's public notices
        if (f.get("filing_type") or f.get("doc_category")) == "tax_delinquency" or "tax_delinquent" in _tags(f):
            d = as_date(f.get("filing_date"))
            if d and d <= as_of:
                unpaid_since.append(d)
                known = True
    unpaid_since = [d for d in unpaid_since if not latest_paid or d > latest_paid]   # paid off since
    if not unpaid_since:
        return Sig("T", 0.0, True, "property taxes paid current", c["reliability"]) if known else Sig("T")
    first = min(unpaid_since)
    months = max(0.0, (as_of - first).days / 30.4)
    s = c["first_missed"] + (1 - c["first_missed"]) * min(1.0, months / c["full_months"])
    return Sig("T", clamp(s), True, f"property taxes unpaid since {first} ({months:.0f} months)", c["reliability"])


def tax_lien_rows(p, tax_rows: dict | None, as_of) -> list[dict]:
    """Tax liens / certificate sales / tax sales on this property (these count in the LATE score)."""
    return [r for r in _rows_for(p, tax_rows or {})
            if TAX_LIEN.search(str(r.get("status") or ""))
            and (as_date(r.get("as_of_date")) or as_date(r.get("due_date")) or date.min) <= as_of]


# ---------------------------------------------------------------------------
# U: compliance trouble (code / fire / health violations, state licensing, CMS stars)
# ---------------------------------------------------------------------------
_STARS = re.compile(r"(\d)\s*-?\s*star", re.I)
_CLOSED = re.compile(r"\b(closed|complied|compliance|resolved|dismissed|abated|void)\b", re.I)
_SEVERE = re.compile(r"\b(unsafe|condemn|vacate|red[- ]tag|imminent|uninhabitable|dangerous)\w*", re.I)


def _licensing_strength(r, c) -> tuple[float, list[str]]:
    text = f"{r.get('status') or ''} {r.get('distress_reason') or ''}"
    s, bits = 0.0, []
    m = _STARS.search(text)
    if m:
        stars = int(m.group(1))
        s = c["star_strength"].get(stars, c["star_strength"].get(str(stars), 0.0))
        bits.append(f"CMS {stars}-star")
    if r.get("distress_flag"):
        s = max(s, c["state_distress"])
        bits.append(r.get("distress_reason") or "state license distress")
    if "abuse" in text.lower():
        s += c["abuse_bonus"]
        bits.append("abuse flag")
    return s, bits


def compliance_trouble(p, c, as_of, compliance: dict | None = None, licenses: dict | None = None) -> Sig:
    best = Sig("U")
    lic = [r for r in _rows_for(p, licenses or {})
           if not as_date(r.get("as_of_date")) or as_date(r["as_of_date"]) <= as_of]
    if lic:
        best = Sig("U", 0.0, True, "licensed, no quality flags", c["reliability"])
    for r in lic:
        s, bits = _licensing_strength(r, c)
        if s > best.value:
            best = Sig("U", clamp(s), True, f"{r.get('facility_name') or 'facility'}: " + ", ".join(bits), c["reliability"])
    recs = []
    for r in _rows_for(p, compliance or {}):
        d = as_date(r.get("record_date"))
        if d and d <= as_of and (as_of - d).days <= c["lookback_months"] * 30.5:
            recs.append(r)
    if recs and not best.known:
        best = Sig("U", 0.0, True, "no open violations", c["reliability"])
    open_recs = [r for r in recs if not _CLOSED.search(str(r.get("status") or ""))]
    if open_recs:
        severe = [r for r in open_recs if _SEVERE.search(f"{r.get('description') or ''} {r.get('status') or ''}")]
        s = 1.0 if severe else min(1.0, c["per_open_violation"] * len(open_recs))
        kinds = sorted({str(r.get("kind") or "code") for r in open_recs})
        why = (f"{len(open_recs)} open {'/'.join(kinds)} violation(s) in the last {c['lookback_months']} months"
               + (f", incl. '{severe[0].get('description')}'" if severe else ""))
        if s > best.value:
            best = Sig("U", clamp(s), True, why, c["reliability"])
    return best


# Older name: licensing is part of compliance trouble now.
def licensing_quality(p, c, licenses, as_of) -> Sig:
    return compliance_trouble(p, c, as_of, None, licenses)


# ---------------------------------------------------------------------------
# Combine
# ---------------------------------------------------------------------------
def combine(sigs: list[Sig], cfg: dict) -> tuple[float, int, str]:
    """C = 1 - prod(1 - q*s). Returns (C, how many creative signals 'show', explanation)."""
    prod = 1.0
    for s in sigs:
        prod *= 1 - s.effective
    value = 1 - prod
    showing = sum(1 for s in sigs if s.effective >= cfg["shows_at"])
    on = [f"{s.code} {s.effective:.2f}" for s in sigs if s.effective > 0]
    why = ("creative signals: " + ", ".join(on)) if on else "no creative signals"
    return value, showing, why
