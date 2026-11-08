"""
signals.py - finds every warning sign on every property.

Reads (never writes) the other parts' tables:
  Part A: recorded_loans, loans, bank_stats
  Part B: filing_fields, projects
  Part C: links (property_id, record_table, record_id) - when Part C exists

Until Part C's `links` table exists, records are grouped by a cleaned-up
street address + state. That is a stand-in: Part C's matcher is what ties a
project website, its construction loan, its contractor lien and its lender together.

Each signal says WHO is pushing the owner (court_or_tax / lender / contractor / market),
where it came from, and the exact source sentence, so the researcher can check it.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from .store import read_table

# ---------------------------------------------------------------------------
# Cleaning helpers
# ---------------------------------------------------------------------------
_STREET_WORDS = {
    "street": "st", "avenue": "ave", "road": "rd", "boulevard": "blvd", "drive": "dr",
    "lane": "ln", "court": "ct", "place": "pl", "parkway": "pkwy", "highway": "hwy",
    "north": "n", "south": "s", "east": "e", "west": "w", "suite": "ste",
}


def clean_address(address: str | None, state: str | None) -> str | None:
    if not address or not str(address).strip():
        return None
    words = re.sub(r"[^a-z0-9 ]", " ", str(address).lower()).split()
    words = [_STREET_WORDS.get(w, w) for w in words]
    return f"{' '.join(words)}|{(state or '').strip().upper()}"


_TYPE_RULES = [
    ("student", "Student housing"), ("senior", "Senior housing"), ("assisted", "Senior housing"),
    ("nursing", "Senior housing"), ("skilled", "Senior housing"), ("multi", "Multifamily"),
    ("apartment", "Multifamily"), ("office", "Office"), ("hotel", "Hotel"), ("lodging", "Hotel"),
    ("industrial", "Industrial"), ("warehouse", "Industrial"), ("storage", "Self storage"),
    ("retail", "Retail"), ("mixed", "Mixed use"),
]
_CMBS_CODES = {"MF": "Multifamily", "OF": "Office", "RT": "Retail", "LO": "Hotel", "IN": "Industrial",
               "SS": "Self storage", "MU": "Mixed use", "HC": "Senior housing", "WH": "Industrial"}


def clean_property_type(raw) -> str | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)) or not str(raw).strip():
        return None
    text = str(raw).strip()
    if text.upper() in _CMBS_CODES:
        return _CMBS_CODES[text.upper()]
    low = text.lower()
    for needle, label in _TYPE_RULES:
        if needle in low:
            return label
    return text.title()


def clean_company(name) -> str:
    if not name or (isinstance(name, float) and pd.isna(name)):
        return ""
    s = re.sub(r"[^a-z0-9 ]", " ", str(name).lower())
    s = re.sub(r"\b(llc|l l c|inc|corp|corporation|co|company|na|n a|lp|ltp|ltd|the)\b", " ", s)
    return " ".join(s.split())


def _val(x):
    """pandas NaN / NaT -> None."""
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return x


def _date(x) -> date | None:
    x = _val(x)
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    if isinstance(x, pd.Timestamp):
        return x.date()
    try:
        return pd.to_datetime(x).date()
    except Exception:
        return None


def _first_quote(quotes) -> str | None:
    quotes = _val(quotes)
    if not quotes:
        return None
    if isinstance(quotes, str):
        try:
            quotes = json.loads(quotes)
        except ValueError:
            return quotes
    if isinstance(quotes, dict):
        for v in quotes.values():
            if v:
                return str(v)
    if isinstance(quotes, list) and quotes:
        return str(quotes[0])
    return None


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    code: str                    # e.g. "mechanics_lien"
    label: str                   # plain English, shown to the researcher
    pusher: str | None           # court_or_tax / lender / contractor / market / None
    detail: str
    source: str                  # table + id, so the researcher can find the document
    quote: str | None = None     # exact sentence from the document
    event_date: date | None = None
    deadline: date | None = None
    deadline_what: str | None = None
    stage: int = 0               # escalation: 1 lien, 2 lawsuit, 3 judgment/foreclosure/sale
    public: bool = False         # already visible to everyone (auction, public sale)
    options_used_up: bool = False
    temporary: bool = False      # a delay / stuck refinance, not a lack of demand
    permanent: bool = False      # a lack of demand

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        for k in ("event_date", "deadline"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


@dataclass
class Property:
    key: str
    names: list = field(default_factory=list)
    address: str | None = None
    city: str | None = None
    state: str | None = None
    types: list = field(default_factory=list)
    sponsors: list = field(default_factory=list)
    lenders: list = field(default_factory=list)
    loan_amounts: list = field(default_factory=list)
    dscr: float | None = None
    signals: list = field(default_factory=list)
    has_co: bool = False

    def first(self, items):
        items = [i for i in items if i]
        return items[0] if items else None

    @property
    def name(self):
        return self.first(self.names) or self.address or self.key

    @property
    def property_type(self):
        return self.first(self.types)

    @property
    def sponsor(self):
        return self.first(self.sponsors)

    @property
    def lender(self):
        return self.first(self.lenders)

    @property
    def loan_amount(self):
        amounts = [a for a in self.loan_amounts if a]
        return max(amounts) if amounts else None


# ---------------------------------------------------------------------------
# Grouping records into properties
# ---------------------------------------------------------------------------
class Grouper:
    def __init__(self, links: pd.DataFrame):
        self.by_record = {}
        for row in links.itertuples(index=False):
            if _val(row.record_table) and _val(row.record_id) and _val(row.property_id):
                self.by_record[(row.record_table, str(row.record_id))] = str(row.property_id)
        self.props: dict[str, Property] = {}

    def get(self, table, record_id, address, city, state) -> Property | None:
        key = self.by_record.get((table, str(record_id)))
        if not key:
            key = clean_address(address, state)
        if not key:
            return None
        prop = self.props.setdefault(key, Property(key=key))
        prop.address = prop.address or _val(address)
        prop.city = prop.city or _val(city)
        prop.state = prop.state or (_val(state) or None)
        return prop


FILING_RULES = {
    # filing_type: (code, label, pusher, stage, public, deadline field, deadline label)
    "foreclosure_notice": ("foreclosure", "Foreclosure notice", "court_or_tax", 3, True, "sale_date", "Foreclosure sale"),
    "receivership": ("receivership", "Receiver appointed", "court_or_tax", 3, False, None, None),
    "lis_pendens": ("lis_pendens", "Lawsuit filed against the property (lis pendens)", "court_or_tax", 2, False, None, None),
    "lender_lawsuit": ("lender_lawsuit", "Lender sued the borrower", "court_or_tax", 2, False, None, None),
    "tax_delinquency": ("tax_delinquency", "Property taxes unpaid", "court_or_tax", 0, False, "sale_date", "Tax sale"),
    "ucc_sale_notice": ("ucc_sale", "UCC sale of the ownership company", "lender", 3, True, "sale_date", "UCC sale"),
    "auction_listing": ("auction", "Listed at auction", "lender", 0, True, "auction_date", "Auction"),
    "mechanics_lien": ("mechanics_lien", "Contractor lien", "contractor", 1, False, None, None),
}


def find_signals(con, settings: dict, today: date | None = None) -> list[Property]:
    today = today or date.today()
    rules = settings["signals"]
    grouper = Grouper(read_table(con, "links", ["property_id", "record_table", "record_id"]))

    # --- Part B: filings the reader has turned into fields -------------------
    ff = read_table(con, "filing_fields", [
        "file_id", "filing_type", "borrower_name", "developer_name", "project_name", "property_address",
        "property_city", "property_state", "property_type", "lender_name", "loan_amount", "amount_owed",
        "maturity_date", "filing_date", "sale_date", "auction_date", "lien_claimant", "lien_amount",
        "lien_work", "case_number", "quotes", "status", "permit_status",
    ])
    maturity_by_file = {}
    auctions_by_prop = defaultdict(int)
    for r in ff.itertuples(index=False):
        if _val(r.status) == "failed":
            continue
        if _val(r.maturity_date):
            maturity_by_file[str(r.file_id)] = _date(r.maturity_date)
        prop = grouper.get("filing_fields", r.file_id, r.property_address, r.property_city, r.property_state)
        if not prop:
            continue
        prop.names.append(_val(r.project_name))
        prop.types.append(clean_property_type(r.property_type))
        prop.sponsors.extend([_val(r.developer_name), _val(r.borrower_name)])
        prop.lenders.append(_val(r.lender_name))
        prop.loan_amounts.append(_val(r.loan_amount))
        ftype = _val(r.filing_type)
        if ftype == "certificate_of_occupancy" or (ftype == "building_permit" and "occupancy" in str(_val(r.permit_status) or "").lower()):
            prop.has_co = True
            continue
        if ftype not in FILING_RULES:
            continue
        code, label, pusher, stage, public, dl_field, dl_label = FILING_RULES[ftype]
        deadline = _date(getattr(r, dl_field)) if dl_field else None
        if deadline and deadline < today:
            deadline = None  # already happened
        if code == "mechanics_lien":
            amt = _val(r.lien_amount)
            detail = f"{_val(r.lien_claimant) or 'A contractor'} filed a lien" + (f" for ${amt:,.0f}" if amt else "")
            if _val(r.lien_work):
                detail += f" ({r.lien_work})"
        elif code == "auction":
            auctions_by_prop[prop.key] += 1
            detail = "Listed for sale at auction" + (f", auction on {deadline:%b %d, %Y}" if deadline else "")
        else:
            owed = _val(r.amount_owed)
            detail = label + (f", ${owed:,.0f} owed" if owed else "") + (f", {dl_label.lower()} on {deadline:%b %d, %Y}" if deadline else "")
        prop.signals.append(Signal(
            code=code, label=label, pusher=pusher, detail=detail, source=f"filing_fields:{r.file_id}",
            quote=_first_quote(r.quotes), event_date=_date(r.filing_date), deadline=deadline,
            deadline_what=dl_label if deadline else None, stage=stage, public=public,
        ))
    for key, n in auctions_by_prop.items():
        if n > 1:
            for s in grouper.props[key].signals:
                if s.code == "auction":
                    s.options_used_up = True
                    s.detail += f" (listed {n} times: did not sell before)"
                    break

    # --- Part B: projects the scout tracks (stale opening dates) -------------
    pj = read_table(con, "projects", [
        "project_id", "project_name", "developer_name", "property_address", "city", "state", "property_type",
        "stated_opening_phrase", "opening_deadline", "stale_candidate", "months_overdue", "missed_lease_up",
        "date_change_count", "first_stated_phrase", "main_url",
    ])
    move_m, move_d = (int(x) for x in rules["student_move_in"].split("-"))
    for r in pj.itertuples(index=False):
        prop = grouper.get("projects", r.project_id, r.property_address, r.city, r.state)
        if not prop:
            continue
        prop.names.insert(0, _val(r.project_name))
        prop.types.insert(0, clean_property_type(r.property_type))
        prop.sponsors.append(_val(r.developer_name))
        if not _val(r.stale_candidate):
            continue
        phrase = _val(r.stated_opening_phrase) or "an opening date"
        months = _val(r.months_overdue)
        slips = int(_val(r.date_change_count) or 0)
        detail = f"Website still says “{phrase}”"
        if months:
            detail += f", {months:.0f} months past the latest reasonable opening date"
        if slips:
            detail += f"; the date changed {slips} time{'s' if slips > 1 else ''}"
            if _val(r.first_stated_phrase):
                detail += f" (first promised “{r.first_stated_phrase}”)"
        sig = Signal(
            code="stale_opening", label="Stale opening date", pusher="market", detail=detail,
            source=f"projects:{r.project_id}", quote=_val(r.main_url), event_date=_date(r.opening_deadline),
            options_used_up=slips >= 2, temporary=True,
        )
        if _val(r.missed_lease_up):
            nxt = date(today.year, move_m, move_d)
            if nxt <= today:
                nxt = date(today.year + 1, move_m, move_d)
            sig.deadline, sig.deadline_what = nxt, "Next student move-in"
            sig.detail += ". Student housing that missed the August move-in: little rent until next August"
        prop.signals.append(sig)

    # --- Part A: recorded loans (stuck construction loans) --------------------
    rl = read_table(con, "recorded_loans", [
        "record_id", "file_id", "state", "county", "document_type", "loan_kind", "recording_date",
        "grantor", "grantee", "amount", "property_address", "instrument_number",
    ])
    chains = defaultdict(list)
    for r in rl.itertuples(index=False):
        prop = grouper.get("recorded_loans", r.record_id, r.property_address, None, r.state)
        if prop:
            chains[prop.key].append(r)
    for key, rows in chains.items():
        prop = grouper.props[key]
        rows = sorted(rows, key=lambda x: _date(x.recording_date) or date.min)
        constructions = [x for x in rows if _val(x.loan_kind) == "construction"]
        if not constructions:
            continue
        c = constructions[-1]
        c_date = _date(c.recording_date)
        prop.sponsors.append(_val(c.grantor))   # on a mortgage, the borrower grants it...
        prop.lenders.insert(0, _val(c.grantee))  # ...to the lender
        prop.loan_amounts.append(_val(c.amount))
        later = [x for x in rows if (_date(x.recording_date) or date.min) >= (c_date or date.min) and x is not c]
        if any(_val(x.loan_kind) in ("permanent", "release") for x in later):
            continue  # healthy: refinanced out
        ext = [x for x in later if _val(x.loan_kind) in ("extension", "modification")]
        maturity = maturity_by_file.get(str(_val(c.file_id)))
        assumed = False
        if not maturity and c_date:
            maturity = date(c_date.year + int(rules["construction_loan_years"]), c_date.month, min(c_date.day, 28))
            assumed = True
        amt = _val(c.amount)
        base = f"Construction loan{f' of ${amt:,.0f}' if amt else ''} from {_val(c.grantee) or 'the lender'}" \
               f"{f' recorded {c_date:%b %Y}' if c_date else ''}"
        if maturity and maturity < today:
            detail = f"{base}, due {'about ' if assumed else ''}{maturity:%b %Y}, with no permanent loan or release recorded"
            if ext:
                detail += f"; {len(ext)} extension{'s' if len(ext) > 1 else ''} or modification{'s' if len(ext) > 1 else ''} recorded"
            prop.signals.append(Signal(
                code="stuck_construction_loan", label="Construction loan never converted", pusher="lender",
                detail=detail, source=f"recorded_loans:{c.record_id}", event_date=maturity,
                options_used_up=len(ext) >= int(rules["extensions_for_options_used_up"]), temporary=True,
            ))
        elif ext:
            prop.signals.append(Signal(
                code="loan_extended", label="Construction loan extended", pusher="lender",
                detail=f"{base}; {len(ext)} extension(s) or modification(s) recorded and no permanent loan yet",
                source=f"recorded_loans:{ext[-1].record_id}", event_date=_date(ext[-1].recording_date),
                deadline=maturity if maturity and maturity >= today else None,
                deadline_what="Construction loan due" if maturity and maturity >= today else None,
                options_used_up=len(ext) >= int(rules["extensions_for_options_used_up"]), temporary=True,
            ))

    # --- Part A: CMBS / Trepp loans (DSCR, watchlist, delinquency) -----------
    ln = read_table(con, "loans", [
        "loan_key", "source", "property_name", "property_address", "property_city", "property_state",
        "property_type", "current_balance", "maturity_date", "dscr", "occupancy", "payment_status",
        "servicer_name", "watchlist_flag", "servicer_commentary",
    ])
    for r in ln.itertuples(index=False):
        prop = grouper.get("loans", r.loan_key, r.property_address, r.property_city, r.property_state)
        if not prop:
            continue
        prop.names.append(_val(r.property_name))
        prop.types.append(clean_property_type(r.property_type))
        prop.loan_amounts.append(_val(r.current_balance))
        dscr = _val(r.dscr)
        if dscr is not None:
            prop.dscr = float(dscr)
        src = f"loans:{r.loan_key}"
        quote = _val(r.servicer_commentary)
        maturity = _date(r.maturity_date)
        upcoming = maturity if maturity and maturity >= today else None
        if dscr is not None and dscr < float(rules["dscr_below"]):
            prop.signals.append(Signal(
                code="low_dscr", label=f"DSCR below {rules['dscr_below']}x", pusher="lender",
                detail=f"Income covers only {dscr:.2f}x the loan payments ({_val(r.source) or 'loan data'})",
                source=src, quote=quote, deadline=upcoming, deadline_what="Loan maturity" if upcoming else None,
            ))
        status = str(_val(r.payment_status) or "").lower()
        if any(w in status for w in ("delinq", "default", "foreclos", "reo", "90", "60", "30", "matured", "non-perform")):
            prop.signals.append(Signal(
                code="delinquent", label="Loan delinquent", pusher="lender",
                detail=f"Payment status: {r.payment_status}", source=src, quote=quote,
            ))
        if _val(r.watchlist_flag):
            prop.signals.append(Signal(
                code="watchlist", label="On the servicer's watchlist", pusher="lender",
                detail="The loan servicer put this loan on its watchlist", source=src, quote=quote,
            ))
        occ = _val(r.occupancy)
        if occ is not None and float(occ) > 1:
            occ = float(occ) / 100
        if occ is not None and occ < 0.7:
            prop.signals.append(Signal(
                code="low_occupancy", label="Low occupancy", pusher=None,
                detail=f"Only {occ:.0%} occupied", source=src, permanent=True,
            ))

    # --- Part A: banks under pressure (FFIEC), matched to each property's lender
    bs = read_table(con, "bank_stats", [
        "rssd_id", "bank_name", "report_date", "cre_nonaccrual_ratio", "nonaccrual_change_qoq", "rising_two_quarters",
    ])
    latest_bank = {}
    for r in bs.sort_values("report_date").itertuples(index=False) if len(bs) else []:
        latest_bank[clean_company(r.bank_name)] = r
    alert = float(rules["bank_nonaccrual_ratio_alert"])
    for prop in grouper.props.values():
        for lender in [lender for lender in prop.lenders if lender]:
            b = latest_bank.get(clean_company(lender))
            if not b:
                continue
            ratio = _val(b.cre_nonaccrual_ratio)
            rising = bool(_val(b.rising_two_quarters))
            if (ratio is not None and ratio > alert) or rising:
                bits = []
                if ratio is not None:
                    bits.append(f"{ratio:.1%} of its commercial real estate loans are nonaccrual")
                if rising:
                    bits.append("rising two quarters in a row")
                prop.signals.append(Signal(
                    code="bank_stress", label="Lender's bank is under pressure", pusher=None,
                    detail=f"{b.bank_name}: {', '.join(bits)}. It may sell this loan at a discount.",
                    source=f"bank_stats:{b.rssd_id}",
                ))
            break

    # A certificate of occupancy means the "stale" website is just out of date.
    for prop in grouper.props.values():
        if prop.has_co:
            prop.signals = [s for s in prop.signals if s.code != "stale_opening"]

    return [p for p in grouper.props.values() if p.signals]
