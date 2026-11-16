"""collectors/market_rents.py  -  fill market_rents (the city rent index the rent-decline signal R
compares each property's asking rents against).

Today there is exactly one source: Zillow's published ZORI city file (a public CSV of the Zillow
Observed Rent Index per city per month). It is a MARKET benchmark only: it never writes anything
about a specific property, so a broad index can never be mistaken for a property's own rent.

What ZORI is (and is not): it tracks asking rents across the whole rental stock (single-family,
condos and apartments, smoothed and seasonally adjusted), NOT just new multifamily. The R signal only
compares each series' PERCENT CHANGE (property asking rents vs. the city index over the same window),
so the index level never mixes with a property's rent level - but a smoothed all-rentals index moves
slower and differently than new-apartment asking rents. Treat a small gap as noise; the signal's
full_gap setting in equation.yaml controls how big a gap counts.

Run it with:   python -m collectors.market_rents
It is also part of "run everything" (collectors/run_all.py).

Terms of use: like every other source, this collector runs only after a person has checked the
source's terms and set verified_terms: true in config/sources.yaml.
"""

from __future__ import annotations

import argparse
import csv
import io
from datetime import date

from collectors.common import config, db, fetch
from collectors.common.log import RunTracker, get_logger, setup_logging

log = get_logger("market_rents.py")
COLLECTOR = "market_rents.py"
SOURCE = "zillow_zori"


def sources(cfg: dict) -> list[dict]:
    """The configured market-rent sources (config/sources.yaml, market_rents: section)."""
    block = cfg.get("market_rents") or {}
    return [dict(s) for s in block.get("sources", []) if s.get("enabled", True)]


def parse_zori(text: str, months: int) -> list[tuple[str, str, date, float]]:
    """Turn the ZORI city CSV into (city, state, period, rent_index) rows for the last `months` months."""
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None) or []
    # Monthly columns are named like 2026-10-31; take the newest `months` of them.
    month_cols = [(i, h) for i, h in enumerate(header) if len(h) == 10 and h[4] == "-" and h[7] == "-"]
    month_cols = month_cols[-months:]
    try:
        name_i, state_i = header.index("RegionName"), header.index("State")
    except ValueError as e:
        raise ValueError(f"not the expected ZORI city CSV (missing RegionName/State column): {e}")
    rows = []
    for parts in reader:
        if len(parts) <= state_i:
            continue
        city, state = parts[name_i].strip(), parts[state_i].strip().upper()
        if not city or not state:
            continue
        for i, period in month_cols:
            if i < len(parts) and parts[i].strip():
                try:
                    rows.append((city, state, date.fromisoformat(period), float(parts[i])))
                except ValueError:
                    continue                    # a blank or odd cell must not stop the rest
    return rows


def collect(con, *, session=None) -> RunTracker:
    with RunTracker(con, COLLECTOR) as run:
        cfg = config.load_sources_config()
        months = int((cfg.get("market_rents") or {}).get("months", 30))
        wanted = sources(cfg)
        if not wanted:
            log.info("no market-rent sources configured (see market_rents: in config/sources.yaml)")
        for src in wanted:
            name = src.get("name") or src.get("url")
            if not src.get("verified_terms"):
                run.manual_inbox(name, "terms of use not confirmed by a person yet "
                                       "(set verified_terms: true in config/sources.yaml after checking them)")
                continue
            settings = config.domain_settings(fetch.urlparse(src["url"]).netloc)
            resp = fetch.polite_get(src["url"], settings, session=session)
            if resp is None:
                run.manual_inbox(name, f"could not download {src['url']}")
                continue
            rows = parse_zori(resp.text, months)
            con.execute("DELETE FROM market_rents WHERE source = ?", [SOURCE])   # full refresh: stays idempotent
            con.executemany("INSERT INTO market_rents (city, state, period, rent_index, source) "
                            "VALUES (?, ?, ?, ?, ?)", [(*r, SOURCE) for r in rows])
            run.new_file()
            log.info("%s: %s city-month rows from %s", COLLECTOR, len(rows), name)
    return run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fill market_rents from public rent indices (Zillow ZORI).")
    parser.parse_args(argv)
    setup_logging()
    con = db.connect()
    from scorer.equation import create_equation_tables
    create_equation_tables(con)
    collect(con)


if __name__ == "__main__":
    main()
