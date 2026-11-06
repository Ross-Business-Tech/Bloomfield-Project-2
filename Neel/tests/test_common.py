import pytest
import requests

from collectors import common
from collectors.common import RunStats, count_rows, download_file, register_bytes, retry, sha256_bytes, upsert


def test_fingerprint_dedupe_same_bytes_registered_once(engine):
    a = register_bytes(engine, b"hello filing", source="COUNTY:Test", state="TX", date="2026-10-01")
    b = register_bytes(engine, b"hello filing", source="COUNTY:Test", state="TX", date="2026-10-01")
    assert a.is_new and b.status == "duplicate"
    assert a.file_id == b.file_id
    assert a.fingerprint == sha256_bytes(b"hello filing")
    assert count_rows(engine, "raw_files") == 1


def test_different_bytes_are_new_files(engine):
    register_bytes(engine, b"one", source="EDGAR")
    register_bytes(engine, b"two", source="EDGAR")
    assert count_rows(engine, "raw_files") == 2


def test_raw_files_row_has_required_columns(engine):
    r = register_bytes(engine, b"x", source="CMS", date="2026-10-01", source_url="https://example.com/a.csv")
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM raw_files")).mappings().one()
    for col in ("file_id", "source", "state", "date", "fingerprint", "local_path", "downloaded_at"):
        assert col in row
    assert row["file_id"] == r.file_id and row["local_path"].endswith(".csv")
    with open(row["local_path"], "rb") as fh:
        assert fh.read() == b"x"


def test_download_dedupes_by_url_and_by_fingerprint(engine, fake_client):
    client = fake_client({"https://ex.gov/a.pdf": b"PDF-A", "https://ex.gov/a-copy.pdf": b"PDF-A"})
    stats = RunStats("t")
    download_file(client, engine, "https://ex.gov/a.pdf", source="COUNTY:X", stats=stats)
    download_file(client, engine, "https://ex.gov/a.pdf", source="COUNTY:X", stats=stats)  # URL seen: no request
    download_file(client, engine, "https://ex.gov/a-copy.pdf", source="COUNTY:X", stats=stats)  # same bytes
    assert client.session.calls == ["https://ex.gov/a.pdf", "https://ex.gov/a-copy.pdf"]
    assert (stats.files_found, stats.files_downloaded, stats.files_skipped_duplicate) == (3, 1, 2)
    assert count_rows(engine, "raw_files") == 1


def test_user_agent_is_sent_and_required(monkeypatch, fake_client):
    client = fake_client({})
    assert client.session.headers["User-Agent"] == "test test@example.com"
    monkeypatch.delenv("USER_AGENT")
    with pytest.raises(RuntimeError, match="USER_AGENT"):
        common.user_agent()


def test_retry_backs_off_exponentially():
    delays, calls = [], {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("boom")
        return "ok"

    assert retry(flaky, attempts=4, sleep=delays.append) == "ok"
    assert delays == [2.0, 4.0]


def test_upsert_is_idempotent(engine):
    row = {"rssd_id": "1", "report_date": "2026-06-30", "bank_name": "Fake Bank", "cre_nonaccrual": 5.0}
    upsert(engine, "bank_stats", [row], ["rssd_id", "report_date"])
    upsert(engine, "bank_stats", [row], ["rssd_id", "report_date"])
    assert count_rows(engine, "bank_stats") == 1


def test_parsers():
    assert common.to_float("$1,234.50") == 1234.5
    assert common.to_float("(12)") == -12
    assert common.to_float("") is None
    assert common.to_iso_date("10/31/2026") == "2026-10-31"
    assert common.to_iso_date("2026-10-31T00:00:00") == "2026-10-31"
    assert common.to_flag("Y") == 1 and common.to_flag("N") == 0 and common.to_flag("") is None
