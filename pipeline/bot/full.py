"""
full.py - the whole weekly run in one command, with the AI doing the reading.

    python -m bot --full                    # crawl, read with the AI, link, score
    python -m bot --full --signals opening_date,distress_news
    python -m bot --full --no-ai            # crawl and score without the AI (keyword reading only)

Order:
  1. Crawl:    the bot collects pages and records (project sites, news, county, bank and SEC collectors). Every page goes
               through robots.txt, the polite pace and the stop-at-login/CAPTCHA rules.
  2. Read:     the AI reader (OpenAI, key in OPENAI_API_KEY) turns each saved page or document into fields with an exact
               quote; quotes are checked against the document, and anything doubtful goes to a person.
  3. Signals:  project pages become opening-date updates; news items and notices become distress filings.
  4. Link:     the matcher joins records about the same property, loan, lender and company.
  5. Score:    the Opportunity Score equation ranks the properties. The score itself is plain arithmetic on the evidence so a
               researcher can always see why a property ranked where it did; the AI never invents a score.
Without a key, step 2 is skipped with a clear message and the rest still runs.
"""
from __future__ import annotations

import argparse
from datetime import date

from collectors.common.log import get_logger, setup_logging
from llm import client
from llm.settings import load_reader_config, provider_name
from matcher.run import load_matching_config, run_matcher
from matcher.schema import create_matcher_tables
from scorer import weekly
from scorer.schema import create_scorer_tables
from scout_reader_schema import open_db

from . import discover, opening, rents
from .run import load_config, run as run_bot

log = get_logger("bot.full")


def ai_ready(cfg: dict | None = None) -> tuple[bool, str]:
    """Is an AI key available for the chosen provider? (Does not call the AI.)"""
    cfg = cfg or load_reader_config()
    provider = provider_name(cfg)
    try:
        client.get_api_key(provider)
    except client.LLMConfigError:
        names = client.KEY_NAMES.get(provider, ["LLM_API_KEY"])
        return False, f"no AI key: add {names[0]} to pipeline/.env"
    return True, provider


def run_full(con, bot_cfg: dict | None = None, signals: list[str] | None = None, today: date | None = None,
             session=None, use_ai: bool = True, reader_run=None) -> dict:
    today = today or date.today()
    bot_cfg = bot_cfg or load_config()
    create_matcher_tables(con)                  # the shared database needs every part's tables before the run
    create_scorer_tables(con)
    out: dict = {}
    ready, why = ai_ready()
    ai = bool(use_ai and ready)
    out["ai"] = {"on": ai, "why": why if ready else why} if use_ai else {"on": False, "why": "switched off with --no-ai"}

    disc = discover.discover(con)                       # new-build permits -> tracked projects (no watchlist needed)
    out["discover"] = {"new_projects": disc.files_new, "errors": disc.errors[:3]}
    out["crawl"] = run_bot(con, bot_cfg, signals, today, session, defer_to_ai=ai)
    rent_run = rents.collect(con, today=today)          # saved leasing pages -> rent_observations (R + K signals)
    out["rents"] = {"pages_with_readings": rent_run.files_new, "errors": rent_run.errors[:3]}
    if ai:
        if reader_run is None:
            from reader import batch
            reader_run = batch.run
        out["read"] = reader_run(con=con, progress=False, quiet=True)
        out["projects"] = opening.apply_readings(con, today)
    else:
        out["read"] = {"skipped": out["ai"]["why"]}
    out["link"] = run_matcher(con, load_matching_config(), today)
    out["run_id"] = weekly.run(con, None, today)
    out["filters"] = weekly.filter_summary(con, out["run_id"])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot --full", description="Crawl, read with the AI, link and score.")
    ap.add_argument("--signals", help="comma-separated; default is every enabled signal in config/bot.yaml")
    ap.add_argument("--no-ai", action="store_true", help="skip the AI reader (keyword reading only)")
    args = ap.parse_args(argv)
    setup_logging()
    con = open_db()
    res = run_full(con, signals=[s.strip() for s in args.signals.split(",")] if args.signals else None, use_ai=not args.no_ai)
    print(f"AI: {'ON (' + res['ai']['why'] + ')' if res['ai']['on'] else 'OFF: ' + res['ai']['why']}")
    for step in ("crawl", "read", "projects", "link"):
        if step in res:
            print(f"{step}: {res[step]}")
    fs = res["filters"]
    print(f"scored: {res['run_id']}: {fs['shown']} properties listed, {fs['hidden']} hidden by filters")
    print("Open the dashboard:  streamlit run dashboard/app.py")
    return 0
