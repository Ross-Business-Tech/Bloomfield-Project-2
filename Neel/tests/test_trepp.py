from pathlib import Path

import pandas as pd
import pytest
import yaml
from sqlalchemy import text

from collectors import trepp
from collectors.common import count_rows
from conftest import FIXTURES

CSV = FIXTURES / "fake_trepp_export.csv"


def _loans(engine):
    with engine.connect() as conn:
        return {r["loan_number"]: dict(r) for r in conn.execute(text("SELECT * FROM loans ORDER BY loan_number")).mappings()}


def test_column_mapping_and_value_parsing(engine):
    stats = trepp.load_trepp(CSV, engine)
    loans = _loans(engine)
    assert set(loans) == {"1001", "1002", "0042"}  # leading zero kept, missing-loan-number row skipped
    a = loans["1001"]
    assert a["loan_id"] == "TREPP:FAKE 2019-C1:1001"
    assert a["source"] == "TREPP" and a["reporting_period"] == "2026-10-15"
    assert a["current_balance"] == 24_100_000.0 and a["original_balance"] == 25_000_000.0
    assert a["interest_rate"] == pytest.approx(0.0475)
    assert a["dscr"] == pytest.approx(0.92) and a["occupancy"] == pytest.approx(0.78)
    assert a["maturity_date"] == "2027-07-01" and a["watchlist"] == 1
    assert a["watchlist_comment"].startswith("DSCR below 1.0x")
    assert loans["1002"]["watchlist"] == 0 and loans["1002"]["interest_rate"] == pytest.approx(0.051)
    assert stats.rows_written == 3 and len(stats.errors) == 1  # the bad row is logged, not fatal


def test_trepp_file_is_registered_as_restricted(engine):
    trepp.load_trepp(CSV, engine)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM raw_files")).mappings().one()
    assert row["source"] == "TREPP" and row["restricted"] == 1 and row["needs_reading"] == 0
    assert row["local_path"] == str(CSV.resolve())  # not copied anywhere
    assert row["date"] == "2026-10-15"


def test_rerun_is_idempotent(engine):
    trepp.load_trepp(CSV, engine)
    stats = trepp.load_trepp(CSV, engine)
    assert stats.files_skipped_duplicate == 1 and stats.rows_written == 0
    assert count_rows(engine, "loans") == 3 and count_rows(engine, "raw_files") == 1


def test_renamed_column_only_needs_config_edit(engine, tmp_path):
    df = trepp.read_table(CSV)
    df.iloc[2] = df.iloc[2].replace({"Current Balance": "Curr Bal As Of Period"})
    renamed = tmp_path / "export_renamed.csv"
    df.to_csv(renamed, header=False, index=False)

    trepp.load_trepp(renamed, engine)
    assert _loans(engine)["1001"]["current_balance"] is None  # unknown header -> blank, logged

    cfg = yaml.safe_load(trepp.MAPPING_PATH.read_text())
    cfg["columns"]["current_balance"].append("Curr Bal As Of Period")
    new_map = tmp_path / "mapping.yaml"
    new_map.write_text(yaml.safe_dump(cfg))
    trepp.load_trepp(renamed, engine, mapping_path=new_map, force=True)
    assert _loans(engine)["1001"]["current_balance"] == 24_100_000.0
    assert count_rows(engine, "loans") == 3


def test_xlsx_export(engine, tmp_path):
    xlsx = tmp_path / "export.xlsx"
    trepp.read_table(CSV).to_excel(xlsx, header=False, index=False)
    trepp.load_trepp(xlsx, engine)
    assert set(_loans(engine)) == {"1001", "1002", "0042"}


def test_cli_entry_point(engine, tmp_path):
    db = f"sqlite:///{tmp_path / 'cli.sqlite'}"
    assert trepp.main([str(CSV), "--db-url", db]) == 0
    assert trepp.main([str(CSV), "--db-url", db]) == 0


def test_trepp_module_never_uses_the_network():
    src = Path(trepp.__file__).read_text()
    assert "requests" not in src and "HttpClient" not in src and "download_file" not in src
