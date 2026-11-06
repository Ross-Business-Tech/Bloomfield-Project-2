"""Pull state licensing distress records and CMS nursing home data.

    python -m collectors.state                  # every enabled source in config/state_sources.yaml
    python -m collectors.state --only cms_nursing_home_provider_info

Each file is registered in raw_files (source = "CMS" or "STATE:<XX>"). Structured
sources (CSV) also get one row per facility in `licensing_records`, with
distress_flag / distress_reason set by the rules in the YAML.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from collectors.common import (
    CONFIG_DIR,
    FileResult,
    HttpClient,
    RunStats,
    clean_str,
    download_file,
    get_engine,
    init_db,
    load_yaml,
    run_isolated,
    setup_logging,
    to_float,
    to_iso_date,
    upsert,
)

CONFIG_PATH = CONFIG_DIR / "state_sources.yaml"
RECORD_COLUMNS = [
    "record_id", "as_of_date", "source", "file_id", "facility_name", "legal_name", "license_number",
    "facility_type", "address", "city", "state", "zip", "status", "distress_flag", "distress_reason",
]
log = logging.getLogger("collectors.state")
csv.field_size_limit(10_000_000)


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _col(header_index: dict[str, int], candidates: list[str]) -> int | None:
    return next((header_index[_norm(c)] for c in candidates if _norm(c) in header_index), None)


def _distress(row: list[str], header_index: dict[str, int], rules: list[dict[str, Any]]) -> list[str]:
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


def parse_records(data: bytes, src: dict[str, Any], *, file_id: str, default_as_of: str) -> list[dict[str, Any]]:
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

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows[1:]:
        get = lambda k: clean_str(r[positions[k]]) if positions.get(k) is not None and positions[k] < len(r) else None  # noqa: E731
        lic = get("license_number")
        if not lic:
            continue
        reasons = _distress(r, header_index, src.get("distress", []))
        as_of = to_iso_date(get("as_of_date")) or default_as_of
        rec = {
            "record_id": f"{src['source']}:{lic}",
            "as_of_date": as_of,
            "source": src["source"],
            "file_id": file_id,
            "facility_name": get("facility_name"),
            "legal_name": get("legal_name"),
            "license_number": lic,
            "facility_type": get("facility_type"),
            "address": get("address"),
            "city": get("city"),
            "state": get("state") or src.get("state"),
            "zip": get("zip"),
            "status": get("status"),
            "distress_flag": int(bool(reasons)),
            "distress_reason": "; ".join(reasons) or None,
        }
        out[(rec["record_id"], as_of)] = rec
    return list(out.values())


def resolve_download_url(client: HttpClient, src: dict[str, Any]) -> str:
    """Find the current CSV link for cms / ckan sources; direct URL otherwise."""
    kind = src["kind"]
    if kind == "cms":
        meta = client.get(src["metastore_url"].format(dataset_id=src["dataset_id"])).json()
        for dist in meta.get("distribution", []):
            url = dist.get("downloadURL") or dist.get("data", {}).get("downloadURL")
            if url and (url.lower().endswith(".csv") or "csv" in str(dist.get("mediaType", "")).lower()):
                return url
        raise ValueError(f"no CSV download listed for CMS dataset {src['dataset_id']}")
    if kind == "ckan":
        result = client.get(src["api_url"].format(dataset=src["dataset"])).json().get("result", {})
        pat = re.compile(src.get("resource_pattern", "(?i)csv"))
        for res in result.get("resources", []):
            label = f"{res.get('format', '')} {res.get('name', '')} {res.get('url', '')}"
            if res.get("url") and pat.search(label):
                return res["url"]
        raise ValueError(f"no CSV resource in CKAN dataset {src['dataset']}")
    return src["url"]


def _rows_loaded(engine: Engine, file_id: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT 1 FROM licensing_records WHERE file_id = :f LIMIT 1"), {"f": file_id}
        ).first() is not None


def collect_source(client: HttpClient, engine: Engine, src: dict[str, Any], stats: RunStats,
                   today: str | None = None) -> FileResult:
    """Download one source once and store its rows (if structured)."""
    today = today or date.today().isoformat()
    url = resolve_download_url(client, src)
    is_page = src["kind"] == "page"
    res = download_file(
        client, engine, url, source=src["source"], state=src.get("state"), date=today,
        needs_reading=is_page, skip_if_url_seen=False, ext=None if is_page else ".csv", stats=stats,
    )
    if is_page or (not res.is_new and _rows_loaded(engine, res.file_id)):
        return res
    rows = parse_records(Path(res.local_path).read_bytes(), src, file_id=res.file_id, default_as_of=today)
    stats.rows_written += upsert(engine, "licensing_records", rows, ["record_id", "as_of_date"])
    flagged = sum(r["distress_flag"] for r in rows)
    log.info("[%s] %d records, %d flagged as distressed", src["name"], len(rows), flagged)
    return res


def run(client: HttpClient, engine: Engine, *, only: list[str] | None = None,
        config_path: Path = CONFIG_PATH, stats: RunStats | None = None, today: str | None = None) -> RunStats:
    """Collect every enabled source; a failing source is logged and skipped."""
    stats = stats or RunStats("state")
    for src in load_yaml(config_path)["sources"]:
        if only and src["name"] not in only:
            continue
        if not only and not src.get("enabled", True):
            continue
        run_isolated(stats, src["name"], lambda s=src: collect_source(client, engine, s, stats, today))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load state licensing + CMS nursing home data.")
    parser.add_argument("--only", action="append", help="run just this source name (repeatable)")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--db-url")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    engine = get_engine(args.db_url)
    init_db(engine)
    stats = run(HttpClient(), engine, only=args.only, config_path=args.config)
    stats.log_summary()
    return 1 if stats.errors and not (stats.rows_written or stats.files_downloaded) else 0


if __name__ == "__main__":
    sys.exit(main())
