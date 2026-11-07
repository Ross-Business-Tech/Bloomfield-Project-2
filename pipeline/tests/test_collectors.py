"""
Tests for the shared building blocks (and, as each collector is built, for the collectors).
Run from the pipeline/ folder:   pytest
"""
import json

from collectors.common import config, fetch, files, inbox
from collectors.common.log import RunTracker
from tests.conftest import FakeResponse, FakeSession

DOMAIN = {"seconds_between_requests": 0, "max_retries": 3, "backoff_base_seconds": 0}


# ---- duplicate detection ------------------------------------------------------------
def test_same_file_twice_adds_one_row(con):
    kw = dict(source="recorder", doc_category="mechanics_lien", collector_name="t",
              state="MI", county="Washtenaw County", ext="pdf")
    first = files.store_file(con, b"%PDF same bytes", **kw)
    second = files.store_file(con, b"%PDF same bytes", **kw)
    assert first.status == "new" and second.status == "duplicate"
    assert second.file_id == first.file_id
    assert con.execute("SELECT count(*) FROM raw_files").fetchone()[0] == 1
    assert config.from_stored_path(first.file_path).read_bytes() == b"%PDF same bytes"


def test_file_ids_and_paths(con):
    r = files.store_file(con, b"a", source="recorder", doc_category="mechanics_lien",
                         collector_name="t", state="MI", county="Washtenaw County", ext="pdf")
    assert r.file_id.startswith("washtenaw-lien-") and r.file_id.endswith("-00001")
    assert "/MI/washtenaw/recorder/" in r.file_path or "MI/washtenaw/recorder" in r.file_path


def test_bad_source_is_rejected(con):
    import pytest
    with pytest.raises(ValueError):
        files.store_file(con, b"x", source="made_up", doc_category="other", collector_name="t")


# ---- robots.txt ------------------------------------------------------------------------
def test_robots_disallowed_url_is_skipped(env):
    session = FakeSession({
        "https://example.org/robots.txt": FakeResponse("User-agent: *\nDisallow: /private/", content_type="text/plain"),
        "https://example.org/private/case1": FakeResponse("secret"),
        "https://example.org/public/case1": FakeResponse("hello"),
    })
    assert fetch.polite_get("https://example.org/private/case1", DOMAIN, session=session) is None
    assert "https://example.org/private/case1" not in session.requested   # never even requested
    assert "/private/case1" in fetch.get_robots_skips()["example.org"]
    assert not fetch.needs_manual_inbox("example.org")                    # rest of the site is fine
    ok = fetch.polite_get("https://example.org/public/case1", DOMAIN, session=session)
    assert ok is not None and ok.text == "hello"


# ---- CAPTCHA / login pages -> manual inbox, no retries ----------------------------------
def test_captcha_page_triggers_manual_inbox_without_retries(env):
    session = FakeSession({
        "https://court.example.org/robots.txt": FakeResponse("", 404),
        "https://court.example.org/search": FakeResponse(
            "<html><body>Please verify you are human <div class='g-recaptcha'>captcha</div></body></html>"),
    })
    assert fetch.polite_get("https://court.example.org/search", DOMAIN, session=session) is None
    assert fetch.needs_manual_inbox("court.example.org")
    assert "CAPTCHA" in fetch.get_fallbacks()["court.example.org"]
    assert session.requested.count("https://court.example.org/search") == 1   # no retries


def test_403_never_retried_and_flags_manual_inbox(env):
    session = FakeSession({
        "https://blocked.example.org/robots.txt": FakeResponse("", 404),
        "https://blocked.example.org/a": FakeResponse("denied", 403),
    })
    assert fetch.polite_get("https://blocked.example.org/a", DOMAIN, session=session) is None
    assert session.requested.count("https://blocked.example.org/a") == 1
    assert fetch.needs_manual_inbox("blocked.example.org")


