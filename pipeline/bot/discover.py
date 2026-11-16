"""bot/discover.py  -  the tool finds projects itself; nobody maintains a watchlist by hand.

(Not collectors/discover.py, which screens county websites. This one finds PROJECTS from permits.)

New construction permits are public the day they are issued. A fresh commercial/multifamily
BUILDING permit means a project exists months before it shows up on any broker list, so this
module turns those permits into rows in the `projects` table (no website yet). The opening scout
(bot/opening.py) then asks the search service for each project's own leasing page, saves the page,
and bot/rents.py reads asking rents and concessions off it.

Only permits that clearly describe a NEW building become projects (work class "New" and a
description that mentions units/apartments/etc.): a remodel or a reroof is not a project.
Everything here is structured data from the permit itself - no AI, nothing guessed.
"""

from __future__ import annotations

import argparse
import json
import re

from collectors.common import db
from collectors.common.log import RunTracker, get_logger, setup_logging
from scout_reader_schema import create_scout_reader_tables, upsert

log = get_logger("discover.py")
COLLECTOR = "discover.py"

NEW_BUILD = re.compile(r"\bnew\b", re.IGNORECASE)
PROJECT_WORDS = re.compile(r"\b(apartment|multi[- ]?family|residential units?|dwelling units?|"
                           r"mixed[- ]?use|senior (living|housing)|student housing|hotel|office building|"
                           r"self[- ]?storage|retail center)\b", re.IGNORECASE)
# Bloomfield sources COMMERCIAL properties: skip one-to-four-unit houses/condos, and require either a
# real unit count (5+) or "new construction" language, so a pool renovation at an apartment complex
# is not mistaken for a project.
SMALL_RESIDENTIAL = re.compile(r"\b(SFR|single[- ]?family|condo(minium)?|duplex|townhome|townhouse)\b", re.IGNORECASE)
TRADE_PERMIT = re.compile(r"\b(Electrical|Plumbing|Mechanical|HVAC|Irrigation|Fire( Alarm| Sprinkler)?)"
                          r"\s+Permit\b", re.IGNORECASE)
ACCESSORY = re.compile(r"\b(carport|garage|parking (garage|structure)|pool|spa|cabana|clubhouse|"
                       r"leasing office|club ?house|fence|shed|dumpster|sign|retaining wall|canopy|awning|deck|patio|"
                       r"demolition)\b", re.IGNORECASE)
NEW_CONSTRUCTION_WORDS = re.compile(r"new (construction|building|apartments?|multi[- ]?family|mixed[- ]?use|"
                                    r"office|hotel|self[- ]?storage|retail|senior|student)", re.IGNORECASE)
TYPE_HINTS = [("student housing", "student_housing"), ("senior", "senior_housing"), ("hotel", "hotel"),
              ("office", "office"), ("storage", "self_storage"), ("retail", "retail"),
              ("apartment", "multifamily"), ("multi", "multifamily"), ("dwelling", "multifamily"),
              ("condo", "multifamily"), ("mixed", "mixed_use")]


def guess_type(text: str) -> str | None:
    low = (text or "").lower()
    return next((t for needle, t in TYPE_HINTS if needle in low), None)


def project_rows(con) -> list[dict]:
    """New-build project permits not already in the projects table (via their permit number)."""
    cur = con.execute(
        "SELECT f.permit_number, f.property_address, f.property_city, f.property_state, f.file_id, f.filing_date "
        "FROM filing_fields f WHERE f.filing_type = 'building_permit' AND f.permit_number IS NOT NULL")
    out = []
    for num, address, city, state, file_id, filed in cur.fetchall():
        # permits.py names filing rows "<raw file_id>:permit:<number>"; the raw file is the part before.
        raw_id = file_id.split(":permit:", 1)[0]
        path = con.execute("SELECT file_path FROM raw_files WHERE file_id = ?", [raw_id]).fetchone()
        if not path:
            continue
        try:
            raw = json.loads(config_path(path[0]).read_text())
        except Exception:
            continue
        desc = f"{raw.get('description') or ''} {raw.get('permit_type') or ''} {raw.get('permit_type_desc') or ''}"
        work = f"{raw.get('work_class') or ''} {raw.get('permit_class') or ''}"
        units = raw.get("housing_units")
        unit_count = int(units) if str(units or "").isdigit() else 0
        if not (NEW_BUILD.search(work) and PROJECT_WORDS.search(desc)):
            continue
        if SMALL_RESIDENTIAL.search(desc):
            continue
        if TRADE_PERMIT.search(desc):
            continue                       # an electrical/plumbing permit for the project is not the project
        if ACCESSORY.search(desc) and unit_count < 5:
            continue                       # "new carport for the apartments" is not the project
        if unit_count < 5 and not NEW_CONSTRUCTION_WORDS.search(desc):
            continue
        out.append({"permit_number": num, "address": address, "city": city, "state": state,
                    "filing_date": filed, "description": desc.strip(), "units": unit_count or None,
                    "property_type": guess_type(desc)})
    # One development pulls many permits (one per building). Keep the biggest permit per address.
    best: dict[tuple, dict] = {}
    for r in out:
        key = ((r["address"] or r["permit_number"]).strip().upper(), (r["city"] or "").strip().upper())
        if key not in best or (r["units"] or 0) > (best[key]["units"] or 0):
            best[key] = r
    return list(best.values())


def config_path(stored: str):
    from collectors.common import config
    return config.from_stored_path(stored)


def discover(con) -> RunTracker:
    with RunTracker(con, COLLECTOR) as run:
        create_scout_reader_tables(con)
        for r in project_rows(con):
            pid = f"permit:{r['permit_number']}"
            if con.execute("SELECT 1 FROM projects WHERE project_id = ?", [pid]).fetchone():
                run.skipped()
                continue
            name = f"{r['address']} ({r['city']})" if r["address"] else f"Permit {r['permit_number']}"
            upsert(con, "projects", {
                "project_id": pid, "project_name": name, "property_address": r["address"],
                "city": r["city"], "state": r["state"], "property_type": r["property_type"],
                "units_or_beds": int(r["units"]) if str(r["units"] or "").isdigit() else None,
                "first_seen_at": db.utc_now(), "check_status": "ok",
                "notes": f"Found from building permit {r['permit_number']} issued {r['filing_date']}: "
                         f"{r['description'][:200]}"})
            run.new_file()
        log.info("%s: %d new projects from permits", COLLECTOR, run.files_new)
    return run


def main(argv=None):
    argparse.ArgumentParser(description="Turn new-build permits into tracked projects (no watchlist needed).").parse_args(argv)
    setup_logging()
    discover(db.connect())


if __name__ == "__main__":
    main()
