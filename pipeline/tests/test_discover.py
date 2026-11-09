"""Tests for the website discovery tool. Pretend directory and sites; no network; every name is FAKE."""
import pytest
import yaml

from collectors import discover
from collectors.common import db
from tests.conftest import FakeResponse, FakeSession

DIR = "https://directory.example.org"
COUNTY_PAGE = DIR + "/state/XX/county/fakeland"
OK_SITE = "https://records.fakeland.example.gov/"
BOT_SITE = "https://deeds.botty.example.gov/"
LOGIN_SITE = "https://court.fakeland.example.gov/"
ROBOTS_SITE = "https://closed.example.gov/search"

CFG = {
    "directories": {"fake_dir": {"enabled": True, "terms_checked": True,
                                 "county_url": DIR + "/state/{state}/county/{county_slug}",
                                 "skip_domains": ["directory.example.org", "facebook.com"]}},
    "classify": {"recorder": ["recorder", "deeds"], "court": ["court"], "public_notice": ["public notice"],
                 "assessor": ["assessor"]},
    "terms_red_flags": ["automated", "scrap", "robot"],
}

DIRECTORY_HTML = f"""<html><body>
<a href="/about">About us</a>
<a href="https://facebook.com/x">Facebook</a>
<a href="{OK_SITE}">Fakeland County Recorder</a>
<a href="{OK_SITE}">Fakeland County Recorder</a>
<a href="{BOT_SITE}">Register of Deeds</a>
<a href="{LOGIN_SITE}">Circuit Court case search</a>
<a href="{ROBOTS_SITE}">Public Notices</a>
<a href="https://www.fakeland.example.gov/">County home page</a>
</body></html>"""


def terms_page(text):
    return FakeResponse(f"<html><body>{text}</body></html>")


def site(link_to_terms=True):
    t = '<a href="/terms">Terms of Use</a>' if link_to_terms else ""
    return FakeResponse(f"<html><body><h1>Records search</h1>{t}</body></html>")


def pages():
    return {
        DIR + "/robots.txt": FakeResponse("User-agent: *\nAllow: /", content_type="text/plain"),
        COUNTY_PAGE: FakeResponse(DIRECTORY_HTML),
        "https://records.fakeland.example.gov/robots.txt": FakeResponse("User-agent: *\nAllow: /", content_type="text/plain"),
        OK_SITE: site(),
        "https://records.fakeland.example.gov/terms": terms_page("You may search records. Please be kind."),
        "https://deeds.botty.example.gov/robots.txt": FakeResponse("User-agent: *\nAllow: /", content_type="text/plain"),
        BOT_SITE: site(),
        "https://deeds.botty.example.gov/terms": terms_page("Automated access and scraping are prohibited."),
        "https://court.fakeland.example.gov/robots.txt": FakeResponse("User-agent: *\nAllow: /", content_type="text/plain"),
        LOGIN_SITE: FakeResponse("<html><body>Please verify you are human (CAPTCHA)</body></html>"),
        "https://closed.example.gov/robots.txt": FakeResponse("User-agent: *\nDisallow: /search", content_type="text/plain"),
    }


@pytest.fixture
def cand(con):
    discover.ensure_table(con)
    return con


def test_the_directory_must_be_approved_by_a_person_first(cand):
    cfg = {"directories": {"fake_dir": {"enabled": True, "terms_checked": False, "county_url": "x"}}}
    with pytest.raises(discover.DirectoryNotApproved):
        discover.discover_county(cand, cfg, "fake_dir", "XX", "Fakeland County", session=FakeSession({}))
    assert cand.execute("SELECT count(*) FROM website_candidates").fetchone()[0] == 0


def test_county_links_are_found_classified_and_deduplicated(cand):
    sess = FakeSession(pages())
    rows = discover.discover_county(cand, CFG, "fake_dir", "XX", "Fakeland County", session=sess)
    kinds = {r["url"]: r["kind"] for r in rows}
    assert kinds[OK_SITE] == "recorder" and kinds[BOT_SITE] == "recorder" and kinds[LOGIN_SITE] == "court"
    assert kinds[ROBOTS_SITE] == "public_notice"
    assert not any("facebook" in u or "directory.example.org" in u for u in kinds)
    assert len(rows) == 5                         # the duplicate link is saved once
    # running again adds nothing
    assert discover.discover_county(cand, CFG, "fake_dir", "XX", "Fakeland County", session=FakeSession(pages())) == []


def test_screening_never_gets_around_anything(cand):
    sess = FakeSession(pages())
    discover.discover_county(cand, CFG, "fake_dir", "XX", "Fakeland County", session=sess)
    counts = discover.screen_pending(cand, CFG, session=sess)
    status = dict(cand.execute("SELECT url, screen_status FROM website_candidates").fetchall())
    assert status[OK_SITE] == "allowed_candidate"
    assert status[BOT_SITE] == "needs_person"          # the terms forbid automated access
    assert status[LOGIN_SITE] == "manual_inbox"        # CAPTCHA wall: stop, never solve
    assert status[ROBOTS_SITE] == "manual_inbox"       # robots.txt disallows it
    assert counts["allowed_candidate"] == 1
    reasons = dict(cand.execute("SELECT url, screen_reason FROM website_candidates").fetchall())
    assert "scrap" in reasons[BOT_SITE] and "robots" in reasons[ROBOTS_SITE]


def test_a_site_with_no_terms_page_goes_to_a_person(cand):
    p = pages()
    p[OK_SITE] = site(link_to_terms=False)
    sess = FakeSession(p)
    status, reason = discover.screen_site(OK_SITE, CFG, sess)
    assert status == "needs_person" and "terms" in reason


def test_proposed_files_are_manual_inbox_and_never_automatic(cand, tmp_path):
    sess = FakeSession(pages())
    discover.discover_county(cand, CFG, "fake_dir", "XX", "Fakeland County", session=sess)
    discover.screen_pending(cand, CFG, session=sess)
    files = discover.write_proposals(cand, tmp_path / "proposed")
    assert len(files) == 1
    doc = yaml.safe_load(files[0].read_text(encoding="utf-8"))
    assert doc["state"] == "XX" and set(doc["sources"]) == {"recorder", "court", "public_notice"}
    assert all(s["access"] == "manual_inbox" for s in doc["sources"].values())
    assert OK_SITE in doc["sources"]["recorder"]["notes"]


def test_the_shipped_settings_keep_the_directory_locked():
    cfg = discover.load_config()
    d = cfg["directories"]["netr_online"]
    assert d["terms_checked"] is False             # a person must flip this after reading the terms
    assert "automated" in cfg["terms_red_flags"]
