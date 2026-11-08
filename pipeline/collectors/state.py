"""
state.py - state licensing actions, CMS nursing home data, and business-registry lookups.

In plain English, three jobs:
  1. CMS nursing home data (federal, open): downloads the public CMS Provider Data files (provider info,
     health deficiencies, ownership, penalties) for ALL states and saves them unchanged in raw_files
     with source "cms". The provider file is also read into the `licensing_records` table with simple
     distress flags (special focus facility, abuse citation, big fines, payment denials, 1-star rating).
  2. State licensing actions: for each state source in config/state_sources.yaml, saves the published
     enforcement / disciplinary actions as files (source "state_license", doc_category "license_action").
     States whose site is not confirmed to allow automated access stay in MANUAL inbox mode.
  3. Business registry: for entity names Part C puts in the entity_lookup_requests table, looks up the
     LLC in the state's business entity search where automated access is allowed (none are turned on
     yet; Michigan is manual). Skipped if the table is empty.

Never logs in, never solves a CAPTCHA. A blocked site is switched to the manual inbox and the reason logged.

Run from the pipeline/ folder:
    python -m collectors.state                                  # every enabled source
    python -m collectors.state --only cms_nursing_home_provider_info

(Adapted from Neel's state.py: CSV parsing, column matching and distress rules are his.)
"""
from __future__ import annotations

import argparse
import csv
import io
import re
from datetime import date
from typing import Any

from collectors.common import config, db, fetch
from collectors.common.files import STRUCTURED_MARKER, store_file
from collectors.common.log import RunTracker, get_logger, setup_logging
from collectors.common.parse import clean_str, load_yaml, to_date, to_float

COLLECTOR = "state.py"
CONFIG_PATH = config.CONFIG_DIR / "state_sources.yaml"
REGISTRY_PATH = config.CONFIG_DIR / "state_registry.yaml"
CMS_METASTORE_URL = "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/{dataset_id}"
log = get_logger("state")
csv.field_size_limit(10_000_000)


# ---------------------------------------------------------------------------
# Reading a facility CSV and applying the distress rules
# ---------------------------------------------------------------------------
def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _col(header_index: dict[str, int], candidates: list[str]) -> int | None:
    return next((header_index[_norm(c)] for c in candidates if _norm(c) in header_index), None)


def distress_reasons(row: list[str], header_index: dict[str, int], rules: list[dict]) -> list[str]:
    """Which distress rules this row trips (see the notes at the top of state_sources.yaml)."""
    reasons = []
    for rule in rules:
        pos = _col(header_index, rule["column"])
        if pos is None or pos >= len(row):
            continue
        value = row[pos].strip()
        hit = False
        if "in" in rule:
            hit = value.lower() in {str(v).lower() for v in rule["in"]}
        elif "matches" in rule:
            hit = bool(value) and re.search(rule["matches"], value) is not None
        elif "at_least" in rule:
            num = to_float(value)
            hit = num is not None and num >= float(rule["at_least"])
        if hit:
            reasons.append(rule.get("reason", "{value}").replace("{value}", value))
    return reasons


