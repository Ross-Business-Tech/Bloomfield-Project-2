"""The whole weekly run with the AI doing the reading (a pretend AI service and pretend websites; no network)."""
from datetime import date

import pytest

from bot import full, opening
from scorer import weekly
from scout_reader_schema import create_scout_reader_tables
from tests.b_fixtures import bcon, fake_llm, make_cfg  # noqa: F401
from tests.conftest import FakeResponse, FakeSession

TODAY = date(2026, 11, 1)
URL = "https://riverbendlofts.example.org/"
PAGE = ("<html><body><h1>Riverbend Lofts</h1><p>Student housing steps from campus, with private bedrooms, a rooftop terrace, "
        "a fitness center and study lounges on every floor.</p><p>Opening Fall 2025.</p>"
        "<p>Coming soon! Join the waitlist to hear first about leasing events and tours of the model apartment.</p>"
        "<p>Riverbend Lofts is developed by Riverbend Partners and managed by a national student housing operator.</p>"
        "</body></html>")
ROBOTS = FakeResponse("User-agent: *\nAllow: /", content_type="text/plain")
BOT_CFG = {"limits": {"max_pages_per_run": 10, "max_search_queries": 0}, "search": {"provider": "none", "skip_domains": []},
           "enabled_signals": ["opening_date"],
           "opening_date": {"watchlist": [{"project_name": "Riverbend Lofts", "url": URL, "city": "Ann Arbor", "state": "MI",
                                           "property_type": "student_housing"}],
                            "queries": [], "opened_markers": ["now open"], "unopened_markers": ["coming soon"]}}


@pytest.fixture
def real_data_only(monkeypatch):
    monkeypatch.delenv("BLOOMFIELD_INCLUDE_DEMO", raising=False)      # a real run: sample properties are left out


def site():
    return FakeSession({"https://riverbendlofts.example.org/robots.txt": ROBOTS, URL: FakeResponse(PAGE, url=URL)})


def ai_reading(user_text):
    return {"filing_type": "project_website", "project_name": "Riverbend Lofts", "stated_opening_phrase": "Opening Fall 2025.",
            "page_still_unopened": True, "property_type": "student_housing", "confidence": "high",
            "quotes": {"project_name": "Riverbend Lofts", "stated_opening_phrase": "Opening Fall 2025."}}


def test_the_ai_reads_the_crawled_page_and_the_property_is_scored(bcon, fake_llm, real_data_only):
    create_scout_reader_tables(bcon)
    fake_llm.reply = ai_reading
    out = full.run_full(bcon, BOT_CFG, today=TODAY, session=site())
    assert out["ai"]["on"] and out["crawl"]["opening_date"]["saved"] == 1        # the crawler only saved the page
    assert out["read"]["ok"] == 1 and len(fake_llm.calls) == 1                   # the AI read it (one call)
    assert out["projects"]["updated"] == 1
    p = bcon.execute("SELECT stated_opening_phrase, stale_candidate, months_overdue, notes FROM projects").fetchone()
    assert p[0] == "Opening Fall 2025." and p[1] and p[2] >= 10 and "AI reader" in p[3]
    assert bcon.execute("SELECT count(*) FROM project_snapshots").fetchone()[0] == 1
    # the same address can appear in more than one group (score 0 sibling); the RANKED row is the result
    flag = bcon.execute("SELECT property_name, score, shown, rank FROM weekly_flags WHERE run_id = ? "
                        "ORDER BY score DESC LIMIT 1", [out["run_id"]]).fetchone()
    assert flag[0] == "Riverbend Lofts" and flag[1] > 0 and flag[2] and flag[3] == 1   # ranked, with a score from the equation


def test_without_an_ai_key_the_run_still_works_and_says_so(bcon, monkeypatch, real_data_only):
    for n in ("OPENAI_API_KEY", "LLM_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openai_responses")
    ready, why = full.ai_ready()
    assert not ready and "OPENAI_API_KEY" in why
    out = full.run_full(bcon, BOT_CFG, today=TODAY, session=site())
    assert not out["ai"]["on"] and "OPENAI_API_KEY" in out["read"]["skipped"]
    p = bcon.execute("SELECT stated_opening_phrase, stale_candidate FROM projects").fetchone()   # keyword reading as the fallback
    assert p[0] and "Fall 2025" in p[0] and p[1]


def test_no_ai_switch_skips_the_ai_even_when_a_key_exists(bcon, fake_llm, real_data_only):
    out = full.run_full(bcon, BOT_CFG, today=TODAY, session=site(), use_ai=False)
    assert not out["ai"]["on"] and fake_llm.calls == [] and "projects" not in out


def test_sample_properties_never_appear_in_a_real_list(bcon, real_data_only):
    from matcher.schema import create_matcher_tables
    from scorer.schema import create_scorer_tables
    from tests.test_weekly import _add
    create_matcher_tables(bcon)
    create_scorer_tables(bcon)
    weekly.create_tables(bcon)
    for pid, name, addr in [("p-real", "Riverbend Lofts", "1 Real St"), ("fk-1", "FAKE Sample Tower", "2 Fake St"),
                            ("p-named", "FAKE Another", "3 Fake St")]:
        _add(bcon, "recorded_loans", record_id=f"r-{pid}", loan_kind="construction", recording_date=date(2021, 1, 1),
             property_address=addr, state="MI", amount=20e6, grantee="Bank", grantor="Owner LLC")
        _add(bcon, "projects", project_id=pid, project_name=name, property_address=addr, state="MI",
             property_type="multifamily", months_overdue=0.0, stale_candidate=False)
    run_id = weekly.run(bcon, None, TODAY)
    names = {r[0] for r in bcon.execute("SELECT property_name FROM weekly_flags WHERE run_id = ?", [run_id]).fetchall()}
    assert names == {"Riverbend Lofts"}
    run2 = weekly.run(bcon, None, TODAY, include_demo=True)                    # the demo database opts in explicitly
    assert len(bcon.execute("SELECT * FROM weekly_flags WHERE run_id = ?", [run2]).fetchall()) == 3


def test_apply_readings_ignores_failed_readings(bcon):
    create_scout_reader_tables(bcon)
    assert opening.apply_readings(bcon, TODAY) == {"updated": 0, "no_date": 0}
