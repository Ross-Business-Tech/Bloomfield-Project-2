"""Tests for the state / CMS / registry collector. Pretend websites only; no network."""
import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from collectors import state
from collectors.common import config
from collectors.common.files import STRUCTURED_MARKER
from collectors.common.log import RunTracker
from collectors.common.parse import load_yaml
from reader import batch
from tests.b_fixtures import fake_llm, make_cfg  # noqa: F401
from tests.conftest import FakeResponse, FakeSession

S = Path(__file__).parent / "fixtures" / "state"
CMS_META = "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/{}"
CMS_CSV = "https://data.cms.gov/provider-data/sites/default/files/resources/fake/NH_ProviderInfo_Oct2026.csv"
ST_CSV = "https://health.example.gov/enforcement.csv"
ST_CSV_TEXT = ("License #,Facility,Action,City\nA1,FAKE CARE ONE,Active,Lansing\n"
               "A2,FAKE CARE TWO,License SUSPENDED,Flint\n")

SOURCES = yaml.safe_load("""
sources:
  - name: cms_provider_info
    kind: cms
    dataset_id: "4pq5-n9py"
    parse: true
    columns:
      license_number: ["CMS Certification Number (CCN)"]
      facility_name: ["Provider Name"]
      legal_name: ["Legal Business Name"]
      city: ["City/Town"]
      state: ["State"]
      status: ["Special Focus Status"]
      as_of_date: ["Processing Date"]
    distress:
      - {column: ["Special Focus Status"], in: ["SFF", "SFF Candidate"], reason: "Special Focus: {value}"}
      - {column: ["Total Amount of Fines in Dollars"], at_least: 100000, reason: "fines ${value}"}
      - {column: ["Overall Rating"], in: ["1"], reason: "1-star overall rating"}
  - name: cms_penalties
    kind: cms
    dataset_id: "g6vv-u9sr"
  - name: mi_enforcement
    kind: csv
    state: MI
    url: "https://health.example.gov/enforcement.csv"
    parse: true
    columns: {license_number: ["License #"], facility_name: ["Facility"], status: ["Action"], city: ["City"]}
    distress:
      - {column: ["Action"], matches: "(?i)suspen|revok", reason: "license status: {value}"}
""")["sources"]


def write_cfg(tmp_path, names=None, extra=()):
    chosen = [s for s in SOURCES if not names or s["name"] in names] + list(extra)
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump({"sources": chosen}))
    return path


def meta(dataset, url):
    return FakeResponse(json.dumps({"distribution": [{"downloadURL": url, "mediaType": "text/csv"}]}),
                        content_type="application/json")


def web(deficiency_ok=True):
    pages = {
        CMS_META.format("4pq5-n9py"): meta("4pq5-n9py", CMS_CSV),
        CMS_CSV: FakeResponse((S / "fake_cms_provider_info.csv").read_text(), content_type="text/csv"),
        CMS_META.format("g6vv-u9sr"): meta("g6vv-u9sr", "https://data.cms.gov/fake/penalties.csv"),
        "https://data.cms.gov/fake/penalties.csv": FakeResponse("CCN,Fine\n015001,5000\n", content_type="text/csv"),
        ST_CSV: FakeResponse(ST_CSV_TEXT, content_type="text/csv"),
    }
    return FakeSession(pages)


def recs(con):
    return {r[0]: r for r in con.execute(
        "SELECT record_id, as_of_date, source, facility_name, legal_name, status, distress_flag, "
        "distress_reason, state, file_id FROM licensing_records ORDER BY record_id").fetchall()}


