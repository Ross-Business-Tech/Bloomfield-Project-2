"""
discovery.py - find the public-record sources for a property (the paid-API lane, OFF unless configured).

The method: the researcher does not guess web addresses. A licensed search service - the
project's EXISTING providers in bot/search.py (Tavily or Brave, configured in config/bot.yaml)
- finds candidate county/CMS/docket pages, and every query is recorded as a search row with
its exact wording and result count. Discovery produces CANDIDATE SOURCES for the collectors
and the manual inbox. It never fetches, reads, scores, or stores a fact: candidates go in
the search row's notes for a person or a screened collector to check.

Paid usage, exactly as the project already handles it, PLUS an explicit opt-in:
  * one query = one search-provider credit, counted by the shared bot/search.py Budget
  * the key comes from .env (TAVILY_API_KEY / BRAVE_SEARCH_API_KEY), never from the repo
  * PAID EXECUTION IS OFF BY DEFAULT: even with a provider and key configured, discovery
    refuses until BLOOMFIELD_RESEARCH_PAID=1 is set in the environment (or .env). A key
    existing is not consent to spend it.
  * with provider "none", no key, or no opt-in, the lane says so and stops - it never
    scrapes search engines, and tests inject a fake searcher instead of touching a network
  * AI reading of any page this finds stays with the existing Reader (pipeline/llm + the
    reader.yaml spending caps); this module makes no model calls of its own

Candidate sources are also registered in suggested_sources (status pending), the human
source-screening list from scout_reader_schema - discovery proposes, a person disposes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from bot import search as bot_search
from collectors.common import config
from collectors.common.log import get_logger
from collectors.common.parse import load_yaml

from . import evidence as ev
from .schema import create_research_tables

log = get_logger("research.discovery")
BOT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "bot.yaml"

# The source lanes discovery can look for, and how it asks. {name} {address} {city} {county}
# {state} are filled from the property. Ten-X is deliberately absent (deferred per Bloomfield).
DISCOVERY_QUERIES = {
    "county_tax": "{county} County {state} property tax lookup {address}",
    "recorder": "{county} County {state} recorder of deeds document search",
    "permits": "{city} {state} building permit search {address}",
    "court_docket": "\"{name}\" lawsuit",
    "cms": "\"{name}\" nursing facility CMS",
    "liens": "\"{name}\" tax lien",
}


def discover_sources(con, *, property_key: str | None = None, name: str | None = None,
                     address: str | None = None, city: str | None = None,
                     county: str | None = None, state: str | None = None,
                     lanes: list[str] | None = None, cfg: dict | None = None,
                     budget: bot_search.Budget | None = None, session=None,
                     searcher=None) -> dict:
    """
    Run the discovery queries for one property and record each one. Returns a summary:
    {"status": "ok" | "no_search_service", "queries": n, "candidates": {lane: [urls]}}.
    With no search service configured nothing is queried and nothing is invented.
    """
    create_research_tables(con)
    cfg = cfg if cfg is not None else load_yaml(BOT_CONFIG)
    s_cfg = cfg.get("search") or {}
    provider = s_cfg.get("provider", "none")
    if provider == "none":
        log.info("no search service configured (config/bot.yaml search.provider is 'none'); "
                 "discovery is off - candidate sources must come from the manual inbox")
        return {"status": "no_search_service", "queries": 0, "candidates": {}}
    if searcher is None:
        # The real provider is paid: it needs the explicit opt-in AND its key. With either
        # missing, say so instead of recording "searches" that never happened.
        config.load_env()
        if os.environ.get("BLOOMFIELD_RESEARCH_PAID", "").strip() != "1":
            log.info("paid execution is off (BLOOMFIELD_RESEARCH_PAID != 1); discovery "
                     "refuses to spend search credits until it is set")
            return {"status": "paid_disabled", "queries": 0, "candidates": {}}
        key_env = (s_cfg.get(provider) or {}).get("api_key_env",
                                                  f"{provider.upper()}_API_KEY")
        if not os.environ.get(key_env, "").strip():
            log.info("no %s key (%s); discovery is off until .env has it", provider, key_env)
            return {"status": "no_search_service", "queries": 0, "candidates": {}}
    searcher = searcher or bot_search.search
    budget = budget or bot_search.Budget(cfg)
    fields = {"name": name or "", "address": address or "", "city": city or "",
              "county": county or "", "state": state or ""}
    candidates: dict[str, list[str]] = {}
    lane_by_url: dict[str, list[str]] = {}
    queries = 0
    wanted = lanes or list(DISCOVERY_QUERIES)
    for lane in wanted:
        template = DISCOVERY_QUERIES.get(lane)
        if not template:
            continue
        query = " ".join(template.format(**fields).split())
        results = searcher(query, cfg, budget, session=session)
        queries += 1
        urls = [r["url"] for r in results if r.get("url")]
        candidates[lane] = urls
        ev.record_search(con, property_key=property_key, lane=lane, query=query,
                         result_count=len(urls),
                         notes=json.dumps({"candidates": urls[:10]}) if urls else None)
        for url in urls:
            lane_by_url.setdefault(url, []).append(lane)
    register_candidate_sources(con, lane_by_url)
    return {"status": "ok", "queries": queries, "candidates": candidates}


def register_candidate_sources(con, lane_by_url: dict[str, list[str]]) -> int:
    """Add candidate domains to suggested_sources (status pending) for a person to screen.
    Counts each domain once per discovery run, whatever lanes surfaced it.
    Returns how many NEW domains were proposed."""
    from urllib.parse import urlparse

    from collectors.common.db import utc_now
    from scout_reader_schema import create_scout_reader_tables
    create_scout_reader_tables(con)
    new = 0
    seen: set[str] = set()
    for url, lanes in lane_by_url.items():
        domain = urlparse(url).netloc.lower()
        if not domain or domain in seen:
            continue
        seen.add(domain)
        found = con.execute("SELECT times_seen FROM suggested_sources WHERE domain = ?",
                            [domain]).fetchone()
        if found:
            con.execute("UPDATE suggested_sources SET times_seen = times_seen + 1 WHERE domain = ?",
                        [domain])
        else:
            con.execute("INSERT INTO suggested_sources (domain, example_url, reason, "
                        "first_seen_at, times_seen, status) VALUES (?, ?, ?, ?, 1, 'pending')",
                        [domain, url, f"research discovery lanes: {', '.join(sorted(set(lanes)))}",
                         utc_now()])
            new += 1
    return new
