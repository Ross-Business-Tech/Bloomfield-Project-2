"""Tests for the FFIEC collector, using tiny hand-made call report zips (all banks are FAKE)."""
import io
import zipfile
from datetime import date

import pytest

from collectors import ffiec
from collectors.common import config
from collectors.common.log import RunTracker
from collectors.common.parse import load_yaml

CFG = load_yaml(ffiec.CONFIG_PATH)
Q = {"2025-09-30": "09302025", "2025-12-31": "12312025", "2026-03-31": "03312026", "2026-06-30": "06302026"}

# A description for each stem that mentions the words the audit expects.
DESC = {s: f"{r['measure']} {' '.join(r.get('expect', []))}".replace("_", " ") for s, r in CFG["items"].items()}


def make_zip(qdate, banks, *, prefix="RCON", describe=True, names=None) -> bytes:
    """banks = {rssd_id: {stem: amount in thousands}}. Builds a POR file plus two schedule files."""
    names = names or {}
    stems = sorted({s for vals in banks.values() for s in vals})
    por = "ID\tIDRSSD\tName\tCity\tState\n".replace("ID\t", "Reporting Period End Date\t")
    por = ("Reporting Period End Date\tIDRSSD\tFinancial Institution Name\tFinancial Institution City\t"
           "Financial Institution State\n")
    for rssd in banks:
        n = names.get(rssd, (f"FAKE BANK {rssd}", "FAKETOWN", "MI"))
        por += f'"{qdate}"\t{rssd}\t"{n[0]}"\t"{n[1]}"\t"{n[2]}"\n'
    half = len(stems) // 2
    files = {f"FFIEC CDR Call Bulk POR {Q[qdate]}.txt": por}
    for label, group in (("RCCI", stems[:half]), ("RCN", stems[half:])):
        header = '"IDRSSD"' + "".join(f'\t"{prefix}{s}"' for s in group)
        lines = [header]
        if describe:
            lines.append('""' + "".join(f'\t"{DESC.get(s, "d")}"' for s in group))
        for rssd, vals in banks.items():
            lines.append(rssd + "".join(f"\t{vals.get(s, '')}" for s in group))
        files[f"FFIEC CDR Call Schedule {label} {Q[qdate]}.txt"] = "\n".join(lines) + "\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def bank(total_thousands, nonaccrual_thousands):
    """A bank whose CRE book is `total` and whose nonaccruals are `nonaccrual` (in thousands)."""
    return {"F158": total_thousands * 0.1, "F159": total_thousands * 0.2, "1460": total_thousands * 0.3,
            "F160": total_thousands * 0.2, "F161": total_thousands * 0.2, "F177": nonaccrual_thousands}


def drop(name, data):
    folder = config.inbox_dir() / "ffiec"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)


def zip_name(qdate):
    return f"FFIEC CDR Call Bulk All Schedules {Q[qdate]}.zip"


def stats(con, rssd="111"):
    return con.execute("SELECT report_date, cre_nonaccrual_ratio, nonaccrual_change_qoq, rising_two_quarters "
                       "FROM bank_stats WHERE rssd_id = ? ORDER BY report_date", [rssd]).fetchall()


# ---- the calculations ---------------------------------------------------------------------
def test_bank_row_totals_ratio_and_units():
    vals = {"RCONF158": "1000", "RCONF159": "2000", "RCON1460": "3000", "RCONF160": "2000", "RCONF161": "2000",
            "RCONF173": "50", "RCONF181": "25", "RCONF175": "10",
            "RCONF177": "200", "RCON3501": "100", "RCONF183": "400",
            "bank_name": "FAKE FIRST BANK", "city": "SPRINGFIELD", "state": "IL"}
    row = ffiec.bank_row("111", vals, CFG, date(2026, 6, 30))
    assert row["cre_construction"] == 3_000_000 and row["cre_multifamily"] == 3_000_000     # thousands -> dollars
    assert row["cre_nonfarm_nonres"] == 4_000_000 and row["cre_total"] == 10_000_000
    assert row["cre_nonaccrual"] == 700_000 and row["cre_nonaccrual_ratio"] == pytest.approx(0.07)
    assert row["cre_past_due_30_89"] == 75_000 and row["cre_past_due_90_plus"] == 10_000
    assert (row["bank_name"], row["city"], row["state"]) == ("FAKE FIRST BANK", "SPRINGFIELD", "IL")


