"""
county.py - ONE reusable collector for county (and statewide) court, recorder and public-notice sites.

In plain English: there are over 3,000 counties and every website looks different. Instead of writing
code for each, every county gets a small YAML settings file in config/counties/ that says where to
look and which parts of the page mean "case number", "filing date", "parties", "document type" and
"link to the document". ADDING A COUNTY MEANS ADDING A YAML FILE, NOT WRITING PYTHON.

Each county file can describe up to three kinds of source (see config/counties/_template.yaml):
    court / recorder / public_notice
and each source has an access mode:
    scrape         fetch search-result pages politely and read them with the selectors in the YAML
    open_data_api  call a county open-data portal (Socrata, ArcGIS or plain JSON)
    manual_inbox   do nothing automatic; load the files a person dropped in data/inbox/ for that county
A "statewide" file (statewide: true) covers every county in a state, so one config can replace many.

What it does for each record found:
  * keeps only the document types listed in the YAML (commercial-looking foreclosure, receivership, lis
    pendens, lender lawsuits, tax delinquency...) and skips rows the site labels residential / consumer,
  * only looks back a set number of days (365 the first time a source runs, then 14: sources.yaml),
  * downloads the document (PDF/HTML) and saves it unchanged in raw_files; if the row has no document
    link, the row itself is saved as a small JSON file,
  * if the site blocks automation (robots.txt, 401/403, CAPTCHA, login) the source switches to the manual
    inbox and the reason is logged. One broken county or selector never stops the others.

Run from the pipeline/ folder:
    python -m collectors.county                          # every configured county
    python -m collectors.county --county mi_washtenaw    # one county (the file name without .yaml)
    python -m collectors.county --state MI --since 2026-10-01

(Link-list pages, paging and the robots/CAPTCHA ideas are adapted from Neel's county.py.)
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from collectors.common import config, db, fetch, inbox
from collectors.common.files import county_slug, store_file
from collectors.common.log import RunTracker, get_logger, setup_logging
from collectors.common.parse import load_yaml, to_date

COLLECTOR = "county.py"
COUNTIES_DIR = config.CONFIG_DIR / "counties"
log = get_logger("county")

# config section name -> raw_files.source
SECTION_SOURCE = {"court": "county_court", "recorder": "recorder", "public_notice": "public_notice"}
ACCESS_MODES = {"scrape", "open_data_api", "manual_inbox"}


class SourceBlocked(Exception):
    """The site will not let us automate (robots.txt, 401/403, CAPTCHA, login). Use the manual inbox."""


# ---------------------------------------------------------------------------
# Loading the YAML files
# ---------------------------------------------------------------------------
def load_configs(config_dir: Path | None = None, state: str | None = None,
                 county: str | None = None) -> list[dict]:
    """
    Every county / statewide file in config/counties/ (files starting with "_" are templates and are
    ignored). Each config gets an "id" (the file name) added. Filter by state ("MI") or county id.
    """
    out = []
    for path in sorted((config_dir or COUNTIES_DIR).glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        cfg = load_yaml(path) or {}
        cfg["id"] = path.stem
        if state and str(cfg.get("state", "")).upper() != state.upper():
            continue
        if county and cfg["id"] != county:
            continue
        out.append(cfg)
    return out


def _label(cfg: dict) -> str:
    return cfg["id"] if not cfg.get("statewide") else f"{cfg['id']} (statewide)"


# ---------------------------------------------------------------------------
# Reading result pages (scrape mode)
# ---------------------------------------------------------------------------
def select_value(el, spec: str | None) -> str | None:
    """
    Pull one value out of a row using a selector like "td.case-no" (the text) or "td.doc a@href"
    (an attribute). Returns None if nothing matches. A badly written selector raises ValueError.
    """
    if not spec:
        return None
    css, _, attr = spec.partition("@")
    try:
        found = el.select_one(css.strip())
    except Exception as e:                                   # invalid CSS
        raise ValueError(f"bad selector '{spec}': {e}") from e
    if found is None:
        return None
    value = found.get(attr.strip()) if attr else " ".join(found.get_text(" ").split())
    return value.strip() if value else None


def parse_rows(html: str, selectors: dict, page_url: str) -> tuple[list[dict], list[str]]:
    """
    Read the result rows of one page. Returns (records, problems). A problem is something wrong with
    the YAML selectors (matched nothing, bad syntax), reported so a person can fix the file.
    """
    soup = BeautifulSoup(html, "lxml")
    try:
        rows = soup.select(selectors["row"])
    except Exception as e:
        return [], [f"bad row selector '{selectors['row']}': {e}"]
    if not rows:
        return [], [f"row selector '{selectors['row']}' matched nothing on {page_url}"]
    records, problems = [], []
    empty = {k: 0 for k in ("record_id", "filing_date", "parties", "document_type", "case_type", "document_link")
             if selectors.get(k)}
    for row in rows:
        try:
            rec = {k: select_value(row, selectors.get(k)) for k in
                   ("record_id", "filing_date", "parties", "document_type", "case_type", "document_link")}
        except ValueError as e:
            return [], [str(e)]
        for k in empty:
            if rec.get(k) is None:
                empty[k] += 1
        rec["document_url"] = urljoin(page_url, rec.pop("document_link")) if rec.get("document_link") else None
        records.append(rec)
    for k, n in empty.items():                               # a selector that found nothing in ANY row is broken
        if n == len(rows):
            problems.append(f"selector '{k}' ('{selectors[k]}') matched nothing in {len(rows)} rows on {page_url}")
    return records, problems


def extract_links(html: str, page_url: str, link_pattern: str, link_text_pattern: str | None = None) -> list[dict]:
    """(Neel) Links on a page whose address matches a pattern: for pages that are just lists of PDF links."""
    soup = BeautifulSoup(html, "lxml")
    url_re = re.compile(link_pattern, re.IGNORECASE)
    text_re = re.compile(link_text_pattern, re.IGNORECASE) if link_text_pattern else None
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "#")):
            continue
        absolute = urljoin(page_url, href).split("#", 1)[0]
        text = " ".join(a.get_text(" ").split())
        if absolute in seen or not url_re.search(absolute) or (text_re and not text_re.search(text)):
            continue
        seen.add(absolute)
        out.append({"record_id": Path(urlparse(absolute).path).stem or None, "filing_date": None, "parties": text or None,
                    "document_type": text or None, "case_type": None, "document_url": absolute})
    return out


def next_page_url(html: str, page_url: str, next_selector: str | None) -> str | None:
    if not next_selector:
        return None
    soup = BeautifulSoup(html, "lxml")
    try:
        a = soup.select_one(next_selector)
    except Exception:
        return None
    href = a.get("href") if a else None
    return urljoin(page_url, href) if href and not href.startswith("javascript:") else None


# ---------------------------------------------------------------------------
# Reading open-data portals
# ---------------------------------------------------------------------------
def _dig(data: Any, path: str | None) -> Any:
    for part in (path or "").split("."):
        if part:
            data = data[part]
    return data


def api_records(payload: Any, api: dict) -> list[dict]:
    """The list of rows inside one API response (Socrata: a list; ArcGIS: features/attributes)."""
    fmt = api.get("format", "socrata")
    if fmt == "arcgis":
        return [f.get("attributes", {}) for f in payload.get("features", [])]
    rows = _dig(payload, api.get("records_path")) if fmt == "json" else payload
    return rows if isinstance(rows, list) else []


def map_api_row(row: dict, fields: dict, base_url: str) -> dict:
    """Rename the portal's field names to ours (see the fields: section of the YAML)."""
    rec = {k: (str(row[v]).strip() if row.get(v) not in (None, "") else None) for k, v in fields.items()}
    rec.setdefault("case_type", None)
    url = rec.pop("document_link", None) or rec.get("document_url")
    rec["document_url"] = urljoin(base_url, url) if url else None
    return rec