def parse_records(data: bytes, src: dict, *, file_id: str, source: str, default_as_of: date) -> list[dict]:
    """CSV bytes -> licensing_records rows using the source's column map and distress rules."""
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
    if not rows:
        return []
    header_index = {_norm(h): i for i, h in enumerate(rows[0])}
    positions = {k: _col(header_index, v) for k, v in src["columns"].items()}
    missing = [k for k, v in positions.items() if v is None]
    if missing:
        log.warning("[%s] columns not found: %s", src["name"], ", ".join(missing))
    if positions.get("license_number") is None:
        raise ValueError(f"[{src['name']}] no license/ID column found; fix 'columns' in state_sources.yaml")

    out: dict[tuple[str, date], dict] = {}
    for r in rows[1:]:
        def get(k: str):
            pos = positions.get(k)
            return clean_str(r[pos]) if pos is not None and pos < len(r) else None
        lic = get("license_number")
        if not lic:
            continue
        reasons = distress_reasons(r, header_index, src.get("distress", []))
        as_of = to_date(get("as_of_date")) or default_as_of
        state = get("state") or src.get("state")
        rec = {
            "record_id": f"{source}:{(state or '').upper()}:{lic}", "as_of_date": as_of, "source": source,
            "file_id": file_id, "facility_name": get("facility_name"), "legal_name": get("legal_name"),
            "license_number": lic, "facility_type": get("facility_type"), "address": get("address"),
            "city": get("city"), "state": state, "zip": get("zip"), "status": get("status"),
            "distress_flag": bool(reasons), "distress_reason": "; ".join(reasons) or None,
        }
        out[(rec["record_id"], as_of)] = rec
    return list(out.values())


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------
def _get(url: str, session=None):
    return fetch.polite_get(url, config.domain_settings(url.split("/")[2]), session=session)


def resolve_download_url(src: dict, session=None) -> str:
    """Find the current CSV link for cms / ckan sources; the direct URL otherwise."""
    kind = src["kind"]
    if kind == "cms":
        resp = _get(CMS_METASTORE_URL.format(dataset_id=src["dataset_id"]), session)
        if resp is None:
            raise RuntimeError(f"could not read the CMS dataset listing for {src['dataset_id']} (blocked or not found)")
        for dist in resp.json().get("distribution", []):
            url = dist.get("downloadURL") or dist.get("data", {}).get("downloadURL")
            if url and (url.lower().endswith(".csv") or "csv" in str(dist.get("mediaType", "")).lower()):
                return url
        raise ValueError(f"no CSV download listed for CMS dataset {src['dataset_id']}")
    if kind == "ckan":
        resp = _get(src["api_url"].format(dataset=src["dataset"]), session)
        if resp is None:
            raise RuntimeError(f"could not read the CKAN dataset listing {src['dataset']}")
        pat = re.compile(src.get("resource_pattern", "(?i)csv"))
        for res in resp.json().get("result", {}).get("resources", []):
            label = f"{res.get('format', '')} {res.get('name', '')} {res.get('url', '')}"
            if res.get("url") and pat.search(label):
                return res["url"]
        raise ValueError(f"no CSV resource in CKAN dataset {src['dataset']}")
    return src["url"]


def collect_source(con, run, src: dict, session=None, today: date | None = None) -> None:
    """Download one source and store it (and its rows, if it is structured)."""
    today = today or date.today()
    if src["kind"] == "manual":
        run.manual_inbox(src["name"], src.get("notes", "manual source"))
        return
    is_cms, structured = src["kind"] == "cms", bool(src.get("parse"))
    source = "cms" if is_cms else "state_license"
    state = src.get("state")
    url = resolve_download_url(src, session)
    is_page = src["kind"] == "page"
    notes = f"{src['name']}" + (f" {STRUCTURED_MARKER}" if (structured or is_cms) else "")
    res = fetch.fetch_and_store(
        con, url, config.domain_settings(url.split("/")[2]), source=source,
        doc_category="other" if is_cms else "license_action", collector_name=f"{COLLECTOR}:{src['name']}",
        session=session, state=state, ext=None if is_page else "csv", notes=notes, source_record_id=src["name"])
    if res is None:
        domain = url.split("/")[2]
        if fetch.needs_manual_inbox(domain):
            run.manual_inbox(src["name"], fetch.get_fallbacks()[domain])
        raise RuntimeError(f"could not download {url}")
    loaded = con.execute("SELECT 1 FROM licensing_records WHERE file_id = ? LIMIT 1", [res.file_id]).fetchone()
    if res.status == "duplicate" and (loaded or not structured):
        run.skipped()
        return
    run.new_file() if res.status == "new" else run.skipped()
    if structured:
        data = config.from_stored_path(res.file_path).read_bytes()
        rows = parse_records(data, src, file_id=res.file_id, source=source, default_as_of=today)
        for r in rows:
            db.insert_row(con, "licensing_records", r, replace=True)
        log.info("[%s] %d records, %d flagged as distressed", src["name"], len(rows),
                 sum(1 for r in rows if r["distress_flag"]))


