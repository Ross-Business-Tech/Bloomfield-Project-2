from sqlalchemy import text

from collectors import state
from collectors.common import count_rows
from conftest import FIXTURES

S = FIXTURES / "state"
CMS_META = "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/4pq5-n9py"
CMS_CSV = "https://data.cms.gov/provider-data/sites/default/files/resources/fake/NH_ProviderInfo_Oct2026.csv"
CKAN = "https://data.chhs.ca.gov/api/3/action/package_show?id=healthcare-facility-locations"
CA_CSV = "https://data.chhs.ca.gov/fake/facilities.csv"


def routes(with_ca=True):
    r = {CMS_META: (S / "fake_cms_metastore.json").read_bytes(), CMS_CSV: (S / "fake_cms_provider_info.csv").read_bytes()}
    if with_ca:
        r[CKAN] = (S / "fake_ckan_package.json").read_bytes()
        r[CA_CSV] = (S / "fake_ca_facilities.csv").read_bytes()
    return r


def records(engine):
    with engine.connect() as conn:
        return {r["record_id"]: dict(r) for r in conn.execute(text("SELECT * FROM licensing_records")).mappings()}


def test_cms_and_state_end_to_end(engine, fake_client):
    stats = state.run(fake_client(routes()), engine, today="2026-11-06")
    recs = records(engine)
    maple = recs["CMS:015001"]  # leading zeros kept
    assert maple["distress_flag"] == 1 and maple["as_of_date"] == "2026-10-01"
    assert "Special Focus: SFF" in maple["distress_reason"] and "fines $250,000" in maple["distress_reason"]
    assert "1-star" in maple["distress_reason"] and maple["legal_name"] == "FAKE MAPLE OPCO LLC"
    assert recs["CMS:015002"]["distress_flag"] == 0
    assert recs["CMS:015003"]["distress_reason"] == "Special Focus: SFF Candidate"
    assert recs["STATE:CA:900000003"]["distress_reason"] == "license status: SUSPENDED"
    assert recs["STATE:CA:900000001"]["distress_flag"] == 0
    assert recs["STATE:CA:900000002"]["as_of_date"] == "2026-11-06"  # no date column -> run date
    with engine.connect() as conn:
        raw = {r["source"]: dict(r) for r in conn.execute(text("SELECT * FROM raw_files")).mappings()}
    assert set(raw) == {"CMS", "STATE:CA"} and raw["STATE:CA"]["state"] == "CA"
    assert stats.rows_written == 6 and not stats.errors


def test_rerun_is_idempotent(engine, fake_client):
    state.run(fake_client(routes()), engine, today="2026-11-06")
    stats = state.run(fake_client(routes()), engine, today="2026-11-07")
    assert stats.files_skipped_duplicate == 2 and stats.rows_written == 0
    assert count_rows(engine, "licensing_records") == 6 and count_rows(engine, "raw_files") == 2


def test_failing_source_does_not_stop_others(engine, fake_client):
    stats = state.run(fake_client(routes(with_ca=False)), engine, today="2026-11-06")
    assert len(stats.errors) == 1 and "california" in stats.errors[0]
    assert count_rows(engine, "licensing_records", "source = 'CMS'") == 3


def test_page_kind_is_saved_for_the_reader(engine, fake_client, tmp_path):
    cfg = tmp_path / "s.yaml"
    cfg.write_text("sources:\n  - {name: tx_page, kind: page, source: 'STATE:TX', state: TX, url: 'https://ex.gov/actions.html'}\n")
    state.run(fake_client({"https://ex.gov/actions.html": b"<html>notices</html>"}), engine, config_path=cfg)
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT * FROM raw_files")).mappings().one()
    assert raw["source"] == "STATE:TX" and raw["needs_reading"] == 1
    assert count_rows(engine, "licensing_records") == 0
