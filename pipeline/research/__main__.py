"""
Run from the pipeline/ folder:

    python -m research ingest    # rebuild evidence from stored CMS files, re-derive the gates
    python -m research report    # the honest counts: lead / partial / held / ready for review + unmet lanes
    python -m research discover --name "Fake Pines" --county Washtenaw --state MI --address "100 Fake St"
                                 # find candidate public-record sources (needs the search
                                 # service from config/bot.yaml; off without it)

Everything here reads stored files or records searches; the network and any paid call stay
in the project's existing collectors and providers (see README.md in this folder).
"""
from __future__ import annotations

import argparse

from collectors.common.log import setup_logging

from . import discovery, gates
from . import ingest as ingest_mod
from .schema import open_db


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="research", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="rebuild evidence from stored files, re-derive the gates")
    sub.add_parser("report", help="print the honest research-depth counts")
    disc = sub.add_parser("discover", help="find candidate public-record sources (paid search API, off unless configured)")
    disc.add_argument("--name")
    disc.add_argument("--address")
    disc.add_argument("--city")
    disc.add_argument("--county")
    disc.add_argument("--state")
    disc.add_argument("--property-key")
    args = parser.parse_args(argv)

    setup_logging()
    con = open_db()
    if args.command == "ingest":
        ingest_mod.ingest(con)
        print(gates.report(con))
    elif args.command == "report":
        print(gates.report(con))
    elif args.command == "discover":
        out = discovery.discover_sources(
            con, property_key=args.property_key, name=args.name, address=args.address,
            city=args.city, county=args.county, state=args.state)
        if out["status"] == "paid_disabled":
            print("Discovery is off: paid execution is disabled. Set BLOOMFIELD_RESEARCH_PAID=1 "
                  "to enable it - each query spends one search-provider credit.")
        elif out["status"] != "ok":
            print("Discovery is off: no search service is configured (config/bot.yaml).")
        else:
            print(f"{out['queries']} queries recorded.")
            for lane, urls in out["candidates"].items():
                print(f"  {lane}: {len(urls)} candidate(s)")
                for u in urls[:5]:
                    print(f"    {u}")


if __name__ == "__main__":
    main()