def test_cms_and_state_csv_end_to_end(con, env, tmp_path):
    run = state.collect(con, session=web(), config_path=write_cfg(tmp_path), registry_path=tmp_path / "none.yaml")
    r = recs(con)
    maple = r["cms:IL:015001"]                                     # leading zeros kept
    assert maple[1] == date(2026, 10, 1) and maple[6] is True
    assert "Special Focus: SFF" in maple[7] and "fines $250,000" in maple[7] and "1-star" in maple[7]
    assert maple[4] == "FAKE MAPLE OPCO LLC"
    assert r["cms:OH:015002"][6] is False and r["cms:OH:015002"][7] is None
    assert r["cms:TX:015003"][7] == "Special Focus: SFF Candidate"
    assert r["state_license:MI:A2"][7] == "license status: License SUSPENDED" and r["state_license:MI:A1"][6] is False
    assert r["state_license:MI:A2"][1] == date.today()             # no date column: the run date
    raw = {x[0]: x for x in con.execute("SELECT notes, source, doc_category, file_type, state FROM raw_files").fetchall()
           if x}
    kinds = sorted((x[1], x[2], x[3], x[4]) for x in raw.values())
    assert kinds == [("cms", "other", "csv", None), ("cms", "other", "csv", None),
                     ("state_license", "license_action", "csv", "MI")]
    assert all(STRUCTURED_MARKER in n for n in raw)                # all three are marked "structured"
    assert run.files_new == 3 and run.status == "ok"
    assert (con.execute("SELECT count(*) FROM licensing_records").fetchone()[0]) == 5


def test_rerun_adds_nothing(con, env, tmp_path):
    cfg = write_cfg(tmp_path)
    state.collect(con, session=web(), config_path=cfg, registry_path=tmp_path / "none.yaml")
    run = state.collect(con, session=web(), config_path=cfg, registry_path=tmp_path / "none.yaml")
    assert run.files_skipped == 3 and run.files_new == 0
    assert con.execute("SELECT count(*) FROM raw_files").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM licensing_records").fetchone()[0] == 5


def test_a_failing_source_does_not_stop_the_others(con, env, tmp_path):
    session = web()
    del session.pages[ST_CSV]                                      # the state site is "down" (404)
    run = state.collect(con, session=session, config_path=write_cfg(tmp_path),
                        registry_path=tmp_path / "none.yaml")
    assert run.status == "partial" and len(run.errors) == 1 and "mi_enforcement" in run.errors[0]
    assert con.execute("SELECT count(*) FROM licensing_records WHERE source = 'cms'").fetchone()[0] == 3


def test_unknown_cms_dataset_gives_a_clear_error(con, env, tmp_path):
    session = web()
    del session.pages[CMS_META.format("g6vv-u9sr")]
    run = state.collect(con, session=session, config_path=write_cfg(tmp_path, ["cms_penalties"]),
                        registry_path=tmp_path / "none.yaml")
    assert "could not read the CMS dataset listing for g6vv-u9sr" in run.errors[0]


def test_page_sources_are_saved_for_the_reader_without_the_structured_marker(con, env, tmp_path):
    page = {"name": "tx_page", "kind": "page", "state": "TX", "url": "https://hhs.example.gov/actions.html"}
    session = FakeSession({page["url"]: FakeResponse("<html>Enforcement notice: facility license revoked</html>")})
    state.collect(con, session=session, config_path=write_cfg(tmp_path, ["none"], [page]),
                  registry_path=tmp_path / "none.yaml")
    row = con.execute("SELECT source, doc_category, file_type, state, notes FROM raw_files").fetchone()
    assert row[:4] == ("state_license", "license_action", "html", "TX") and STRUCTURED_MARKER not in row[4]
    assert con.execute("SELECT count(*) FROM licensing_records").fetchone()[0] == 0


def test_manual_sources_are_reported_not_fetched(con, env, tmp_path):
    manual = {"name": "mi_lara", "kind": "manual", "state": "MI", "notes": "check the LARA site"}
    session = FakeSession({})
    run = state.collect(con, session=session, config_path=write_cfg(tmp_path, ["none"], [manual]),
                        registry_path=tmp_path / "none.yaml")
    assert session.requested == [] and ("mi_lara", "check the LARA site") in run.fallbacks


def test_disabled_sources_are_skipped_unless_asked_for(con, env, tmp_path):
    off = {"name": "off_one", "kind": "manual", "enabled": False, "notes": "x"}
    run = state.collect(con, session=FakeSession({}), config_path=write_cfg(tmp_path, ["none"], [off]),
                        registry_path=tmp_path / "none.yaml")
    assert run.fallbacks == []
    run = state.collect(con, only=["off_one"], session=FakeSession({}),
                        config_path=write_cfg(tmp_path, ["none"], [off]), registry_path=tmp_path / "none.yaml")
    assert len(run.fallbacks) == 1


