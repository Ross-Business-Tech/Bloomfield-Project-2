"""collectors/cms_ops.py  -  fill operating_metrics + market_occupancy from CMS nursing-facility data,
so signal Q (occupancy slide) has real occupancy history for senior-care properties.

WHAT THIS DATA IS - read before trusting it:
  * The CMS "Provider Information" file (dataset 4pq5-n9py) covers Medicare/Medicaid CERTIFIED NURSING
    FACILITIES (skilled nursing / nursing homes) - not every kind of senior housing. Independent living
    and most assisted living communities are NOT in it.
  * occupancy = average_number_of_residents_per_day / number_of_certified_beds. Both come from the same
    facility, so this is true facility occupancy, not an estimate.
  * Each run is a SNAPSHOT of the file's Processing Date. One snapshot cannot show a slide: history
    only exists after this collector has run for several CMS updates (CMS refreshes the file about
    monthly). disclosed_on is the processing date, so future-dated history can never leak into scores.
  * market_occupancy rows are a SIMPLE AVERAGE of facility occupancy per state, labeled
    property_type "Nursing facility". The scorer keys these under "Senior housing (nursing)" and
    uses them ONLY for properties whose own records say nursing/skilled care; assisted and
    independent-living properties never see nursing-facility averages (they honestly get
    "no market data" until a NIC/assisted-living source is added).

Run it with:   python -m collectors.cms_ops
It reads the CSV the state.py collector already downloads; if none is stored yet it downloads the
current file itself (same public source, same terms - verified_terms: true in state_sources.yaml).
"""

from __future__ import annotations

import argparse
import csv
import io
from collections import defaultdict
from datetime import date

from collectors import state as state_collector
from collectors.common import config, db, fetch
from collectors.common.files import STRUCTURED_MARKER
from collectors.common.log import RunTracker, get_logger, setup_logging
from collectors.common.parse import load_yaml, to_float

log = get_logger("cms_ops.py")
COLLECTOR = "cms_ops.py"
SOURCE = "cms_nursing"                      # nursing facilities only - see the module docstring
PROVIDER_INFO = "cms_nursing_home_provider_info"

BEDS_COLS = ["Number of Certified Beds"]
RESIDENTS_COLS = ["Average Number of Residents per Day"]

csv.field_size_limit(10_000_000)


def provider_info_source() -> dict:
    """The provider-info source from state_sources.yaml (already terms-verified)."""
    for src in (load_yaml(state_collector.CONFIG_PATH).get("sources") or []):
        if src.get("name") == PROVIDER_INFO:
            return src
    raise KeyError(f"{PROVIDER_INFO} not found in state_sources.yaml")


def stored_csv(con) -> tuple[str, str] | None:
    """The newest provider-info CSV already saved by state.py, as (file_id, text)."""
    row = con.execute("SELECT file_id, file_path FROM raw_files WHERE source = 'cms' AND source_record_id = ? "
                      "ORDER BY collected_at DESC LIMIT 1", [PROVIDER_INFO]).fetchone()
    if not row:
        return None
    return row[0], config.from_stored_path(row[1]).read_text(errors="ignore")


def download_csv(con, src: dict, session=None) -> tuple[str, str] | None:
    """No stored file yet: download the current provider-info CSV exactly like state.py does."""
    url = state_collector.resolve_download_url(src, session)
    res = fetch.fetch_and_store(con, url, config.domain_settings(url.split("/")[2]), source="cms",
                                doc_category="other", collector_name=f"{COLLECTOR}:{PROVIDER_INFO}",
                                session=session, ext="csv",
                                notes=f"{PROVIDER_INFO} {STRUCTURED_MARKER}", source_record_id=PROVIDER_INFO)
    if res is None:
        return None
    return res.file_id, config.from_stored_path(res.file_path).read_text(errors="ignore")