# ---------------------------------------------------------------------------
# Deciding what to keep and saving it
# ---------------------------------------------------------------------------
def _matches(doc_type: dict, text: str) -> bool:
    pattern = doc_type.get("match") or re.escape(str(doc_type["value"]))
    return re.search(pattern, text or "", re.IGNORECASE) is not None


def classify(rec: dict, src: dict) -> str | None:
    """
    Which raw_files doc_category this record gets, or None to skip it. Skips rows the site labels
    residential / consumer (skip_labels), and rows whose document type is not in the doc_types list.
    """
    label = f"{rec.get('document_type') or ''} {rec.get('case_type') or ''}"
    for bad in src.get("skip_labels", []):
        if re.search(bad, label, re.IGNORECASE):
            return None
    doc_types = src.get("doc_types") or []
    if not doc_types:
        return src.get("default_doc_category", "other")
    for dt in doc_types:
        if _matches(dt, label) or _matches(dt, rec.get("record_id") or ""):
            return dt["doc_category"]
    return None


def parse_filing_date(value, src: dict) -> date | None:
    """Read a filing date using the site's date_format from the YAML first, then common formats."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    if src.get("date_format"):
        try:
            return datetime.strptime(str(value).strip(), src["date_format"]).date()
        except ValueError:
            pass
    return to_date(value)


def _notes(rec: dict) -> str:
    bits = [rec.get("parties"), rec.get("document_type"), rec.get("case_type"),
            f"filed {rec['filing_date']}" if rec.get("filing_date") else None]
    return "; ".join(str(b) for b in bits if b)


def save_record(con, run, rec: dict, *, source: str, category: str, state: str | None, county: str | None,
                session=None, scope: str | None = None) -> str | None:
    """Download the record's document (or save the row as JSON) into raw_files. Returns the file_id."""
    common = dict(source=source, doc_category=category, collector_name=COLLECTOR, state=state, county=county,
                  source_record_id=rec.get("record_id"), notes=_notes(rec), scope=scope)
    url = rec.get("document_url")
    if url:
        known = con.execute("SELECT file_id FROM raw_files WHERE url = ?", [url]).fetchone()
        if known:                                            # documents never change: do not fetch twice
            run.skipped()
            return known[0]
        res = fetch.fetch_and_store(con, url, config.domain_settings(urlparse(url).netloc), session=session,
                                    collector_name=COLLECTOR, **{k: v for k, v in common.items()
                                                                 if k != "collector_name"})
        if res is None:
            domain = urlparse(url).netloc
            if fetch.needs_manual_inbox(domain):
                raise SourceBlocked(fetch.get_fallbacks()[domain])
            run.error(f"could not download {url}")
            return None
    else:                                                    # no document link: save the row itself
        body = json.dumps({k: (v.isoformat() if isinstance(v, date) else v) for k, v in rec.items()}, sort_keys=True,
                          indent=2).encode()
        res = store_file(con, body, ext="json", **common)
    run.new_file() if res.status == "new" else run.skipped()
    return res.file_id