# ---------------------------------------------------------------------------
# Business registry lookups (entity_lookup_requests from Part C)
# ---------------------------------------------------------------------------
def lookup_entities(con, run, session=None, registry_path=REGISTRY_PATH) -> dict:
    """
    Answer pending entity_lookup_requests. A state in manual_inbox mode (the default for every state) is
    only logged: a person looks the entity up and drops the result in the inbox. For an open_data_api
    state, the response is saved as a business_record file and the request is marked done.
    """
    totals = {"pending": 0, "done": 0, "failed": 0, "manual": 0}
    try:
        pending = con.execute("SELECT entity_name, state FROM entity_lookup_requests WHERE status = 'pending' "
                              "OR status IS NULL").fetchall()
    except Exception:                       # the table does not exist yet: nothing to do
        return totals
    totals["pending"] = len(pending)
    if not pending:
        return totals
    try:
        states_cfg = (load_yaml(registry_path) or {}).get("states", {})
    except OSError:                         # no registry file: every state is manual
        log.warning("registry settings file %s not found; treating every state as manual", registry_path)
        states_cfg = {}
    manual_states: set[str] = set()
    for name, state in pending:
        cfg = states_cfg.get((state or "").upper(), {"mode": "manual_inbox"})
        if cfg.get("mode") != "open_data_api":
            totals["manual"] += 1
            manual_states.add((state or "?").upper())
            continue
        try:
            resp = fetch.polite_get(cfg["url"], config.domain_settings(cfg["url"].split("/")[2]), session=session,
                                    params={cfg.get("query_param", "q"): name})
            if resp is None:
                raise RuntimeError("blocked, disallowed or not found")
            res = store_file(con, resp.content, source="state_license", doc_category="business_record",
                             collector_name=f"{COLLECTOR}:registry", state=state.upper(), ext="json",
                             source_record_id=name, notes=f"business registry lookup for {name}")
            con.execute("UPDATE entity_lookup_requests SET status = 'done' WHERE entity_name = ? AND state = ?",
                        [name, state])
            totals["done"] += 1
            run.new_file() if res.status == "new" else run.skipped()
        except Exception as e:
            con.execute("UPDATE entity_lookup_requests SET status = 'failed' WHERE entity_name = ? AND state = ?",
                        [name, state])
            totals["failed"] += 1
            run.error(f"registry lookup for {name} ({state}): {e}")
    for st in sorted(manual_states):
        run.manual_inbox(f"business registry {st}",
                         "no automated lookup allowed: look the entity up by hand and drop the result in "
                         f"data/inbox/state_license/{st}/_statewide/ (see config/state_registry.yaml)")
    return totals


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def collect(con, only: list[str] | None = None, session=None, config_path=CONFIG_PATH,
            registry_path=REGISTRY_PATH) -> RunTracker:
    """The entry point used by run_all.py."""
    with RunTracker(con, COLLECTOR) as run:
        for src in load_yaml(config_path)["sources"]:
            if only and src["name"] not in only:
                continue
            if not only and not src.get("enabled", True):
                continue
            try:
                collect_source(con, run, src, session)
            except Exception as e:                         # one failing source must not stop the others
                run.error(f"{src['name']}: {e}")
        if not only:
            try:
                lookup_entities(con, run, session, registry_path)
            except Exception as e:                         # the registry step must never break the rest
                run.error(f"business registry lookups: {e}")
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description="Load CMS nursing home data and state licensing actions.")
    parser.add_argument("--only", action="append", help="run just this source name (repeatable)")
    args = parser.parse_args()
    setup_logging()
    con = db.connect()
    run = collect(con, args.only)
    print(f"State/CMS: {run.status}, {run.files_new} new, {run.files_skipped} skipped, {len(run.errors)} errors")


if __name__ == "__main__":
    main()
