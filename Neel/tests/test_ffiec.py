import zipfile
from datetime import date

import pytest
from sqlalchemy import text

from collectors import ffiec
from collectors.common import count_rows
from conftest import FIXTURES


def make_zip(tmp_path, quarter, stamp):
    z = tmp_path / f"FFIEC CDR Call Bulk All Schedules {stamp}.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in (FIXTURES / "ffiec" / quarter).glob("*.txt"):
            zf.write(f, f.name)
    return z


def stats_by_bank(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM bank_stats ORDER BY report_date")).mappings()
        return {(r["rssd_id"], r["report_date"]): dict(r) for r in rows}


def test_report_date_from_name():
    assert ffiec.report_date_from_name("FFIEC CDR Call Bulk All Schedules 06302026.zip") == "2026-06-30"
    assert ffiec.report_date_from_name("nothing.zip") is None


def test_one_quarter_cre_nonaccrual(engine, tmp_path):
    ffiec.run(engine, zips=[make_zip(tmp_path, "2026Q1", "03312026")])
    b = stats_by_bank(engine)
    first = b[("111", "2026-03-31")]
    assert first["bank_name"] == "FAKE FIRST BANK" and first["state"] == "IL"
    assert first["cre_nonaccrual"] == 400 and first["cre_loans_total"] == 10_000
    assert first["cre_nonaccrual_ratio"] == pytest.approx(0.04)
    assert first["nonaccrual_trend"] is None  # no earlier quarter yet
    assert ("333", "2026-03-31") not in b  # bank with no CRE figures is skipped
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT * FROM raw_files")).mappings().one()
    assert raw["source"] == "FFIEC" and raw["date"] == "2026-03-31"


def test_quarter_over_quarter_trend(engine, tmp_path):
    q2 = make_zip(tmp_path, "2026Q2", "06302026")
    q1 = make_zip(tmp_path, "2026Q1", "03312026")
    ffiec.run(engine, zips=[q2, q1])  # given out of order on purpose
    b = stats_by_bank(engine)
    assert b[("111", "2026-06-30")]["nonaccrual_qoq_change"] == 300
    assert b[("111", "2026-06-30")]["nonaccrual_trend"] == "up"
    assert b[("222", "2026-06-30")]["nonaccrual_trend"] == "flat"


def test_trend_also_right_when_quarters_loaded_in_separate_runs(engine, tmp_path):
    ffiec.run(engine, zips=[make_zip(tmp_path, "2026Q2", "06302026")])
    ffiec.run(engine, zips=[make_zip(tmp_path, "2026Q1", "03312026")])
    assert stats_by_bank(engine)[("111", "2026-06-30")]["nonaccrual_trend"] == "up"


def test_rerun_is_idempotent(engine, tmp_path):
    z = make_zip(tmp_path, "2026Q1", "03312026")
    ffiec.run(engine, zips=[z])
    stats = ffiec.run(engine, zips=[z])
    assert stats.files_skipped_duplicate == 1 and stats.rows_written == 0
    assert count_rows(engine, "bank_stats") == 2 and count_rows(engine, "raw_files") == 1


def test_bad_file_does_not_stop_others(engine, tmp_path):
    bad = tmp_path / "broken 09302026.zip"
    bad.write_bytes(b"not a zip")
    stats = ffiec.run(engine, zips=[bad, make_zip(tmp_path, "2026Q1", "03312026")])
    assert len(stats.errors) == 1 and stats.rows_written == 2


def test_download_by_url(engine, tmp_path, fake_client):
    z = make_zip(tmp_path, "2026Q1", "03312026")
    url = "https://example.gov/bulk/FFIEC%20CDR%20Call%20Bulk%20All%20Schedules%2003312026.zip"
    stats = ffiec.run(engine, urls=[url], client=fake_client({url: z.read_bytes()}))
    assert stats.rows_written == 2
