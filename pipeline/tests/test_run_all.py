"""Tests for run_all: order, isolation of failures, filters, and the summary."""
import pytest

from collectors import run_all
from collectors.common.log import RunTracker


class Spy:
    """Replaces every collector with a fake that records its call."""

    def __init__(self, monkeypatch, con, boom=()):
        self.calls = []
        for name in list(run_all.RUNNERS):
            def runner(c, args, name=name):
                self.calls.append((name, args.state, args.county))
                if name in boom:
                    raise RuntimeError(f"{name} exploded")
                with RunTracker(c, f"{name}.py") as run:
                    run.new_file(2)
                    if name == "county":
                        run.manual_inbox("mi_washtenaw court", "site blocks robots")
                return run
            monkeypatch.setitem(run_all.RUNNERS, name, runner)


def test_runs_everything_in_the_agreed_order(con, monkeypatch):
    spy = Spy(monkeypatch, con)
    results = run_all.run_all(con)
    assert [c[0] for c in spy.calls] == ["inbox", "trepp", "edgar", "ffiec", "county", "state"]
    assert [r["collector"] for r in results] == run_all.ORDER
    assert {r["collector"]: r["status"] for r in results if r["collector"] in run_all.NOT_BUILT} == \
        {"recorded_loans": "not built", "liens": "not built", "permits": "not built"}


def test_one_collector_crashing_does_not_stop_the_others(con, monkeypatch):
    spy = Spy(monkeypatch, con, boom={"edgar"})
    results = {r["collector"]: r for r in run_all.run_all(con)}
    assert results["edgar"]["status"] == "failed" and "exploded" in results["edgar"]["messages"][0]
    assert [c[0] for c in spy.calls][-3:] == ["ffiec", "county", "state"]       # everything after still ran
    assert results["ffiec"]["status"] == "ok"


def test_only_and_county_filters(con, monkeypatch):
    spy = Spy(monkeypatch, con)
    run_all.run_all(con, only=["county"], county_id="mi_washtenaw")
    assert spy.calls == [("county", None, "mi_washtenaw")]
    spy.calls.clear()
    run_all.run_all(con, only=["ffiec", "trepp"])
    assert [c[0] for c in spy.calls] == ["trepp", "ffiec"]                      # always the agreed order


def test_state_flag_skips_national_collectors(con, monkeypatch):
    spy = Spy(monkeypatch, con)
    run_all.run_all(con, state_code="MI")
    assert spy.calls == [("inbox", "MI", None), ("county", "MI", None)]
    spy.calls.clear()
    run_all.run_all(con, only=["edgar"], state_code="MI")                      # unless named explicitly
    assert [c[0] for c in spy.calls] == ["edgar"]


def test_unknown_collector_name_is_a_clear_error(con):
    with pytest.raises(ValueError, match="unknown collector"):
        run_all.run_all(con, only=["nope"])


def test_summary_lists_counts_errors_and_manual_inbox_fallbacks(con, monkeypatch, capsys):
    Spy(monkeypatch, con, boom={"state"})
    run_all.print_summary(run_all.run_all(con))
    out = capsys.readouterr().out
    assert "Collector summary" in out and "recorded_loans" in out and "not built" in out
    assert "[state] crashed: state exploded" in out
    assert "[county] mi_washtenaw court: site blocks robots" in out


def test_real_inbox_runner_loads_waiting_files(con, env):
    from collectors.common import config
    d = config.inbox_dir() / "recorder" / "MI"
    d.mkdir(parents=True)
    (d / "m.pdf").write_bytes(b"%PDF a mortgage")
    results = {r["collector"]: r for r in run_all.run_all(con, only=["inbox"])}
    assert results["inbox"]["new"] == 1 and results["inbox"]["status"] == "ok"
