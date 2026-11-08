"""
run.py - run every Part C step in order and save the results.

Order: lenders -> properties -> loan histories -> CMBS loan links -> entities -> lookup requests.
Each run rebuilds Part C's tables from scratch (in one transaction, so a crash leaves the old
results in place). People's yes/no answers in match_review are kept and obeyed.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from collectors.common import config, db

from .cmbs import link_cmbs_loans
from .entities import build_entities, request_lookups
from .lenders import match_lenders
from .loan_history import build_histories
from .properties import match_properties, review_rows
from .schema import REBUILT_EACH_RUN, rows, table_exists

MATCHING_YAML = config.CONFIG_DIR / "matching.yaml"


def load_matching_config(path: str | Path | None = None) -> dict:
    with open(path or MATCHING_YAML, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _insert(con, table: str, row: dict) -> None:
    cols = list(row)
    con.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [row[c] for c in cols])


def run_matcher(con, cfg: dict | None = None, as_of: date | None = None) -> dict:
    """Run everything. Returns counts for the printout."""
    cfg = cfg or load_matching_config()
    as_of = as_of or date.today()
    now = db.utc_now()

    lender_rows, lender_reviews = match_lenders(con, cfg)
    lender_rssd = {r["lender_name"]: r["rssd_id"] for r in lender_rows if r["rssd_id"]}
    props = match_properties(con, cfg)
    recorded = rows(con, "SELECT * FROM recorded_loans") if table_exists(con, "recorded_loans") else []
    histories = build_histories(recorded, props.record_property, lender_rssd, as_of)
    loan_links = link_cmbs_loans(con, props.record_property, cfg)
    entities, entity_links = build_entities(con, props.record_property, cfg)

    con.execute("BEGIN TRANSACTION")
    try:
        for t in REBUILT_EACH_RUN:
            con.execute(f"DELETE FROM {t}")
        for r in props.properties:
            _insert(con, "properties", {**r, "matched_at": now})
        for r in props.links:
            _insert(con, "property_links", {**r, "matched_at": now})
        for r in histories:
            _insert(con, "loan_histories", {**r, "built_at": now})
        for r in loan_links:
            _insert(con, "loan_links", {**r, "matched_at": now})
        for r in lender_rows:
            _insert(con, "lender_links", {**r, "matched_at": now})
        for r in entities:
            _insert(con, "entities", {**r, "matched_at": now})
        for r in entity_links:
            _insert(con, "entity_links", {**r, "matched_at": now})
        # New "maybe" matches; existing ones (and people's answers) are left alone.
        new_reviews = 0
        for r in review_rows(props.reviews + lender_reviews):
            if not con.execute("SELECT 1 FROM match_review WHERE review_id = ?", [r["review_id"]]).fetchone():
                _insert(con, "match_review", {**r, "decision": None, "first_seen_at": now})
                new_reviews += 1
        lookups = request_lookups(con, entities, cfg)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    methods = {}
    for r in props.links:
        methods[r["match_method"]] = methods.get(r["match_method"], 0) + 1
    return {
        "records_linked": len(props.links), "properties": len(props.properties),
        "multi_record_properties": sum(1 for p in props.properties if p["record_count"] > 1),
        "link_methods": methods,
        "loan_histories": len(histories),
        "open_construction_loans": sum(1 for h in histories if h["loan_kind"] == "construction" and h["status"] == "open"),
        "cmbs_loans": len(loan_links),
        "cmbs_cross_source_groups": len({r["loan_group_id"] for r in loan_links if r["match_method"] != "alone"}),
        "lender_names": len(lender_rows),
        "lenders_linked": sum(1 for r in lender_rows if r["rssd_id"]),
        "entities": len(entities),
        "multi_property_entities": sum(1 for e in entities if e["property_count"] > 1),
        "new_reviews": new_reviews,
        "pending_reviews": con.execute("SELECT count(*) FROM match_review WHERE decision IS NULL").fetchone()[0],
        "lookup_requests_added": lookups,
    }
