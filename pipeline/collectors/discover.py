"""
discover.py - finds county record websites online and adds them to our list of CANDIDATE sources.

In plain English:
  1. it reads a public directory of county websites (settings: config/discovery.yaml; for example NETR Online),
     but only after a person has confirmed that directory's terms of use (terms_checked: true),
  2. for each county it collects the links to that county's recorder, court, public-notice and assessor sites,
  3. it SCREENS every link with the same polite fetcher the other collectors use: robots.txt must allow the
     page, there must be no login or CAPTCHA wall, and the site's terms page must not mention automated
     access (if it does, or if we cannot find the terms, a person decides),
  4. it saves the results in the website_candidates table and can write a PROPOSED county settings file.

It never solves a CAPTCHA, never logs in, and never switches a county to automatic by itself: a screened
site is only a candidate until a person reads the terms and adds the selectors (see config/counties/_template.yaml).

Run from the pipeline/ folder:
    python -m collectors.discover --state MI --county "Washtenaw County"
    python -m collectors.discover --state MI --limit 10 --screen --proposals
    python -m collectors.discover --report                 # what has been found so far
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml
from bs4 import BeautifulSoup

from collectors.common import config, db, fetch
from collectors.common.log import get_logger, setup_logging
from collectors.common.parse import load_yaml

log = get_logger("discover")

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "discovery.yaml"
PROPOSAL_DIR = Path(__file__).resolve().parents[1] / "config" / "counties" / "proposed"

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS website_candidates (
    url            TEXT PRIMARY KEY,
    state          TEXT,
    county         TEXT,
    kind           TEXT,        -- recorder / court / public_notice / assessor / other
    link_text      TEXT,
    found_on       TEXT,        -- the directory page the link came from
    directory      TEXT,
    screen_status  TEXT,        -- not_screened / allowed_candidate / needs_person / manual_inbox
    screen_reason  TEXT,
    found_at       TIMESTAMP,
    screened_at    TIMESTAMP
);
"""


class DirectoryNotApproved(Exception):
    """A person has not yet confirmed the directory's terms of use."""


def load_config(path: str | Path | None = None) -> dict:
    return load_yaml(path or CONFIG_PATH) or {}


def ensure_table(con) -> None:
    con.execute(TABLE_SQL)


def county_slug(name: str) -> str:
    return re.sub(r"\s+", "_", re.sub(r"\s+county$", "", (name or "").strip().lower(), flags=re.I))


# ---------------------------------------------------------------------------
# Reading a directory page
# ---------------------------------------------------------------------------
def classify(text: str, url: str, rules: dict) -> str:
    hay = f"{text} {url}".lower()
    for kind, words in rules.items():
        if any(w in hay for w in words):
            return kind
    return "other"


def parse_links(html: str, page_url: str, skip_domains: list[str]) -> list[tuple[str, str]]:
    """Links on a directory page that lead to some other website (the county's own sites)."""
    seen, out = set(), []
    for a in BeautifulSoup(html, "html.parser").select("a[href]"):
        href = urljoin(page_url, a["href"].strip())
        p = urlparse(href)
        host = p.netloc.lower()
        if p.scheme not in ("http", "https") or not host:
            continue
        if any(host == d or host.endswith("." + d) for d in skip_domains):
            continue
        key = href.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append((" ".join(a.get_text(" ").split()), href))
    return out


def _directory(cfg: dict, name: str) -> dict:
    d = (cfg.get("directories") or {}).get(name)
    if not d or not d.get("enabled", True):
        raise KeyError(f"directory {name!r} is not enabled in discovery.yaml")
    if not d.get("terms_checked"):
        raise DirectoryNotApproved(
            f"{name}: a person must read the directory's terms of use and set terms_checked: true in "
            f"config/discovery.yaml first. {d.get('terms_note', '')}")
    return d


def discover_county(con, cfg: dict, directory: str, state: str, county: str, session=None) -> list[dict]:
    """Find the county's website links in the directory and save them as candidates."""
    d = _directory(cfg, directory)
    url = d["county_url"].format(state=state, county_slug=county_slug(county))
    resp = fetch.polite_get(url, config.domain_settings(urlparse(url).netloc), session=session)
    if resp is None:
        log.warning("could not read %s (blocked, disallowed or down); use the manual route for %s", url, county)
        return []
    rows = []
    for text, link in parse_links(resp.text, url, d.get("skip_domains", [])):
        row = {"url": link, "state": state, "county": county, "kind": classify(text, link, cfg.get("classify", {})),
               "link_text": text[:200], "found_on": url, "directory": directory}
        if con.execute("SELECT 1 FROM website_candidates WHERE url = ?", [link]).fetchone():
            continue
        con.execute("INSERT INTO website_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [link, state, county, row["kind"], row["link_text"], url, directory,
                     "not_screened", None, db.utc_now(), None])
        rows.append(row)
    log.info("%s, %s: %d new candidate sites", county, state, len(rows))
    return rows