def test_zero_or_missing_loans_give_no_ratio_and_no_data_gives_no_row():
    row = ffiec.bank_row("2", {"RCONF158": "0", "RCONF177": "5"}, CFG, date(2026, 6, 30))
    assert row["cre_total"] == 0 and row["cre_nonaccrual_ratio"] is None
    assert ffiec.bank_row("3", {"bank_name": "X"}, CFG, date(2026, 6, 30)) is None


def test_rcon_is_preferred_and_rcfd_is_the_fallback():
    only_rcfd = ffiec.bank_row("1", {"RCFDF158": "100", "RCFD3501": "5"}, CFG, date(2026, 6, 30))
    assert only_rcfd["cre_total"] == 100_000 and only_rcfd["cre_nonaccrual"] == 5_000
    both = ffiec.bank_row("1", {"RCONF158": "100", "RCFDF158": "999"}, CFG, date(2026, 6, 30))
    assert both["cre_total"] == 100_000


def test_trend_math_on_a_hand_made_series():
    d = [date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]
    out = ffiec.compute_trends(list(zip(d, [0.01, 0.02, 0.03, 0.025])))
    assert [c for c, _ in out][0] is None
    assert [round(c, 4) for c, _ in out[1:]] == [0.01, 0.01, -0.005]
    assert [r for _, r in out] == [False, False, True, False]     # up, up -> True; then it fell


def test_a_missing_quarter_or_ratio_breaks_the_chain():
    d = [date(2025, 9, 30), date(2026, 3, 31), date(2026, 6, 30)]          # Dec 2025 is missing
    out = ffiec.compute_trends(list(zip(d, [0.01, 0.02, 0.03])))
    assert out[1][0] is None and out[2][0] == pytest.approx(0.01) and out[2][1] is False
    out = ffiec.compute_trends([(date(2025, 12, 31), 0.01), (date(2026, 3, 31), None), (date(2026, 6, 30), 0.03)])
    assert [c for c, _ in out] == [None, None, None]


# ---- loading ------------------------------------------------------------------------------
def load_all(con, *quarters_data):
    for qdate, banks, kw in quarters_data:
        drop(zip_name(qdate), make_zip(qdate, banks, **kw))
    with RunTracker(con, "ffiec.py") as run:
        ffiec.load_inbox(con, run)
    return run


def test_four_quarters_end_to_end(con, env):
    series = [("2025-09-30", 0.010), ("2025-12-31", 0.020), ("2026-03-31", 0.030), ("2026-06-30", 0.025)]
    data = [(q, {"111": bank(1_000_000, 1_000_000 * r), "222": bank(500_000, 500_000 * 0.004)}, {}) for q, r in series]
    run = load_all(con, *reversed(data))                # dropped newest-first: loader must sort by quarter
    assert run.status == "ok" and run.files_new == 4
    rows = stats(con)
    assert [r[0] for r in rows] == [date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]
    assert [round(r[1], 4) for r in rows] == [0.01, 0.02, 0.03, 0.025]
    assert rows[0][2] is None and rows[2][2] == pytest.approx(0.01) and rows[3][2] == pytest.approx(-0.005)
    assert [r[3] for r in rows] == [False, False, True, False]
    assert not any(r[3] for r in stats(con, "222"))      # a steady bank never trips the flag
    assert con.execute("SELECT bank_name, city, state FROM bank_stats WHERE rssd_id='111' LIMIT 1").fetchone() \
        == ("FAKE BANK 111", "FAKETOWN", "MI")
    f = con.execute("SELECT source, doc_category, file_type, state FROM raw_files").fetchall()
    assert len(f) == 4 and set(f) == {("ffiec", "other", "zip", None)}
    assert not list((config.inbox_dir() / "ffiec").glob("*.zip"))


