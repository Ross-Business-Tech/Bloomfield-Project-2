"""
parse.py - small helpers that turn messy text from files and websites into clean values.

In plain English: spreadsheets and web pages write dates, dollars, percentages and Yes/No in
many different ways ("07/01/2027", "$24,100,000.00", "4.75%", "Y", "1.25x"). These helpers
turn all of them into proper Python values, or None when the cell is blank or not understood.

(Adapted from Neel's collectors/common.py, which had the original versions.)
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d", "%m/%d/%y", "%Y%m%d",
                 "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y")
_NULLS = {"", "nan", "nat", "none", "null", "n/a", "na", "-"}


def load_yaml(path: str | Path) -> Any:
    """Read a YAML settings file."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clean_str(value: Any) -> str | None:
    """Trim a cell; blanks and NaN become None."""
    if value is None or (isinstance(value, float) and value != value):
        return None
    s = str(value).strip()
    return None if s.lower() in {"nan", "nat"} else (s or None)


def to_date(value: Any) -> date | None:
    """Many date styles -> a real date, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return None if value != value else value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):                      # pandas Timestamp
        try:
            return value.to_pydatetime().date()
        except Exception:                                    # NaT
            return None
    s = str(value).strip()
    if s.lower() in _NULLS:
        return None
    if re.match(r"\d{4}-\d{2}-\d{2}[T ]", s):
        s = s[:10]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def to_float(value: Any) -> float | None:
    """'1,234.5', '$1,000', '85%', '(12)', '1.25x' -> a number, or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if value != value else float(value)
    s = str(value).strip()
    if s.lower() in _NULLS:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[,$%()\s]", "", s)
    s = re.sub(r"[xX]$", "", s)                              # DSCR written as "1.25x"
    try:
        num = float(s)
    except ValueError:
        return None
    return -num if negative else num


def to_percent(value: Any) -> float | None:
    """'5.25', '5.25%' or 0.0525 -> 0.0525 (always a fraction)."""
    num = to_float(value)
    if num is None:
        return None
    return num / 100 if (abs(num) > 1.0 or "%" in str(value)) else num


def to_bool(value: Any) -> bool | None:
    """Y/N, Yes/No, true/false, 1/0 -> True/False (None if blank or unclear)."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in {"y", "yes", "true", "t", "1", "1.0"}:
        return True
    if s in {"n", "no", "false", "f", "0", "0.0"}:
        return False
    return None
