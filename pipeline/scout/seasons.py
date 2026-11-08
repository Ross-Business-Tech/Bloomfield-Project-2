"""
seasons.py - turns an opening phrase like "Opening Winter 2026" into a deadline date.

In plain English: a project website says when the building will open, in words. To spot a late project
we need a date. We always use the LATEST REASONABLE date the phrase could mean, so we never flag a
project that is not actually late yet. Every rule lives in the RULES table below, so changing a rule
means changing one line, and every rule has a test (tests/test_seasons.py).

    to_deadline("Opening Fall 2026")            -> (2026-12-31, "...")
    interpret("Opening Winter 2025", student_housing=True)   -> deadline 2026-03-20, lease_up_date 2025-08-31

Student housing is special: students sign leases for an August move-in. If the building misses that
August, there is usually no rent for a full year. So for student housing we also compute lease_up_date =
August 31 of the year in the phrase. (For an "Academic year 2026-27" phrase it is set for any property.)

The same deadline rules are used by the browser demo (src/lib/openingDate.js); keep the two in step.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

# ---------------------------------------------------------------------------
# THE RULES TABLE. Edit values here. (month, day) are the latest reasonable date.
# ---------------------------------------------------------------------------
SEASON_DEADLINES = {
    # season: (months to add to the year, month, day)
    "spring": (0, 6, 20),
    "summer": (0, 9, 21),
    "fall": (0, 12, 31),     # student housing move-in is usually August, so "Fall" may already be late then
    "autumn": (0, 12, 31),
    "winter": (1, 3, 20),    # ambiguous: early 2026, or Dec 2026 / early 2027. We allow until March of the NEXT year.
}
VAGUE_DEADLINES = {          # early / mid / late <year>
    "early": (4, 30), "beginning": (4, 30), "mid": (8, 31), "late": (12, 31), "end": (12, 31),
}
ACADEMIC_YEAR_DEADLINE = (12, 31)    # "Academic year 2026-27" -> Dec 31 of the first year
LEASE_UP = (8, 31)                   # student housing: August 31 of the year in the phrase
YEAR_ONLY_DEADLINE = (12, 31)        # "Opening 2026" -> Dec 31
HALF_DEADLINES = {1: (6, 30), 2: (12, 31)}

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS["sept"] = 9
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_YEAR = r"((?:19|20)\d{2}|['’]\d{2})"
_ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}


def _year(text: str) -> int:
    digits = re.sub(r"\D", "", text)
    return 2000 + int(digits) if len(digits) == 2 else int(digits)


def _end_of_month(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


@dataclass
class Opening:
    phrase: str
    deadline: date | None          # None = "coming soon" with no date, or no year: not late on its own
    lease_up_date: date | None     # student housing: the August 31 move-in that matters
    year: int | None               # the year the phrase refers to
    note: str                      # plain-English explanation (shown next to the flag)
    rule: str | None = None        # name of the rule that matched


# ---------------------------------------------------------------------------
# Each rule: (name, regex, function(match) -> (deadline, year, note, academic_year?))
# Rules are tried in order; the first match wins (most specific first).
# ---------------------------------------------------------------------------
def _exact_full(m):
    y = _year(m[3])
    return date(y, _MONTHS[m[1].lower()], int(m[2])), y, "An exact date is given, so that is the deadline.", False


def _exact_numeric(m):
    y = int(m[3])
    return date(y, int(m[1]), int(m[2])), y, "An exact date is given, so that is the deadline.", False


def _month_year(m):
    y = _year(m[2])
    return _end_of_month(y, _MONTHS[m[1].lower()]), y, "A month is given, so we allow until the last day of that month.", False


def _month_year_numeric(m):
    y = int(m[2])
    return _end_of_month(y, int(m[1])), y, "A month is given, so we allow until the last day of that month.", False


def _academic_year(m):
    # the first year is in group 1 (as two digits after the century) or group 3 (the "academic year 2026-27" order)
    y = int("20" + m[1]) if m[1] else int(m[2])
    return date(y, *ACADEMIC_YEAR_DEADLINE), y, (
        "An academic year is given. Students move in by late August, so lease-up is Aug 31; "
        "we allow until Dec 31 before calling it late."), True


def _season(m):
    y = _year(m[2])
    add, month, day = SEASON_DEADLINES[m[1].lower()]
    note = f"A season is given, so we allow until the end of that season ({calendar.month_name[month]} {day})."
    if m[1].lower() == "winter":
        note = (f"'Winter {y}' is ambiguous: it could mean early {y} or December {y} / early {y + 1}. "
                f"Using the latest, March 20, {y + 1}.")
    elif m[1].lower() in ("fall", "autumn"):
        note += " For student housing, move-in is usually August, so this may already be late for a student building."
    return date(y + add, month, day), y, note, False


def _quarter(m):
    q = int(m[1]) if m[1] else _ORDINAL[m[2].lower()]
    y = _year(m[3])
    return _end_of_month(y, q * 3), y, "A quarter is given, so we allow until the last day of that quarter.", False


def _half(m):
    h = int(m[1]) if m[1] else _ORDINAL[m[2].lower()]
    y = _year(m[3])
    month, day = HALF_DEADLINES[h]
    return date(y, month, day), y, "Half a year is given, so we allow until the end of that half.", False


def _vague(m):
    y = _year(m[2])
    month, day = VAGUE_DEADLINES[re.sub(r"\s+.*", "", m[1].lower())]
    return date(y, month, day), y, ('"Early" means by April 30, "mid" by August 31, "late" or "end of" by December 31.'), False


def _year_only(m):
    y = _year(m[1])
    return date(y, *YEAR_ONLY_DEADLINE), y, "Only a year is given, so we allow until December 31 of that year.", False


RULES = [
    ("exact_date_words", re.compile(rf"\b{_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+{_YEAR}", re.I), _exact_full),
    ("exact_date_numeric", re.compile(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\b"), _exact_numeric),
    ("month_year", re.compile(rf"\b{_MONTH}\.?,?\s+(?:of\s+)?{_YEAR}", re.I), _month_year),
    ("month_year_numeric", re.compile(r"\b(\d{1,2})/((?:19|20)\d{2})\b"), _month_year_numeric),
    ("academic_year", re.compile(
        r"(?:\b(?:19|20)(\d{2})\s*[-–/]\s*(?:\d{2}|(?:19|20)\d{2})\s+(?:school|academic)\s+year"
        r"|\b(?:school|academic)\s+year\s+((?:19|20)\d{2})\s*[-–/]\s*(?:\d{2}|(?:19|20)\d{2}))", re.I),
     _academic_year),
    ("season_year", re.compile(rf"\b(?:early|mid|late)?[-\s]*(spring|summer|fall|autumn|winter)\s+(?:of\s+)?{_YEAR}", re.I), _season),
    ("quarter_year", re.compile(
        rf"\b(?:q([1-4])|([1-4](?:st|nd|rd|th)|first|second|third|fourth)\s+quarter)\s*(?:of\s+)?,?\s*{_YEAR}", re.I), _quarter),
    ("half_year", re.compile(rf"\b(?:h([12])|(first|second|1st|2nd)\s+half)\s*(?:of\s+)?{_YEAR}", re.I), _half),
    ("early_mid_late", re.compile(rf"\b(early|beginning\s+of|mid|late|end\s+of)[-\s]+(?:of\s+)?{_YEAR}", re.I), _vague),
    ("year_only", re.compile(
        r"\b(?:open(?:ing|s)?|coming|deliver(?:ing|s|y)?|complet(?:e|es|ion)|available|ready|move[-\s]?ins?)\b"
        rf"[^.!?\d]{{0,20}}?{_YEAR}", re.I), _year_only),
]


# ---------------------------------------------------------------------------
# The public functions
# ---------------------------------------------------------------------------
def interpret(phrase: str | None, reference_date: date | None = None, *, student_housing: bool = False) -> Opening:
    """
    Read an opening phrase. `reference_date` is accepted for future use (phrases like "this fall");
    today it does not change the answer. Returns an Opening with a deadline (or None) and a note.
    """
    text = (phrase or "").strip()
    for name, pattern, build in RULES:
        m = pattern.search(text)
        if not m:
            continue
        try:
            deadline, year, note, academic = build(m)
        except (ValueError, KeyError):                 # e.g. "February 30, 2026"
            continue
        lease_up = date(year, *LEASE_UP) if (student_housing or academic) else None
        return Opening(text, deadline, lease_up, year, note, name)
    if re.search(r"coming\s+soon|pre-?leasing|opening\s+soon|stay\s+tuned", text, re.I):
        return Opening(text, None, None, None,
                       "No date is given ('coming soon'), so it cannot be called late on its own.")
    return Opening(text, None, None, None, "No opening date or year was found in the phrase.")


def to_deadline(phrase: str | None, reference_date: date | None = None) -> tuple[date | None, str]:
    """(deadline date or None, plain-English note). See interpret() for the lease-up date."""
    o = interpret(phrase, reference_date)
    return o.deadline, o.note