# ---------------------------------------------------------------------------
# Screening a candidate site (never gets around anything)
# ---------------------------------------------------------------------------
def screen_site(url: str, cfg: dict, session=None) -> tuple[str, str]:
    """Return (status, reason). allowed_candidate only means 'passed the automatic screens'."""
    domain = urlparse(url).netloc.lower()
    settings = config.domain_settings(domain)
    resp = fetch.polite_get(url, settings, session=session)
    if resp is None:
        if fetch.needs_manual_inbox(domain):
            return "manual_inbox", fetch.get_fallbacks().get(domain, "access denied, login or CAPTCHA")
        if (urlparse(url).path or "/") in fetch.get_robots_skips().get(domain, set()):
            return "manual_inbox", "robots.txt disallows this page"
        return "needs_person", "could not be read (down, error or unreachable)"
    soup = BeautifulSoup(resp.text, "html.parser")
    terms = next((urljoin(url, a["href"]) for a in soup.select("a[href]")
                  if re.search(r"terms|conditions|use policy|disclaimer", a.get_text(" "), re.I)), None)
    if not terms:
        return "needs_person", "no terms of use page found; a person must check"
    t = fetch.polite_get(terms, settings, session=session)
    if t is None:
        return "needs_person", "terms page could not be read; a person must check"
    text = BeautifulSoup(t.text, "html.parser").get_text(" ").lower()
    hits = [w for w in cfg.get("terms_red_flags", []) if w in text]
    if hits:
        return "needs_person", "terms mention: " + ", ".join(hits) + " (a person must read them)"
    return "allowed_candidate", "passed robots.txt, no login/CAPTCHA, no red-flag words in terms; a person still confirms"


def screen_pending(con, cfg: dict, limit: int | None = None, session=None) -> dict:
    counts: dict[str, int] = {}
    rows = con.execute("SELECT url FROM website_candidates WHERE screen_status = 'not_screened' "
                       "AND kind != 'other' ORDER BY state, county" + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    for (url,) in rows:
        status, reason = screen_site(url, cfg, session)
        con.execute("UPDATE website_candidates SET screen_status=?, screen_reason=?, screened_at=? WHERE url=?",
                    [status, reason, db.utc_now(), url])
        counts[status] = counts.get(status, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Adding to the list: a PROPOSED county file for a person to review
# ---------------------------------------------------------------------------
SOURCE_FOR_KIND = {"recorder": "recorder", "court": "court", "public_notice": "public_notice"}


def write_proposals(con, out_dir: str | Path | None = None) -> list[Path]:
    out = Path(out_dir or PROPOSAL_DIR)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for state, county in con.execute("SELECT DISTINCT state, county FROM website_candidates ORDER BY 1, 2").fetchall():
        rows = con.execute("SELECT url, kind, screen_status, screen_reason FROM website_candidates "
                           "WHERE state=? AND county=? AND kind != 'other'", [state, county]).fetchall()
        if not rows:
            continue
        sources = {}
        for kind in SOURCE_FOR_KIND.values():
            mine = [r for r in rows if r[1] == kind]
            if mine:
                sources[kind] = {"access": "manual_inbox",
                                 "notes": "PROPOSED by discover.py. A person must confirm each site's terms before changing "
                                          "access to scrape/open_data_api. Candidates: "
                                          + "; ".join(f"{u} [{s}: {why or 'not screened yet'}]" for u, _, s, why in mine)}
        if not sources:
            continue
        doc = {"county": county, "state": state, "status": "manual_inbox", "priority": 5,
               "notes": "PROPOSED file written by discover.py. Review, then move it to config/counties/ (not done automatically).",
               "sources": sources}
        path = out / f"{state.lower()}_{county_slug(county)}.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        written.append(path)
    return written


def report(con) -> str:
    rows = con.execute("SELECT screen_status, count(*) FROM website_candidates GROUP BY 1 ORDER BY 1").fetchall()
    return "Candidate websites: " + (", ".join(f"{n} {s}" for s, n in rows) if rows else "none yet")


def main() -> None:
    ap = argparse.ArgumentParser(description="Find county record websites online and add them as candidates.")
    ap.add_argument("--directory", default="netr_online")
    ap.add_argument("--state"), ap.add_argument("--county", help='e.g. "Washtenaw County"')
    ap.add_argument("--limit", type=int, help="max counties (or sites to screen)")
    ap.add_argument("--screen", action="store_true", help="screen the sites found (robots.txt, login/CAPTCHA, terms)")
    ap.add_argument("--proposals", action="store_true", help="write proposed county settings files")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    setup_logging()
    con = db.connect()
    ensure_table(con)
    cfg = load_config()
    if not args.report:
        counties = [(args.state, args.county)] if args.county else [
            (s, c) for s, c in con.execute(
                "SELECT state, county FROM county_registry WHERE (? IS NULL OR state = ?) ORDER BY priority, state, county",
                [args.state, args.state]).fetchall()][: args.limit]
        try:
            for st, co in counties:
                discover_county(con, cfg, args.directory, st, co)
        except DirectoryNotApproved as e:
            raise SystemExit(str(e))
        if args.screen:
            print("Screened:", screen_pending(con, cfg, args.limit))
    if args.proposals:
        for p in write_proposals(con):
            print("Proposed:", p)
    print(report(con))


if __name__ == "__main__":
    main()