# ---------------------------------------------------------------------------
# One source
# ---------------------------------------------------------------------------
def _fmt(template: str, since: date, until: date, **extra) -> str:
    return template.format(since=since, until=until, **extra)


def iter_scrape_records(src: dict, since: date, until: date, session, problems: list[str]) -> Iterator[dict]:
    """Walk the search result pages (every doc type, every page) and yield the rows found."""
    base = src["base_url"]
    paging = src.get("paging") or {}
    max_pages = int(paging.get("max_pages", 5))
    dtypes = [dt["value"] for dt in src.get("doc_types", [])] if "{doc_type}" in src.get("search_url", "") else [""]
    for doc_type in dtypes:
        url: str | None = urljoin(base, _fmt(src["search_url"], since, until, page=int(paging.get("start_page", 1)),
                                             doc_type=quote_plus(str(doc_type))))
        page_no = int(paging.get("start_page", 1))
        for _ in range(max_pages):
            if url is None:
                break
            resp = fetch.polite_get(url, config.domain_settings(urlparse(url).netloc), session=session)
            if resp is None:
                domain = urlparse(url).netloc
                reason = fetch.get_fallbacks().get(domain) or "robots.txt disallows it, or the page was not found"
                raise SourceBlocked(f"{reason} ({url})")
            if src.get("layout", "table") == "links":
                found = extract_links(resp.text, url, src["link_pattern"], src.get("link_text_pattern"))
                for rec in found:
                    yield rec
            else:
                found, probs = parse_rows(resp.text, src["selectors"], url)
                problems.extend(probs)
                if probs and not found:
                    return
                for rec in found:
                    yield rec
            page_no += 1
            if paging.get("page_param"):                     # the page number is in the URL
                url = urljoin(base, _fmt(src["search_url"], since, until, page=page_no, doc_type=quote_plus(str(doc_type)))) \
                    if found else None
            else:
                url = next_page_url(resp.text, url, paging.get("next_selector") or paging.get("next_link_selector"))


def iter_api_records(src: dict, since: date, until: date, session) -> Iterator[dict]:
    """Call the open-data API (page by page) and yield rows renamed to our field names."""
    api = src["api"]
    page_size, max_pages = int(api.get("page_size", 500)), int(api.get("max_pages", 5))
    for page in range(max_pages):
        params = {k: (_fmt(v, since, until) if isinstance(v, str) else v) for k, v in (api.get("params") or {}).items()}
        if api.get("offset_param"):
            params[api["offset_param"]] = page * page_size
        if api.get("limit_param"):
            params[api["limit_param"]] = page_size
        resp = fetch.polite_get(api["url"], config.domain_settings(urlparse(api["url"]).netloc), session=session,
                                params=params)
        if resp is None:
            domain = urlparse(api["url"]).netloc
            raise SourceBlocked(fetch.get_fallbacks().get(domain) or f"the API did not answer ({api['url']})")
        rows = api_records(resp.json(), api)
        for row in rows:
            yield map_api_row(row, src["fields"], src.get("base_url", api["url"]))
        if len(rows) < page_size or not api.get("offset_param"):
            break


