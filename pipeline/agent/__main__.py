"""
Run the sourcing agent from the command line:

    python -m agent "find what the public record says about the Willow Creek project"

The agent must be switched on (BLOOMFIELD_AGENT=1 in .env or enabled: true in config/agent.yaml)
and needs the LLM key in .env, like the reader. Every run is logged in agent_runs / agent_steps,
and every fact it relies on lands in the evidence table with its source address.
"""
from __future__ import annotations

import argparse
import json

from collectors.common import db
from collectors.common.log import setup_logging

from . import loop


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the sourcing agent on one goal.")
    ap.add_argument("goal", help="Plain-English goal, e.g. 'what does the public record say about <project>?'")
    args = ap.parse_args()
    setup_logging()
    con = db.connect()
    summary = loop.run(con, args.goal)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
