"""
run_all.py - runs every collector in order (or just the ones you name) and prints a summary.

In plain English: this is the "weekly run" button. Order: inbox, trepp, edgar, ffiec, county,
recorded_loans, liens, permits, state. One collector failing never stops the others (it is logged
and the next one runs). At the end you get a table: per collector, new files, skipped duplicates,
errors, and which sources fell back to the manual inbox.

Run from the pipeline/ folder:
    python -m collectors.run_all                                   # everything
    python -m collectors.run_all --only county --county mi_washtenaw
    python -m collectors.run_all --only trepp,ffiec                # several, comma separated
    python -m collectors.run_all --state MI                        # one state: inbox + county only
    python -m collectors.run_all --since 2026-10-01

--state skips the national collectors (trepp, edgar, ffiec, state/CMS), which always cover every state.
recorded_loans, liens and permits are not built yet and are listed as "not built".
"""
from __future__ import annotations

import argparse
from datetime import date

from collectors import county, edgar, ffiec, state, trepp
from collectors.common import db, inbox
from collectors.common.log import RunTracker, get_logger, setup_logging

log = get_logger("run_all")

ORDER = ["inbox", "trepp", "edgar", "ffiec", "county", "recorded_loans", "liens", "permits", "state"]
NATIONAL = {"trepp", "edgar", "ffiec", "state"}          # always every state: skipped by --state
NOT_BUILT = {"recorded_loans", "liens", "permits"}


def _inbox(con, a):
    with RunTracker(con, "inbox.py") as run:
        inbox.load_inbox(con, run, only_state=a.state)
    return run


RUNNERS = {
    "inbox": _inbox,
    "trepp": lambda con, a: trepp.collect(con),
    "edgar": lambda con, a: edgar.collect(con, since=a.since),
    "ffiec": lambda con, a: ffiec.collect(con),
    "county": lambda con, a: county.collect(con, state=a.state, county=a.county, since=a.since),
    "state": lambda con, a: state.collect(con),
}


def run_all(con, *, only: list[str] | None = None, state_code: str | None = None, county_id: str | None = None,
            since: date | None = None) -> list[dict]:
    """Run the chosen collectors. Returns one summary dict per collector (also printed by main())."""
    args = argparse.Namespace(state=state_code, county=county_id, since=since)
    unknown = [n for n in (only or []) if n not in ORDER]
    if unknown:
        raise ValueError(f"unknown collector(s): {', '.join(unknown)}. Choose from: {', '.join(ORDER)}")
    results = []
    for name in ORDER:
        if only and name not in only:
            continue
        if not only and state_code and name in NATIONAL:
            continue
        if name in NOT_BUILT:
            results.append({"collector": name, "status": "not built", "new": 0, "skipped": 0, "errors": 0,
                            "fallbacks": [], "messages": []})
            continue
        try:
            run = RUNNERS[name](con, args)
            results.append({"collector": name, "status": run.status, "new": run.files_new,
                            "skipped": run.files_skipped, "errors": len(run.errors), "fallbacks": run.fallbacks,
                            "messages": run.errors[:3]})
        except Exception as e:                           # one collector crashing must never stop the others
            log.error("%s crashed: %s", name, e)
            results.append({"collector": name, "status": "failed", "new": 0, "skipped": 0, "errors": 1,
                            "fallbacks": [], "messages": [f"crashed: {e}"]})
    return results


def print_summary(results: list[dict]) -> None:
    print("\nCollector summary")
    print(f"  {'collector':<16}{'status':<11}{'new files':>10}{'skipped':>9}{'errors':>8}")
    for r in results:
        print(f"  {r['collector']:<16}{r['status']:<11}{r['new']:>10}{r['skipped']:>9}{r['errors']:>8}")
    problems = [(r["collector"], m) for r in results for m in r["messages"]]
    if problems:
        print("\n  First errors:")
        for name, msg in problems:
            print(f"    [{name}] {msg[:200]}")
    falls = [(r["collector"], s, why) for r in results for s, why in r["fallbacks"]]
    print("\n  Sources that fell back to the manual inbox:" + ("" if falls else " none"))
    for name, source, why in falls:
        print(f"    [{name}] {source}: {why[:160]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the public records collectors.")
    parser.add_argument("--only", help="collector name(s), comma separated: " + ", ".join(ORDER))
    parser.add_argument("--county", help="one county config (e.g. mi_washtenaw); with --only county")
    parser.add_argument("--state", help="one state (2-letter code); skips the national collectors")
    parser.add_argument("--since", type=date.fromisoformat, help="look back to this date (YYYY-MM-DD)")
    args = parser.parse_args()
    setup_logging()
    con = db.connect()
    only = [n.strip() for n in args.only.split(",")] if args.only else None
    try:
        results = run_all(con, only=only, state_code=args.state, county_id=args.county, since=args.since)
    except ValueError as e:
        raise SystemExit(str(e))
    print_summary(results)


if __name__ == "__main__":
    main()