def test_the_shipped_config_is_safe_by_default():
    cfg = load_yaml(state.CONFIG_PATH)["sources"]
    cms = [s for s in cfg if s["kind"] == "cms"]
    assert len(cms) == 4 and all(s["enabled"] for s in cms)        # open federal data
    states = [s for s in cfg if s.get("state")]
    assert {s["state"] for s in states} == {"MI", "IL", "AZ", "TX", "FL"}
    assert all(s["kind"] == "manual" and s["enabled"] is False for s in states)   # unchecked terms: manual


# ---- the reader must not pay to read structured spreadsheets ---------------------------------
def test_reader_skips_structured_files(con, env, tmp_path, fake_llm):
    from scout_reader_schema import create_scout_reader_tables
    create_scout_reader_tables(con)
    state.collect(con, session=web(), config_path=write_cfg(tmp_path, ["mi_enforcement"]),
                  registry_path=tmp_path / "none.yaml")
    s = batch.run(con=con, progress=False, quiet=True)
    assert s["skipped_structured"] == 1 and fake_llm.calls == []


# ---- business registry -----------------------------------------------------------------------------
def requests(con, rows):
    for name, st, status in rows:
        con.execute("INSERT INTO entity_lookup_requests VALUES (?, ?, 'part_c', now(), ?)", [name, st, status])


def reg_cfg(tmp_path, body):
    p = tmp_path / "registry.yaml"
    p.write_text(body)
    return p


def test_registry_requests_in_manual_states_are_reported_and_stay_pending(con, env, tmp_path):
    requests(con, [("MAIZE ROW HOLDINGS LLC", "MI", "pending"), ("OLD DONE LLC", "MI", "done")])
    with RunTracker(con, "state.py") as run:
        totals = state.lookup_entities(con, run, registry_path=reg_cfg(tmp_path, "states: {}\n"))
    assert totals["manual"] == 1 and totals["done"] == 0
    assert any("business registry MI" in s for s, _ in run.fallbacks)
    assert con.execute("SELECT status FROM entity_lookup_requests WHERE entity_name LIKE 'MAIZE%'").fetchone()[0] == "pending"


def test_registry_open_data_state_is_saved_as_a_business_record(con, env, tmp_path):
    requests(con, [("FAKE HOLDINGS LLC", "XX", "pending"), ("MISSING LLC", "XX", "pending")])
    cfg = reg_cfg(tmp_path, "states:\n  XX:\n    mode: open_data_api\n    url: 'https://data.example.gov/entities.json'\n"
                            "    query_param: q\n")

    class Session(FakeSession):
        def get(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/entities.json"):
                self.requested.append((url, params and params.get("q")))
                if params["q"] == "MISSING LLC":
                    return FakeResponse("nope", 404)
                return FakeResponse(json.dumps([{"name": params["q"], "status": "ACTIVE", "agent": "A. Agent"}]),
                                    content_type="application/json")
            return super().get(url, params, headers, timeout)

    with RunTracker(con, "state.py") as run:
        totals = state.lookup_entities(con, run, session=Session({}), registry_path=cfg)
    assert totals["done"] == 1 and totals["failed"] == 1
    row = con.execute("SELECT source, doc_category, state, source_record_id, file_type FROM raw_files").fetchone()
    assert row == ("state_license", "business_record", "XX", "FAKE HOLDINGS LLC", "json")
    statuses = dict(con.execute("SELECT entity_name, status FROM entity_lookup_requests").fetchall())
    assert statuses == {"FAKE HOLDINGS LLC": "done", "MISSING LLC": "failed"}


def test_registry_is_skipped_when_the_requests_table_is_missing(con, env, tmp_path):
    con.execute("DROP TABLE entity_lookup_requests")
    with RunTracker(con, "state.py") as run:
        assert state.lookup_entities(con, run, registry_path=reg_cfg(tmp_path, "states: {}\n"))["pending"] == 0