def test_500_is_retried_then_succeeds(env):
    session = FakeSession({
        "https://flaky.example.org/robots.txt": FakeResponse("", 404),
        "https://flaky.example.org/a": [FakeResponse("err", 503), FakeResponse("err", 503), FakeResponse("fine")],
    })
    resp = fetch.polite_get("https://flaky.example.org/a", DOMAIN, session=session)
    assert resp is not None and resp.text == "fine"
    assert session.requested.count("https://flaky.example.org/a") == 3


def test_normal_page_with_signin_link_is_not_blocked(env):
    session = FakeSession({
        "https://ok.example.org/robots.txt": FakeResponse("", 404),
        "https://ok.example.org/a": FakeResponse("<html><a href='/login'>Sign in</a> results...</html>"),
    })
    assert fetch.polite_get("https://ok.example.org/a", DOMAIN, session=session) is not None


def test_missing_contact_email_refuses_to_fetch(env, monkeypatch):
    monkeypatch.delenv("CONTACT_EMAIL")
    assert fetch.polite_get("https://example.org/a", DOMAIN, session=FakeSession({})) is None


def test_rate_limit_waits_between_requests(env, monkeypatch):
    waits = []
    monkeypatch.setattr(fetch, "_sleep", waits.append)
    t = {"now": 100.0}
    monkeypatch.setattr(fetch, "_clock", lambda: t["now"])
    session = FakeSession({
        "https://slow.example.org/robots.txt": FakeResponse("", 404),
        "https://slow.example.org/a": FakeResponse("a"),
        "https://slow.example.org/b": FakeResponse("b"),
    })
    settings = {"seconds_between_requests": 3, "max_retries": 0}
    fetch.polite_get("https://slow.example.org/a", settings, session=session)
    fetch.polite_get("https://slow.example.org/b", settings, session=session)
    assert waits and all(abs(w - 3) < 1e-6 for w in waits)


# ---- manual inbox -------------------------------------------------------------------------
def test_inbox_loads_files_with_sidecar_and_skips_duplicates(con, env):
    con.execute("INSERT INTO county_registry VALUES ('MI','Washtenaw County','26161','not_started',NULL,NULL)")
    root = config.inbox_dir() / "recorder" / "MI" / "washtenaw" / "mechanics_lien"
    root.mkdir(parents=True)
    (root / "lien1.pdf").write_bytes(b"%PDF lien one")
    (root / "lien1.pdf.json").write_text(json.dumps(
        {"url": "https://example.org/l1", "instrument_number": "2026-123", "city": "Ann Arbor",
         "doc_type": "Claim of Lien"}))
    (root / "zcopy.pdf").write_bytes(b"%PDF lien one")        # identical content -> duplicate

    with RunTracker(con, "inbox.py") as run:
        totals = inbox.load_inbox(con, run)
    assert totals == {"new": 1, "skipped": 1, "errors": 0}
    row = con.execute("SELECT source, doc_category, state, county, city, url, source_record_id, notes "
                      "FROM raw_files").fetchone()
    assert row == ("recorder", "mechanics_lien", "MI", "Washtenaw County", "Ann Arbor",
                   "https://example.org/l1", "2026-123", "doc type: Claim of Lien")
    assert not (root / "lien1.pdf").exists()                  # moved out of the inbox
    assert (config.inbox_dir() / "_duplicates").exists()
    assert con.execute("SELECT status, files_new, files_skipped FROM collector_runs").fetchone() == ("ok", 1, 1)

    # Running again loads nothing new.
    assert inbox.load_inbox(con) == {"new": 0, "skipped": 0, "errors": 0}


def test_inbox_unknown_source_folder_is_reported_not_crashed(con, env):
    bad = config.inbox_dir() / "nonsense" / "MI"
    bad.mkdir(parents=True)
    (bad / "x.pdf").write_bytes(b"x")
    assert inbox.load_inbox(con)["errors"] == 1


def test_run_tracker_survives_a_crash(con):
    with RunTracker(con, "boom") as run:
        raise RuntimeError("kaboom")
    assert con.execute("SELECT status, errors FROM collector_runs").fetchone() == ("failed", 1)
