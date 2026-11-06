"""One reusable scraper for county court, recorder, sheriff and public-notice sites.

    python -m collectors.county --since 2026-10-01           # every enabled county
    python -m collectors.county --only dallas_tx -v          # one county, chatty log

Everything about a county lives in config/counties.yaml; adding a county means
adding a YAML entry. This module only fetches and saves raw filings (PDF/HTML)
into raw_files with source = "COUNTY:<name>" and needs_reading = 1. Reading the
text is Part B's job.

Good-citizen rules built in: declared User-Agent, timeouts, a slow default rate
(1 request / 2 s per host), robots.txt is obeyed, and any page that asks for a
login or CAPTCHA stops that county (logged as skipped). We never try to get past
access controls.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from sqlalchemy import Engine

from collectors.common import (
    CONFIG_DIR,
    HttpClient,
    RunStats,
    download_file,
    get_engine,
    init_db,
    load_yaml,
    register_bytes,
    run_isolated,
    setup_logging,
)

CONFIG_PATH = CONFIG_DIR / "counties.yaml"
log = logging.getLogger("collectors.county")

_BLOCK_SIGNS = re.compile(
    r"""<input[^>]+type\s*=\s*["']?password|g-recaptcha|hcaptcha|cf-challenge|captcha""", re.IGNORECASE
)


class AccessBlocked(Exception):
    """The site wants a login/CAPTCHA, or robots.txt disallows us. We skip it."""


@dataclass
class Link:
    url: str
    text: str


def extract_links(html: str, page_url: str, link_pattern: str, link_text_pattern: str | None = None) -> list[Link]:
    """All unique links on the page whose absolute URL matches ``link_pattern``
    (and whose text matches ``link_text_pattern``, if given), in page order."""
    soup = BeautifulSoup(html, "html.parser")
    url_re = re.compile(link_pattern, re.IGNORECASE)
    text_re = re.compile(link_text_pattern, re.IGNORECASE) if link_text_pattern else None
    seen: set[str] = set()
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "#")):
            continue
        absolute = urljoin(page_url, href).split("#", 1)[0]
        text = " ".join(a.get_text(" ").split())
        if absolute in seen or not url_re.search(absolute) or (text_re and not text_re.search(text)):
            continue
        seen.add(absolute)
        out.append(Link(absolute, text))
    return out


def find_next_page(html: str, page_url: str, next_text_pattern: str | None) -> str | None:
    """URL of the 'next page' link, if the county config defines one."""
    if not next_text_pattern:
        return None
    soup = BeautifulSoup(html, "html.parser")
    pat = re.compile(next_text_pattern, re.IGNORECASE)
    for a in soup.find_all("a", href=True):
        if pat.search(" ".join(a.get_text(" ").split())) and not a["href"].startswith("javascript:"):
            return urljoin(page_url, a["href"])
    return None


def check_not_blocked(html: str, url: str) -> None:
    """Raise AccessBlocked if the page is a login or CAPTCHA wall."""
    if _BLOCK_SIGNS.search(html):
        raise AccessBlocked(f"{url} asks for a login or CAPTCHA; skipped (we do not bypass access controls)")


class RobotsCache:
    """Fetches each host's robots.txt once and answers can_fetch()."""

    def __init__(self, client: HttpClient) -> None:
        self.client = client
        self._parsers: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._parsers:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                resp = self.client.get(root + "/robots.txt")
                parser.parse(resp.text.splitlines())
            except Exception:  # noqa: BLE001 - no robots.txt means no restrictions
                parser = None
            self._parsers[root] = parser
        parser = self._parsers[root]
        return parser is None or parser.can_fetch(self.client.session.headers["User-Agent"], url)


def page_urls(county: dict[str, Any], since: date, until: date) -> list[str]:
    """Expand the county's pages; {since:%m/%d/%Y} style placeholders are filled in."""
    base = county.get("base_url", "")
    return [urljoin(base, p.format(since=since, until=until)) for p in county.get("pages", [])]


def scrape_county(client: HttpClient, engine: Engine, county: dict[str, Any], since: date, until: date,
                  stats: RunStats, robots: RobotsCache | None = None) -> None:
    """Fetch one county's pages and save every matching filing once."""
    robots = robots or RobotsCache(client)
    name, method = county["name"], county.get("fetch", "links")
    source, state = f"COUNTY:{name}", county["state"]
    today = date.today().isoformat()
    max_links = int(county.get("max_links", 200))
    saved = 0

    for start_url in page_urls(county, since, until):
        url: str | None = start_url
        for _page_no in range(int(county.get("max_pages", 5))):
            if url is None:
                break
            if not robots.allowed(url):
                raise AccessBlocked(f"robots.txt disallows {url}; skipped")
            if method == "direct":  # the configured URL is itself a filing (PDF/HTML)
                download_file(client, engine, url, source=source, state=state, date=today,
                              needs_reading=True, skip_if_url_seen=county.get("immutable", True), stats=stats)
                break
            resp = client.get(url)
            html = resp.text
            check_not_blocked(html, url)
            if method == "page":  # the page itself is the notice listing: save a snapshot
                stats.files_found += 1
                res = register_bytes(engine, resp.content, source=source, state=state, date=today, source_url=url,
                                     content_type=resp.headers.get("Content-Type"), ext=".html", needs_reading=True)
                if res.is_new:
                    stats.files_downloaded += 1
                else:
                    stats.files_skipped_duplicate += 1
            else:  # "links": save every linked filing that matches link_pattern
                for link in extract_links(html, url, county["link_pattern"], county.get("link_text_pattern")):
                    if saved >= max_links:
                        break
                    if not robots.allowed(link.url):
                        log.info("[%s] robots.txt disallows %s; skipped", name, link.url)
                        continue
                    run_isolated(stats, f"{name} {link.url}", lambda l=link: download_file(
                        client, engine, l.url, source=source, state=state, date=today, needs_reading=True,
                        skip_if_url_seen=county.get("immutable", True), stats=stats))
                    saved += 1
            url = find_next_page(html, url, county.get("next_text_pattern"))


def run(client: HttpClient, engine: Engine, since: date, until: date, *, only: list[str] | None = None,
        config_path: Path = CONFIG_PATH, stats: RunStats | None = None) -> RunStats:
    """Scrape every enabled county; a failing or blocked county is logged and skipped."""
    stats = stats or RunStats("county")
    robots = RobotsCache(client)
    for county in load_yaml(config_path)["counties"]:
        if only and county["name"] not in only:
            continue
        if not only and not county.get("enabled", True):
            log.info("[%s] disabled in config: %s", county["name"], county.get("notes", ""))
            continue
        run_isolated(stats, county["name"],
                     lambda c=county: scrape_county(client, engine, c, since, until, stats, robots))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Save county foreclosure/receivership filings to raw_files.")
    parser.add_argument("--since", type=date.fromisoformat, default=date.today() - timedelta(days=7))
    parser.add_argument("--until", type=date.fromisoformat, default=date.today())
    parser.add_argument("--only", action="append", help="just this county name (repeatable; runs it even if disabled)")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--db-url")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    engine = get_engine(args.db_url)
    init_db(engine)
    # County sites are small public servers: default to 1 request every 2 seconds per host.
    stats = run(HttpClient(default_rate=0.5), engine, args.since, args.until, only=args.only, config_path=args.config)
    stats.log_summary()
    return 1 if stats.errors and not stats.files_downloaded else 0


if __name__ == "__main__":
    sys.exit(main())