def collect_source(con, run, cfg: dict, section: str, src: dict, since: date, until: date, session=None,
                   max_results: int | None = None) -> None:
    """Run one source (court / recorder / public_notice) of one county or statewide config."""
    label, state = _label(cfg), (cfg.get("state") or "").upper() or None
    county = None if cfg.get("statewide") else cfg.get("county")
    source = SECTION_SOURCE[section]
    mode = src.get("access", "manual_inbox")
    if mode not in ACCESS_MODES:
        raise ValueError(f"unknown access mode '{mode}' (use one of {', '.join(sorted(ACCESS_MODES))})")

    def use_inbox(reason: str | None = None) -> None:
        if reason:
            run.manual_inbox(f"{label} {section}", reason)
        folder = "_statewide" if cfg.get("statewide") else (county_slug(county) if county else None)
        inbox.load_inbox(con, run, only_source=source, only_state=state, only_county=folder,
                         collector_name=COLLECTOR)

    if mode == "manual_inbox":
        use_inbox()
        return

    problems: list[str] = []
    limit = int(max_results or src.get("max_results", 200))
    saved = 0
    try:
        records = iter_api_records(src, since, until, session) if mode == "open_data_api" \
            else iter_scrape_records(src, since, until, session, problems)
        for rec in records:
            if saved >= limit:
                break
            filed = parse_filing_date(rec.get("filing_date"), src)
            if filed is not None:
                rec["filing_date"] = filed
                if not (since <= filed <= until):            # outside the look-back window
                    continue
            category = classify(rec, src)
            if category is None:                             # residential / consumer / not a wanted type
                continue
            if save_record(con, run, rec, source=source, category=category, state=state, county=county,
                           session=session, scope=county_slug(county) if county else (state or None)) is not None:
                saved += 1
    except SourceBlocked as e:
        use_inbox(str(e))                                     # fall back to the manual inbox, say why
    for p in problems:
        run.error(f"{label} {section}: {p}")


# ---------------------------------------------------------------------------
# All counties
# ---------------------------------------------------------------------------
def window(con, collector_key: str, since: date | None, until: date | None, cfg_sources: dict) -> tuple[date, date]:
    """Look back 365 days the first time a source runs, then 14 days (set in sources.yaml)."""
    until = until or date.today()
    if since is not None:
        return since, until
    lookback = config.load_sources_config().get("lookback_days", {})
    first = db.get_state(con, COLLECTOR, collector_key) is None
    days = int(lookback.get("first_run", 365) if first else lookback.get("weekly_run", 14))
    return until - timedelta(days=days), until


def collect(con, *, state: str | None = None, county: str | None = None, since: date | None = None,
            until: date | None = None, sections: list[str] | None = None, session=None,
            config_dir: Path | None = None, collector_name: str = COLLECTOR) -> RunTracker:
    """
    Run every configured source (statewide files once per state). One failing source never stops the others.
    The entry point used by run_all.py; recorded_loans.py, liens.py and permits.py reuse collect_source().
    """
    with RunTracker(con, collector_name) as run:
        for cfg in load_configs(config_dir, state, county):
            for section, src in (cfg.get("sources") or {}).items():
                if section not in SECTION_SOURCE or (sections and section not in sections):
                    continue
                key = f"{cfg['id']}:{section}:last_success"
                try:
                    start, end = window(con, key, since, until, cfg.get("sources", {}))
                    before = len(run.errors)
                    collect_source(con, run, cfg, section, src or {}, start, end, session)
                    if len(run.errors) == before:             # remember success: next run only looks back 14 days
                        db.set_state(con, COLLECTOR, key, end.isoformat())
                except Exception as e:                        # a bad county config must not stop the rest
                    run.error(f"{_label(cfg)} {section}: {e}")
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect county court / recorder / public notice records.")
    parser.add_argument("--county", help="one county (its file name in config/counties/, e.g. mi_washtenaw)")
    parser.add_argument("--state", help="only this state (2-letter code); includes its statewide file")
    parser.add_argument("--since", type=date.fromisoformat, help="look back to this date (YYYY-MM-DD)")
    parser.add_argument("--until", type=date.fromisoformat)
    args = parser.parse_args()
    setup_logging()
    con = db.connect()
    run = collect(con, state=args.state, county=args.county, since=args.since, until=args.until)
    print(f"County: {run.status}, {run.files_new} new, {run.files_skipped} skipped, {len(run.errors)} errors")
    for source, reason in run.fallbacks:
        print(f"  manual inbox: {source} ({reason})")


if __name__ == "__main__":
    main()
