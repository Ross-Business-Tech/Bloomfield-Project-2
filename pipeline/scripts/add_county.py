"""
add_county.py - creates a starter YAML settings file for a county, using the county registry.

In plain English: adding a county to the tool means adding one YAML file in config/counties/. This
script writes a safe starter file (everything set to manual inbox) with the county's name, state and
FIPS code filled in from the registry, so you only have to add what you learn about its websites.

Run from the pipeline/ folder (the registry must be loaded first: scripts/load_county_registry.py):
    python scripts/add_county.py 26161                       # by 5-digit FIPS code
    python scripts/add_county.py --state MI --county Washtenaw
    python scripts/add_county.py 26161 --force               # overwrite an existing file
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.common import config, db  # noqa: E402
from collectors.common.files import county_slug  # noqa: E402

STARTER = """\
# {county}, {state}. Starter file made by scripts/add_county.py: everything is manual_inbox until a
# person checks the county's websites and terms of use. See config/counties/_template.yaml for how to
# turn a source into scrape / open_data_api. Then set status: configured.
county: {county}
state: {state}
fips: "{fips}"
status: manual_inbox
priority: 5
notes: "TODO: who checked the sites, when, and anything odd (login, CAPTCHA, robots.txt)."

sources:
  court:
    access: manual_inbox
    notes: "TODO: name the court case search site for this county."
  recorder:
    access: manual_inbox
    notes: "TODO: name the recorder / register of deeds search site."
  public_notice:
    access: manual_inbox
    notes: "TODO: name the legal / public notice site."
"""


def file_name(state: str, county: str) -> str:
    """('MI', 'Washtenaw County') -> 'mi_washtenaw.yaml'; ('FL', 'Miami-Dade County') -> 'fl_miami_dade.yaml'"""
    return f"{state.lower()}_{re.sub(r'-', '_', county_slug(county))}.yaml"


def add_county(con, fips: str | None, state: str | None, county: str | None,
               out_dir: Path | None = None, force: bool = False) -> Path:
    """Write the starter file and return its path. Raises ValueError with a plain message if it can't."""
    out_dir = out_dir or config.CONFIG_DIR / "counties"
    if fips:
        rows = con.execute("SELECT state, county, fips FROM county_registry WHERE fips = ?", [fips.zfill(5)]).fetchall()
    else:
        rows = [r for r in con.execute("SELECT state, county, fips FROM county_registry WHERE state = ?",
                                       [(state or "").upper()]).fetchall()
                if county_slug(r[1]) == county_slug(county or "")]
    if not rows:
        raise ValueError("county not found in the registry. Run scripts/load_county_registry.py first, "
                         "and check the FIPS code or the state and county name.")
    if len(rows) > 1:
        raise ValueError("more than one county matches: " + ", ".join(f"{r[1]} ({r[2]})" for r in rows)
                         + ". Use the FIPS code.")
    st, name, code = rows[0]
    path = out_dir / file_name(st, name)
    if path.exists() and not force:
        raise ValueError(f"{path.name} already exists (use --force to overwrite it)")
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(STARTER.format(county=name, state=st, fips=code))
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a starter county YAML from the county registry.")
    parser.add_argument("fips", nargs="?", help="5-digit FIPS code, e.g. 26161")
    parser.add_argument("--state")
    parser.add_argument("--county", help="county name, with or without the word County")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not args.fips and not (args.state and args.county):
        parser.error("give a FIPS code, or both --state and --county")
    try:
        path = add_county(db.connect(), args.fips, args.state, args.county, force=args.force)
    except ValueError as e:
        sys.exit(f"Could not add the county: {e}")
    print(f"Created {path}")
    print("Next: open it, fill in what you learn about the county's websites, and run\n"
          "  python scripts/load_county_registry.py     (this updates the county's status in the registry)")


if __name__ == "__main__":
    main()
