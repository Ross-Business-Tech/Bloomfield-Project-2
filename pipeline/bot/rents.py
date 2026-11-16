"""bot/rents.py  -  read asking rents and concessions off the leasing pages the bot already saved,
and write them to rent_observations (the table the rent-decline signal R and the concession-creep
signal K read).

Rules that keep this honest:
  * Only the project's OWN page (source "project_site"), never an aggregator.
  * asking_rent = the LOWEST rent-looking price on the page ("from $1,450", "$1,450/mo", ...),
    matching the table's definition ("lowest advertised rent").
  * A concession is only recorded when the page literally says free rent ("1 month free",
    "6 weeks free"). Look-and-lease language without free rent is not a concession.
  * No price on the page means NO row - never a guess. The exact page stays linked by file_id,
    so every number can be read in context.

Run it with:   python -m bot.rents        (usually via bot/run.py's weekly flow)
"""

from __future__ import annotations

import argparse
import re
from datetime import date

from collectors.common import config, db
from collectors.common.log import RunTracker, get_logger, setup_logging

log = get_logger("rents.py")
COLLECTOR = "rents.py"

PRICE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{3,5})(?=\s*(?:/|per\s|mo\b|month|/mo|\+|\s|$|[.,]))",
                   re.IGNORECASE)
RENT_CONTEXT = re.compile(r"rent|/mo|per month|bedroom|studio|/bed|starting|from \$", re.IGNORECASE)
FREE = re.compile(r"(\d+|one|two|three|four|five|six)\s*(month|week)s?\s+(of\s+)?free\s*(rent)?", re.IGNORECASE)
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def page_text(html: str) -> str:
    from bot.opening import page_text as _pt
    return _pt(html)


def extract_rent(text: str) -> float | None:
    """Lowest rent-looking price on the page, or None. Prices without rent context are ignored."""
    best = None
    for m in PRICE.finditer(text):
        amount = float(m.group(1).replace(",", ""))
        window = text[max(0, m.start() - 120):m.end() + 120]
        if not RENT_CONTEXT.search(window):
            continue
        if not (300 <= amount <= 25000):             # not a plausible monthly asking rent
            continue
        best = amount if best is None else min(best, amount)
    return best


def extract_concession_months(text: str) -> float | None:
    """Free-rent months in the biggest advertised concession, or None."""
    best = None
    for m in FREE.finditer(text):
        n = m.group(1).lower()
        value = float(WORDS.get(n, n))
        months = value if m.group(2).lower().startswith("month") else value * 7 / 30.44
        best = months if best is None else max(best, months)
    return best


def collect(con, *, today: date | None = None) -> RunTracker:
    from scorer.equation import create_equation_tables
    with RunTracker(con, COLLECTOR) as run:
        create_equation_tables(con)
        today = today or date.today()
        rows = con.execute(
            "SELECT r.file_id, r.file_path, r.url, p.project_id FROM raw_files r "
            "JOIN projects p ON p.main_url = r.url WHERE r.source = 'project_site' "
            "AND lower(coalesce(r.file_type, '')) IN ('html', 'htm', '')").fetchall()
        for file_id, file_path, url, pid in rows:
            if db.get_state(con, COLLECTOR, f"read:{file_id}"):
                run.skipped()
                continue
            try:
                text = page_text(config.from_stored_path(file_path).read_text(errors="ignore"))
            except Exception as e:
                run.error(f"{file_id}: {e}")
                continue
            rent = extract_rent(text)
            concession = extract_concession_months(text)
            if rent is None and concession is None:
                db.set_state(con, COLLECTOR, f"read:{file_id}", "nothing rent-like on the page")
                run.skipped()
                continue
            con.execute("INSERT INTO rent_observations (project_id, observed_at, asking_rent, concession_months, "
                        "lease_term_months, unit_basis, source_url, file_id) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
                        [pid, today, rent, concession, "unit", url, file_id])
            db.set_state(con, COLLECTOR, f"read:{file_id}",
                         f"rent={rent} concession_months={concession}")
            run.new_file()
        log.info("%s: %d pages gave rent/concession readings", COLLECTOR, run.files_new)
    return run


def main(argv=None):
    argparse.ArgumentParser(description="Read asking rents and concessions from saved leasing pages.").parse_args(argv)
    setup_logging()
    collect(db.connect())


if __name__ == "__main__":
    main()
