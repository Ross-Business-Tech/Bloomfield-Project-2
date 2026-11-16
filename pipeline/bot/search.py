"""
search.py - ways the bot finds pages. It only ever finds web addresses; every page is then opened through
collectors/common/fetch.py (robots.txt, polite pace, stops at logins and CAPTCHAs).

  * brave  : the Brave Search API (a licensed search service; needs BRAVE_SEARCH_API_KEY). We never scrape search engines.
  * tavily : the Tavily search API (built for AI agents; needs TAVILY_API_KEY). Free plan: 1,000
             searches a month, no card, and it cannot charge unless pay-as-you-go is switched on.
  * none   : no open-web search; the bot uses only the project sites and news feeds listed in config/bot.yaml.
"""
from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import requests

from collectors.common import config
from collectors.common.log import get_logger

log = get_logger("bot.search")
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
TAVILY_URL = "https://api.tavily.com/search"


class Budget:
    """Shared page and query counters so one run can never go past its limits."""

    def __init__(self, cfg: dict):
        lim = cfg.get("limits") or {}
        self.pages_left = int(lim.get("max_pages_per_run", 60))
        self.queries_left = int(lim.get("max_search_queries", 20))

    def take_page(self) -> bool:
        if self.pages_left <= 0:
            return False
        self.pages_left -= 1
        return True

    def take_query(self) -> bool:
        if self.queries_left <= 0:
            return False
        self.queries_left -= 1
        return True


def allowed_domain(url: str, cfg: dict) -> bool:
    host = urlparse(url).netloc.lower()
    skip = ((cfg.get("search") or {}).get("skip_domains")) or []
    return not any(host == d or host.endswith("." + d) for d in skip)


def search(query: str, cfg: dict, budget: Budget, session=None) -> list[dict]:
    """[{title, url, snippet}] for a query, or [] when no search service is on."""
    s_cfg = cfg.get("search") or {}
    provider = s_cfg.get("provider", "none")
    if provider == "tavily":
        return _tavily(query, s_cfg.get("tavily") or {}, cfg, budget, session)
    if provider != "brave":
        return []
    config.load_env()
    b = s_cfg.get("brave") or {}
    key = os.environ.get(b.get("api_key_env", "BRAVE_SEARCH_API_KEY"), "").strip()
    if not key:
        log.info("no search key (%s): skipping the open-web search", b.get("api_key_env", "BRAVE_SEARCH_API_KEY"))
        return []
    if not budget.take_query():
        log.info("search query limit reached")
        return []
    getter = (session or requests).get
    try:
        resp = getter(BRAVE_URL, params={"q": query, "count": int(b.get("results_per_query", 5))},
                      headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=30)
    except requests.RequestException as e:
        log.warning("search failed: %s", e)
        return []
    if resp.status_code != 200:
        log.warning("search returned HTTP %s", resp.status_code)
        return []
    time.sleep(0 if session is not None else 1)                       # the service allows about one query a second
    out = []
    for r in (resp.json().get("web") or {}).get("results") or []:
        if r.get("url") and allowed_domain(r["url"], cfg):
            out.append({"title": r.get("title") or "", "url": r["url"], "snippet": r.get("description") or ""})
    return out


def _tavily(query: str, t_cfg: dict, cfg: dict, budget: Budget, session=None) -> list[dict]:
    """Tavily search (POST /search). Key from TAVILY_API_KEY (or api_key_env in config/bot.yaml)."""
    config.load_env()
    key = os.environ.get(t_cfg.get("api_key_env", "TAVILY_API_KEY"), "").strip()
    if not key:
        log.info("no search key (%s): skipping the open-web search", t_cfg.get("api_key_env", "TAVILY_API_KEY"))
        return []
    if not budget.take_query():
        log.info("search query limit reached")
        return []
    poster = (session or requests).post
    try:
        resp = poster(TAVILY_URL, json={"query": query, "max_results": int(t_cfg.get("results_per_query", 5)),
                                        "search_depth": "basic"},
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=30)
    except requests.RequestException as e:
        log.warning("search failed: %s", e)
        return []
    if resp.status_code != 200:
        log.warning("search returned HTTP %s", resp.status_code)
        return []
    time.sleep(0 if session is not None else 1)
    out = []
    for r in resp.json().get("results") or []:
        if r.get("url") and allowed_domain(r["url"], cfg):
            out.append({"title": r.get("title") or "", "url": r["url"], "snippet": r.get("content") or ""})
    return out