def test_trends_are_right_even_if_a_middle_quarter_arrives_later(con, env):
    mk = lambda q, r: (q, {"111": bank(1_000_000, 1_000_000 * r)}, {})  # noqa: E731
    load_all(con, mk("2025-09-30", 0.01), mk("2026-03-31", 0.03))
    assert stats(con)[1][2] is None                                       # Dec 2025 missing: no change yet
    load_all(con, mk("2025-12-31", 0.02))                                 # the missing quarter shows up
    assert [r[3] for r in stats(con)] == [False, False, True]


def test_same_quarter_twice_is_skipped(con, env):
    d = ("2026-06-30", {"111": bank(1_000, 10)}, {})
    load_all(con, d)
    run = load_all(con, d)
    assert run.files_skipped == 1 and run.files_new == 0
    assert con.execute("SELECT count(*) FROM bank_stats").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM raw_files").fetchone()[0] == 1


def test_a_crash_between_saving_and_loading_is_repaired(con, env):
    from collectors.common.files import store_file
    data = make_zip("2026-06-30", {"111": bank(1_000, 10)})
    store_file(con, data, source="ffiec", doc_category="other", collector_name="x", ext=".zip")   # saved, never loaded
    drop(zip_name("2026-06-30"), data)
    with RunTracker(con, "ffiec.py") as run:
        ffiec.load_inbox(con, run)
    assert con.execute("SELECT count(*) FROM bank_stats").fetchone()[0] == 1       # loaded now
    assert con.execute("SELECT count(*) FROM raw_files").fetchone()[0] == 1


def test_a_bad_zip_is_logged_and_the_good_one_still_loads(con, env):
    drop("FFIEC CDR Call Bulk All Schedules 03312026.zip", b"this is not a zip")
    drop(zip_name("2026-06-30"), make_zip("2026-06-30", {"111": bank(1_000, 10)}))
    with RunTracker(con, "ffiec.py") as run:
        ffiec.load_inbox(con, run)
    assert run.status == "partial" and len(run.errors) == 1 and len(stats(con)) == 1


def test_zip_without_a_date_in_its_name_is_reported(con, env):
    drop("download.zip", make_zip("2026-06-30", {"111": bank(1_000, 10)}))
    with RunTracker(con, "ffiec.py") as run:
        ffiec.load_inbox(con, run)
    assert "can't tell the quarter" in run.errors[0]


def test_wrong_codes_in_the_files_give_a_clear_error(con, env):
    drop(zip_name("2026-06-30"), make_zip("2026-06-30", {"111": {"ZZ9999": 5}}))
    with RunTracker(con, "ffiec.py") as run:
        ffiec.load_inbox(con, run)
    assert "no CRE figures found" in run.errors[0]


# ---- the code audit --------------------------------------------------------------------------
def test_audit_passes_when_descriptions_match_and_warns_when_they_do_not():
    good = {f"RCON{s}": d for s, d in DESC.items()}
    assert ffiec.audit_codes(good, CFG) == []
    bad = dict(good)
    bad["RCONF180"] = "Past due 90 days or more: other nonfarm nonresidential"        # the wrong meaning
    warnings = ffiec.audit_codes(bad, CFG)
    assert len(warnings) == 1 and "RCONF180" in warnings[0] and "may be wrong" in warnings[0]
    missing = {k: v for k, v in good.items() if k != "RCONF180"}
    assert any("none of RCONF180, RCFDF180 found" in w for w in ffiec.audit_codes(missing, CFG))


def test_audit_runs_when_a_zip_is_loaded(con, env, caplog):
    with caplog.at_level("INFO"):
        load_all(con, ("2026-06-30", {"111": bank(1_000, 10)}, {}))
    assert any("code audit" in m for m in caplog.messages)
