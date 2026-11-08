"""Tests for the Trepp loader: column mapping, validation, inbox handling, privacy."""
import json
import shutil
from datetime import date
from pathlib import Path

import pytest
import requests

from collectors import trepp
from collectors.common import config
from collectors.common.log import RunTracker
from collectors.common.parse import load_yaml

SAMPLE = Path(__file__).parent / "fixtures" / "loan_screen_sample.csv"
CFG = load_yaml(trepp.MAPPING_PATH)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Trepp data must never go anywhere: any web request in these tests is a failure."""
    def boom(*a, **k):
        raise AssertionError("the Trepp loader tried to use the network")
    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests, "post", boom)


def drop(name="screen.csv", src=SAMPLE):
    folder = config.inbox_dir() / "trepp"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, folder / name)
    return folder / name


def loans(con):
    return {r[0]: r for r in con.execute(
        "SELECT loan_key, property_name, property_state, current_balance, original_balance, maturity_date, "
        "dscr, occupancy, payment_status, servicer_name, watchlist_flag, servicer_commentary, report_period, "
        "source, file_id FROM loans ORDER BY loan_key").fetchall()}


def test_column_mapping_on_the_sample_file(con, env):
    drop()
    with RunTracker(con, "trepp.py") as run:
        trepp.load_inbox(con, run)
    rows = loans(con)
    # 3 good loans; the 4th row has no loan ID, so it is skipped and logged as an error
    assert set(rows) == {"trepp:FAKE 2019-C1:1001", "trepp:FAKE 2019-C1:1002", "trepp:FAKE 2021-B7:0042"}
    assert len(run.errors) == 1 and "row 7" in run.errors[0] and run.status == "partial"
    a = rows["trepp:FAKE 2019-C1:1001"]
    assert a[1:3] == ("Maple Plaza", "IL")
    assert a[3] == 24_100_000 and a[4] == 25_000_000            # "$25,000,000" and "24,100,000.00"
    assert a[5] == date(2027, 7, 1) and a[6] == 0.92            # "0.92x"
    assert a[7] == pytest.approx(0.78)                          # "78%" -> fraction
    assert a[8] == "0" and a[9] == "Sample Servicing Co"
    assert a[10] is True and "anchor tenant vacated" in a[11]   # watchlist Y + commentary
    assert a[12] == date(2026, 10, 15) and a[13] == "trepp"
    b = rows["trepp:FAKE 2019-C1:1002"]
    assert b[10] is False and b[11] is None and b[7] == pytest.approx(0.91)   # "91" -> 0.91


def test_file_is_saved_unchanged_as_trepp_source_and_inbox_is_cleared(con, env):
    path = drop()
    original = path.read_bytes()
    with RunTracker(con, "trepp.py") as run:
        trepp.load_inbox(con, run)
    row = con.execute("SELECT source, doc_category, file_type, file_path, state FROM raw_files").fetchone()
    assert row[:3] == ("trepp", "other", "csv") and row[4] is None
    assert config.from_stored_path(row[3]).read_bytes() == original
    assert not path.exists()
    assert con.execute("SELECT status, files_new FROM collector_runs").fetchone() == ("partial", 1)


def test_same_file_twice_loads_once(con, env):
    drop("a.csv")
    with RunTracker(con, "trepp.py") as run:
        trepp.load_inbox(con, run)
    drop("b.csv")                                                # identical bytes, different name
    with RunTracker(con, "trepp.py") as run:
        trepp.load_inbox(con, run)
    assert run.files_skipped == 1 and run.files_new == 0
    assert con.execute("SELECT count(*) FROM raw_files").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM loans").fetchone()[0] == 3
    assert (config.inbox_dir() / "trepp" / "_duplicates" / "b.csv").exists()


def test_a_crash_between_saving_and_loading_is_repaired_on_rerun(con, env):
    path = drop()
    # pretend the file was stored but the loans were never written
    from collectors.common.files import store_file
    store_file(con, path.read_bytes(), source="trepp", doc_category="other", collector_name="x", ext=".csv")
    with RunTracker(con, "trepp.py") as run:
        trepp.load_inbox(con, run)
    assert con.execute("SELECT count(*) FROM loans").fetchone()[0] == 3


def test_dscr_at_or_above_one_is_kept_but_warned(con, env, caplog):
    path = config.inbox_dir() / "trepp" / "s.csv"
    path.parent.mkdir(parents=True)
    path.write_text("Deal Name,Loan ID,Property Name,DSCR,Distribution Date\n"
                    "FAKE 2020-X,1,Low Place,0.9,10/15/2026\nFAKE 2020-X,2,High Place,1.05,10/15/2026\n")
    with caplog.at_level("WARNING"):
        with RunTracker(con, "trepp.py") as run:
            trepp.load_inbox(con, run)
    assert loans(con)["trepp:FAKE 2020-X:2"][6] == 1.05 and len(loans(con)) == 2   # kept
    assert any("1 of 2 loans have DSCR of 1.00 or higher" in m and "screen may have changed" in m
               for m in caplog.messages)


def test_missing_columns_are_warned_and_left_blank(con, env, caplog):
    csv_text = "Deal Name,Loan ID,Property Name,DSCR\nFAKE 2020-X,7,Some Place,0.8\n"
    path = config.inbox_dir() / "trepp" / "s.csv"
    path.parent.mkdir(parents=True)
    path.write_text(csv_text)
    with caplog.at_level("WARNING"):
        with RunTracker(con, "trepp.py") as run:
            trepp.load_inbox(con, run, as_of=date(2026, 9, 30))
    assert any("expected columns not found" in m and "maturity_date" in m for m in caplog.messages)
    row = loans(con)["trepp:FAKE 2020-X:7"]
    assert row[5] is None and row[6] == 0.8 and row[12] == date(2026, 9, 30)      # --as-of used


def test_excel_files_work_and_title_rows_are_skipped(con, env, tmp_path):
    df = trepp.read_table(SAMPLE)
    folder = config.inbox_dir() / "trepp"
    folder.mkdir(parents=True)
    df.to_excel(folder / "screen.xlsx", header=False, index=False)
    with RunTracker(con, "trepp.py") as run:
        trepp.load_inbox(con, run)
    assert len(loans(con)) == 3 and run.files_new == 1


def test_report_date_from_sidecar_and_newest_report_wins(con, env):
    csv_text = "Deal Name,Loan ID,Property Name,DSCR\nFAKE 2020-X,7,Some Place,0.8\n"
    folder = config.inbox_dir() / "trepp"
    folder.mkdir(parents=True)
    (folder / "new.csv").write_text(csv_text)
    (folder / "new.csv.json").write_text(json.dumps({"report_period": "2026-10-31"}))
    with RunTracker(con, "trepp.py") as run:
        trepp.load_inbox(con, run)
    (folder / "old.csv").write_text(csv_text.replace("0.8", "0.7"))
    with RunTracker(con, "trepp.py") as run:
        trepp.load_inbox(con, run, as_of=date(2026, 8, 31))        # an OLDER report arrives later
    row = loans(con)["trepp:FAKE 2020-X:7"]
    assert row[12] == date(2026, 10, 31) and row[6] == 0.8         # the newer report is kept


def test_bad_file_is_logged_and_does_not_stop_the_others(con, env):
    folder = config.inbox_dir() / "trepp"
    folder.mkdir(parents=True)
    (folder / "a_broken.csv").write_text("nothing,useful\n1,2\n")
    shutil.copy(SAMPLE, folder / "b_good.csv")
    with RunTracker(con, "trepp.py") as run:
        trepp.load_inbox(con, run)
    assert run.status == "partial" and len(run.errors) == 2 and len(loans(con)) == 3   # broken file + skipped row
    assert (folder / "a_broken.csv").exists()                      # left for a person to fix


def test_empty_inbox_is_fine(con, env):
    with RunTracker(con, "trepp.py") as run:
        assert trepp.load_inbox(con, run) == {"files": 0, "errors": 0}