def parse_rows(src: dict, text: str) -> tuple[str, list[dict]]:
    """Read the CSV. Returns (processing_date_iso, one dict per facility with beds + residents)."""
    cols = src["columns"]
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None) or []
    idx = state_collector._col  # CMS header names drift; state.py's matcher handles the aliases
    h = {state_collector._norm(name): i for i, name in enumerate(header)}
    pos = {k: idx(h, v) for k, v in cols.items()}
    beds_i = idx(h, BEDS_COLS)
    res_i = idx(h, RESIDENTS_COLS)
    if beds_i is None or res_i is None:
        raise ValueError("beds/residents columns not found in the CMS file (its headers changed?)")
    rows, as_of = [], None
    for parts in reader:
        def val(p):                      # noqa: E306
            return parts[p].strip() if p is not None and 0 <= p < len(parts) else ""
        beds, residents = to_float(val(beds_i)), to_float(val(res_i))
        as_of = as_of or val(pos.get("as_of_date"))
        if not beds or beds <= 0 or residents is None:
            continue                    # no honest occupancy without both numbers from the same facility
        rows.append({"ccn": val(pos.get("license_number")), "name": val(pos.get("facility_name")),
                     "address": val(pos.get("address")), "city": val(pos.get("city")),
                     "state": val(pos.get("state")).upper(), "zip": val(pos.get("zip")),
                     "occupancy": min(residents / beds, 1.5)})   # >100% happens (doubled rooms); cap sanity
    return as_of, rows


def collect(con, *, session=None) -> RunTracker:
    from scorer.equation import create_equation_tables
    from scout_reader_schema import create_scout_reader_tables
    with RunTracker(con, COLLECTOR) as run:
        create_equation_tables(con)
        create_scout_reader_tables(con)
        src = provider_info_source()
        got = stored_csv(con) or download_csv(con, src, session)
        if got is None:
            run.manual_inbox(PROVIDER_INFO, "could not get the CMS Provider Information CSV")
            return run
        file_id, text = got
        as_of, rows = parse_rows(src, text)
        if not rows:
            run.error("no facility rows with beds + residents in the CMS file")
            return run
        try:
            period = date.fromisoformat(as_of[:10])
        except (TypeError, ValueError):
            run.error(f"no usable Processing Date in the CMS file (got {as_of!r})")
            return run
        now = db.utc_now()
        quote = '{"note": "nursing-facility occupancy: avg residents per day / certified beds, same facility"}'
        fids = [f"{file_id}:{r['ccn'] or i}" for i, r in enumerate(rows)]
        # One sweep per table: this period's rows are replaced wholesale (reruns never duplicate).
        con.execute("DELETE FROM operating_metrics WHERE period_end = ? AND source = ?", [period, SOURCE])
        con.execute("DELETE FROM market_occupancy WHERE period = ? AND source = ?", [period, SOURCE])
        con.execute("DELETE FROM filing_fields WHERE filing_type = 'cms_provider_info' AND file_id LIKE ?", [file_id + ":%"])
        con.executemany(
            "INSERT OR REPLACE INTO filing_fields (file_id, filing_type, project_name, property_address, "
            "property_city, property_state, property_zip, property_type, filing_date, status, confidence, "
            "model_name, prompt_version, extracted_at, quotes) VALUES (?, 'cms_provider_info', ?, ?, ?, ?, ?, "
            "'Senior housing', ?, 'ok', 'high', 'structured (no AI needed)', 'cms_ops.py', ?, ?)",
            [(fid, r["name"], r["address"], r["city"], r["state"], r["zip"], period, now, quote)
             for fid, r in zip(fids, rows)])
        con.executemany(
            "INSERT INTO operating_metrics (link_id, period_end, period_months, occupancy, source, file_id, "
            "disclosed_on) VALUES (?, ?, 12, ?, ?, ?, ?)",
            [(fid, period, r["occupancy"], SOURCE, fid, period) for fid, r in zip(fids, rows)])
        state_occ: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            if r["state"]:
                state_occ[r["state"]].append(r["occupancy"])
        con.executemany(
            "INSERT INTO market_occupancy (state, property_type, period, occupancy, source) "
            "VALUES (?, 'Nursing facility', ?, ?, ?)",
            [(st, period, sum(occs) / len(occs), SOURCE) for st, occs in state_occ.items()])
        n = len(rows)
        run.new_file()
        log.info("%s: %d facilities, %d state averages, period %s", COLLECTOR, n, len(state_occ), period)
    return run


def main(argv=None):
    argparse.ArgumentParser(description="Fill operating_metrics + market_occupancy from CMS nursing-facility data.").parse_args(argv)
    setup_logging()
    con = db.connect()
    collect(con)


if __name__ == "__main__":
    main()
