"""
load_county_registry.py - fill the county_registry table with every U.S. county.

Source: the Census Bureau's official county list (about 3,140 counties + DC).
Every county starts as "not_started". A county becomes "configured" or
"manual_inbox" when its YAML file in config/counties/ says so (this script
syncs that every time you run it).

Run from the pipeline/ folder:
    python scripts/load_county_registry.py                 # download from census.gov
    python scripts/load_county_registry.py --file county_list.txt   # use a saved copy
    python scripts/load_county_registry.py --include-territories    # also PR, GU, VI, AS, MP
"""
import argparse
import csv
import io
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.common import config, db, fetch  # noqa: E402
from collectors.common.log import setup_logging  # noqa: E402

CENSUS_URL = "https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt"
TERRITORIES = {"PR", "GU", "VI", "AS", "MP", "UM"}


def parse_census_text(text: str, include_territories: bool = False) -> list[dict]:
    """
    Census file columns (pipe-separated):
    STATE|STATEFP|COUNTYFP|COUNTYNS|COUNTYNAME|CLASSFP|FUNCSTAT
    """
    rows = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter="|")
    for r in reader:
        state = r["STATE"].strip()
        if state in TERRITORIES and not include_territories:
            continue
        rows.append({
            "state": state,
            "county": r["COUNTYNAME"].strip(),
            "fips": r["STATEFP"].strip().zfill(2) + r["COUNTYFP"].strip().zfill(3),
        })
    return rows


def load_registry(con, rows: list[dict]) -> int:
    """Add counties we don't have yet. Existing rows (and their status) are never overwritten."""
    added = 0
    for r in rows:
        if db.insert_if_new(con, "county_registry", {
            **r, "config_status": "not_started", "priority": None, "notes": None,
        }, ["fips"]):
            added += 1
    return added


def sync_from_yaml(con, counties_dir: Path | None = None) -> int:
    """
    Read config/counties/*.yaml. For each county file, set that county's
    config_status ("configured" or "manual_inbox" - from the file's `status:`)
    and `priority`. Statewide files (no fips) are skipped here.
    """
    counties_dir = counties_dir or (config.CONFIG_DIR / "counties")
    updated = 0
    for f in sorted(counties_dir.glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        fips = str(data.get("fips") or "").zfill(5) if data.get("fips") else None
        if not fips:
            continue
        status = data.get("status", "configured")
        if status not in ("configured", "manual_inbox", "not_started"):
            print(f"  warning: {f.name} has unknown status {status!r}; skipped")
            continue
        found = con.execute("SELECT 1 FROM county_registry WHERE fips = ?", [fips]).fetchone()
        if not found:
            print(f"  warning: {f.name} has FIPS {fips}, which is not in the registry")
            continue
        con.execute("UPDATE county_registry SET config_status = ?, priority = ?, notes = ? WHERE fips = ?",
                    [status, data.get("priority"), data.get("notes"), fips])
        updated += 1
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Load all U.S. counties into county_registry.")
    parser.add_argument("--file", help="use this saved copy of the Census list instead of downloading")
    parser.add_argument("--include-territories", action="store_true")
    parser.add_argument("--db")
    args = parser.parse_args()
    setup_logging(to_file=False)

    con = db.connect(args.db)
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        resp = fetch.polite_get(CENSUS_URL, config.domain_settings("www2.census.gov"))
        if resp is None:
            sys.exit("Could not download the Census list. Download it in your browser from\n"
                     f"  {CENSUS_URL}\nsave it, and re-run with --file <that file>.")
        text = resp.text

    rows = parse_census_text(text, args.include_territories)
    added = load_registry(con, rows)
    synced = sync_from_yaml(con)
    total = con.execute("SELECT count(*) FROM county_registry").fetchone()[0]
    print(f"County registry: {added} added, {total} total, {synced} synced from YAML configs.")
    for status, n in con.execute(
            "SELECT config_status, count(*) FROM county_registry GROUP BY 1 ORDER BY 1").fetchall():
        print(f"  {status:<14} {n}")


if __name__ == "__main__":
    main()
