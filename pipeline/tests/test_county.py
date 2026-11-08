"""Tests for the county template collector. Pretend websites; no network; every name is FAKE."""
import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from collectors import county
from collectors.common import config, db
from collectors.common.log import RunTracker
from tests.conftest import FakeResponse, FakeSession

TODAY = date.today()


def d(days_ago):
    return (TODAY - timedelta(days=days_ago)).strftime("%m/%d/%Y")


def listing(rows, next_href=None):
    body = "".join(
        f'<tr class="case"><td class="case-no">{r[0]}</td><td class="filed">{r[1]}</td><td class="parties">{r[2]}</td>'
        f'<td class="type">{r[3]}</td><td class="doc"><a href="{r[4]}">View</a></td></tr>' for r in rows)
    nxt = f'<a class="next" href="{next_href}">Next</a>' if next_href else ""
    return f'<html><body><table class="results">{body}</table>{nxt}</body></html>'


BASE = "https://notices.example.gov"
SEARCH = BASE + "/search?from={}&to={}&page=1"

SCRAPE_SRC = {
    "access": "scrape", "base_url": BASE,
    "search_url": "/search?page={page}",
    "layout": "table",
    "selectors": {"row": "table.results tr.case", "record_id": "td.case-no", "filing_date": "td.filed",
                  "parties": "td.parties", "document_type": "td.type", "document_link": "td.doc a@href"},
    "date_format": "%m/%d/%Y",
    "paging": {"next_selector": "a.next", "max_pages": 5},
    "doc_types": [{"value": "FORECLOSURE", "doc_category": "foreclosure_notice"},
                  {"value": "RECEIVERSHIP", "doc_category": "receivership"}],
    "skip_labels": ["residential", "consumer"],
}


def make_dir(tmp_path, configs):
    folder = tmp_path / "counties"
    folder.mkdir()
    for name, cfg in configs.items():
        (folder / f"{name}.yaml").write_text(yaml.safe_dump(cfg))
    return folder


def washtenaw(**src_over):
    src = {**SCRAPE_SRC, **src_over}
    return {"county": "Washtenaw County", "state": "MI", "fips": "26161", "status": "configured",
            "sources": {"public_notice": src}}


def site(**extra):
    pages = {
        BASE + "/robots.txt": FakeResponse("User-agent: *\nDisallow: /private/", 200, "text/plain"),
        BASE + "/search?page=1": FakeResponse(listing([
            ("2026-CH-0042", d(10), "FAKE FIRST BANK v. MAPLE PLAZA LLC", "FORECLOSURE - COMMERCIAL", "/docs/42.pdf"),
            ("2026-CH-0043", d(12), "FAKE BANK v. J. DOE", "FORECLOSURE - RESIDENTIAL CONSUMER", "/docs/43.pdf"),
            ("2026-CH-0044", d(900), "FAKE LENDER v. OLD LLC", "FORECLOSURE", "/docs/44.pdf"),
            ("2026-CV-0045", d(5), "FAKE v. FAKE", "TRAFFIC", "/docs/45.pdf"),
        ], next_href="/search?page=2")),
        BASE + "/search?page=2": FakeResponse(listing([
            ("2026-CH-0050", d(3), "FAKE RECEIVER v. OAK HOLDINGS LP", "RECEIVERSHIP", "/docs/50.pdf"),
        ])),
        BASE + "/docs/42.pdf": FakeResponse("%PDF-1.4 notice 42", 200, "application/pdf"),
        BASE + "/docs/43.pdf": FakeResponse("%PDF-1.4 notice 43", 200, "application/pdf"),
        BASE + "/docs/44.pdf": FakeResponse("%PDF-1.4 notice 44", 200, "application/pdf"),
        BASE + "/docs/45.pdf": FakeResponse("%PDF-1.4 notice 45", 200, "application/pdf"),
        BASE + "/docs/50.pdf": FakeResponse("%PDF-1.4 notice 50", 200, "application/pdf"),
    }
    pages.update(extra)
    return FakeSession(pages)


def files(con):
    return con.execute("SELECT source, doc_category, state, county, source_record_id, file_type, url, notes "
                       "FROM raw_files ORDER BY source_record_id").fetchall()


# ---- scraping ---------------------------------------------------------------------------------
def test_scrape_keeps_commercial_types_skips_residential_old_and_unwanted(con, env, tmp_path):
    folder = make_dir(tmp_path, {"mi_washtenaw": washtenaw()})
    session = site()
    run = county.collect(con, session=session, config_dir=folder)
    got = files(con)
    assert [(g[0], g[1], g[2], g[3], g[4], g[5]) for g in got] == [
        ("public_notice", "foreclosure_notice", "MI", "Washtenaw County", "2026-CH-0042", "pdf"),
        ("public_notice", "receivership", "MI", "Washtenaw County", "2026-CH-0050", "pdf")]   # page 2 followed
    assert got[0][6] == BASE + "/docs/42.pdf" and "MAPLE PLAZA" in got[0][7]
    for skipped in ("43", "44", "45"):                     # residential, too old, wrong type: never even downloaded
        assert BASE + f"/docs/{skipped}.pdf" not in session.requested
    assert run.status == "ok" and run.files_new == 2


def test_second_run_adds_nothing_and_looks_back_only_14_days(con, env, tmp_path):
    folder = make_dir(tmp_path, {"mi_washtenaw": washtenaw()})
    county.collect(con, session=site(), config_dir=folder)
    start, _ = county.window(con, "mi_washtenaw:public_notice:last_success", None, None, {})
    assert (TODAY - start).days == 14                      # after a successful run: weekly window
    session = site()
    run = county.collect(con, session=session, config_dir=folder)
    assert run.files_new == 0 and run.files_skipped == 2
    assert BASE + "/docs/42.pdf" not in session.requested  # known documents are not fetched again


def test_first_run_looks_back_365_days(con, env):
    start, end = county.window(con, "never:public_notice:last_success", None, None, {})
    assert (end - start).days == 365


def test_robots_disallowed_pages_are_not_fetched(con, env, tmp_path):
    cfg = washtenaw(search_url="/private/search?page={page}")
    run = county.collect(con, session=site(), config_dir=make_dir(tmp_path, {"mi_washtenaw": cfg}))
    assert files(con) == [] and any("washtenaw" in s for s, _ in run.fallbacks)


# ---- broken YAML must not crash the run -----------------------------------------------------------
def test_a_selector_that_matches_nothing_is_logged_and_the_run_continues(con, env, tmp_path):
    bad = washtenaw()
    bad["sources"]["public_notice"]["selectors"] = {**SCRAPE_SRC["selectors"], "record_id": "td.nope"}
    good = {"county": "Cook County", "state": "IL", "fips": "17031", "status": "configured",
            "sources": {"public_notice": {**SCRAPE_SRC, "base_url": BASE}}}
    folder = make_dir(tmp_path, {"a_bad": bad, "b_good": good})
    run = county.collect(con, session=site(), config_dir=folder)
    assert any("a_bad" in e and "record_id" in e and "matched nothing" in e for e in run.errors)
    assert len(files(con)) >= 2 and run.status == "partial"      # the good county still saved its files


def test_invalid_css_selector_is_an_error_not_a_crash(con, env, tmp_path):
    bad = washtenaw()
    bad["sources"]["public_notice"]["selectors"] = {**SCRAPE_SRC["selectors"], "row": "table[[["}
    run = county.collect(con, session=site(), config_dir=make_dir(tmp_path, {"mi_washtenaw": bad}))
    assert run.status in ("failed", "partial") and any("selector" in e for e in run.errors)


def test_a_row_selector_that_matches_nothing_at_all(con, env, tmp_path):
    bad = washtenaw()
    bad["sources"]["public_notice"]["selectors"] = {**SCRAPE_SRC["selectors"], "row": "div.nothing"}
    run = county.collect(con, session=site(), config_dir=make_dir(tmp_path, {"mi_washtenaw": bad}))
    assert any("row selector 'div.nothing' matched nothing" in e for e in run.errors)


def test_unknown_access_mode_and_missing_fields_are_reported(con, env, tmp_path):
    cfg = {"county": "X County", "state": "MI", "sources": {"court": {"access": "telepathy"},
           "recorder": {"access": "scrape"}}}
    run = county.collect(con, session=site(), config_dir=make_dir(tmp_path, {"mi_x": cfg}))
    assert len(run.errors) == 2 and "unknown access mode" in run.errors[0]


# ---- blocked sites fall back to the manual inbox ------------------------------------------------------
def test_blocked_site_falls_back_to_the_inbox_and_loads_that_countys_files(con, env, tmp_path):
    folder = make_dir(tmp_path, {"mi_washtenaw": washtenaw()})
    con.execute("INSERT INTO county_registry VALUES ('MI','Washtenaw County','26161','configured',NULL,NULL)")
    inbox_dir = config.inbox_dir() / "public_notice" / "MI" / "washtenaw"
    inbox_dir.mkdir(parents=True)
    (inbox_dir / "notice.pdf").write_bytes(b"%PDF by hand")
    other = config.inbox_dir() / "public_notice" / "MI" / "wayne"
    other.mkdir(parents=True)
    (other / "other.pdf").write_bytes(b"%PDF someone else's county")
    session = site(**{BASE + "/search?page=1": FakeResponse("denied", 403)})
    run = county.collect(con, session=session, config_dir=folder)
    assert any("403" in r for _, r in run.fallbacks)
    (only,) = files(con)
    assert only[:4] == ("public_notice", "other", "MI", "Washtenaw County") and only[5] == "pdf"
    assert (other / "other.pdf").exists()                  # only Washtenaw's inbox was loaded, not Wayne's


def test_captcha_page_also_falls_back(con, env, tmp_path):
    folder = make_dir(tmp_path, {"mi_washtenaw": washtenaw()})
    session = site(**{BASE + "/search?page=1": FakeResponse("<html>Please complete the CAPTCHA</html>")})
    run = county.collect(con, session=session, config_dir=folder)
    assert any("CAPTCHA" in r for _, r in run.fallbacks) and files(con) == []


def test_manual_inbox_mode_only_loads_the_inbox(con, env, tmp_path):
    cfg = {"county": "Washtenaw County", "state": "MI", "sources": {"recorder": {"access": "manual_inbox"}}}
    folder = make_dir(tmp_path, {"mi_washtenaw": cfg})
    d_ = config.inbox_dir() / "recorder" / "MI" / "washtenaw"
    d_.mkdir(parents=True)
    (d_ / "m.pdf").write_bytes(b"%PDF mortgage")
    session = FakeSession({})
    run = county.collect(con, session=session, config_dir=folder)
    assert session.requested == [] and run.files_new == 1
    assert files(con)[0][:3] == ("recorder", "other", "MI")


# ---- open data APIs ------------------------------------------------------------------------------------
API_URL = "https://data.example.gov/resource/abcd-1234.json"


def api_cfg(fmt="socrata"):
    return {"county": "Cook County", "state": "IL", "fips": "17031", "status": "configured", "sources": {"court": {
        "access": "open_data_api", "base_url": "https://data.example.gov",
        "api": {"format": fmt, "url": API_URL, "params": {"$where": "filed >= '{since:%Y-%m-%d}'"},
                "limit_param": "$limit", "offset_param": "$offset", "page_size": 2, "max_pages": 3,
                **({"records_path": "data.items"} if fmt == "json" else {})},
        "fields": {"record_id": "case_no", "filing_date": "filed", "parties": "who", "document_type": "ctype",
                   "document_link": "doc"},
        "doc_types": [{"value": "foreclosure", "doc_category": "foreclosure_notice"}],
        "skip_labels": ["residential"]}}}


class ApiSession(FakeSession):
    def __init__(self, payloads, **kw):
        super().__init__({"https://data.example.gov/robots.txt": FakeResponse("", 404), **kw})
        self.payloads, self.params = payloads, []

    def get(self, url, params=None, headers=None, timeout=None):
        if url == API_URL:
            self.params.append(dict(params or {}))
            offset = int((params or {}).get("$offset", 0))
            return FakeResponse(json.dumps(self.payloads(offset)), content_type="application/json")
        return super().get(url, params, headers, timeout)


def test_socrata_api_rows_are_saved_as_json_and_paged(con, env, tmp_path):
    iso = lambda n: (TODAY - timedelta(days=n)).isoformat() + "T00:00:00.000"  # noqa: E731
    rows = [{"case_no": "C1", "filed": iso(5), "who": "FAKE BANK v MAPLE LLC", "ctype": "Foreclosure - commercial"},
            {"case_no": "C2", "filed": iso(6), "who": "FAKE BANK v J DOE", "ctype": "Foreclosure - residential"},
            {"case_no": "C3", "filed": iso(7), "who": "FAKE LENDER v OAK LP", "ctype": "Foreclosure", "doc": "/d/c3.pdf"}]
    session = ApiSession(lambda off: rows[off:off + 2], **{"https://data.example.gov/d/c3.pdf":
                                                          FakeResponse("%PDF c3", content_type="application/pdf")})
    run = county.collect(con, session=session, config_dir=make_dir(tmp_path, {"il_cook": api_cfg()}))
    got = {f[4]: f for f in files(con)}
    assert set(got) == {"C1", "C3"}                          # C2 is residential
    assert got["C1"][5] == "json" and got["C3"][5] == "pdf" and got["C3"][0] == "county_court"
    saved = json.loads(config.from_stored_path(con.execute(
        "SELECT file_path FROM raw_files WHERE source_record_id='C1'").fetchone()[0]).read_text())
    assert saved["parties"] == "FAKE BANK v MAPLE LLC" and saved["record_id"] == "C1"
    assert [p["$offset"] for p in session.params] == [0, 2] and session.params[0]["$limit"] == 2
    assert session.params[0]["$where"].startswith("filed >= '") and run.status == "ok"


def test_arcgis_format(con, env, tmp_path):
    cfg = api_cfg("arcgis")
    cfg["sources"]["court"]["api"].pop("offset_param")
    payload = {"features": [{"attributes": {"case_no": "A9", "filed": TODAY.isoformat(), "who": "X v Y",
                                            "ctype": "Foreclosure"}}]}
    county.collect(con, session=ApiSession(lambda off: payload), config_dir=make_dir(tmp_path, {"il_cook": cfg}))
    assert [f[4] for f in files(con)] == ["A9"]


# ---- which configs run -------------------------------------------------------------------------------------
def test_statewide_config_runs_once_for_the_state_and_filters_work(con, env, tmp_path):
    state_cfg = {"statewide": True, "state": "MI", "sources": {"public_notice": {**SCRAPE_SRC}}}
    folder = make_dir(tmp_path, {"mi_washtenaw": washtenaw(), "mi_wayne": {**washtenaw(), "county": "Wayne County"},
                                 "mi_statewide": state_cfg, "il_cook": {**washtenaw(), "state": "IL"}})
    assert [c["id"] for c in county.load_configs(folder, state="MI")] == ["mi_statewide", "mi_washtenaw", "mi_wayne"]
    assert [c["id"] for c in county.load_configs(folder, county="mi_wayne")] == ["mi_wayne"]
    session = site()
    run = county.collect(con, session=session, config_dir=folder, state="MI")      # Illinois' file is left alone
    # the statewide file ran first and saved its documents with no county; the county files point at the
    # same site, so their documents are recognised as already saved (same web address) and skipped
    assert [f[3] for f in files(con)] == [None, None] and run.files_new == 2 and run.files_skipped == 4
    assert session.requested.count(BASE + "/search?page=1") == 3          # statewide once + two counties, not x3 per county


def test_template_files_are_ignored(tmp_path):
    folder = make_dir(tmp_path, {"_template": {"county": "X"}, "mi_x": {"county": "X County", "state": "MI"}})
    assert [c["id"] for c in county.load_configs(folder)] == ["mi_x"]


# ---- the shipped configs ---------------------------------------------------------------------------------------
def test_shipped_configs_cover_the_five_sample_counties_and_are_all_manual_for_now():
    cfgs = {c["id"]: c for c in county.load_configs()}
    fips = {c["fips"]: c["county"] for c in cfgs.values() if c.get("fips")}
    assert fips == {"26161": "Washtenaw County", "17031": "Cook County", "04013": "Maricopa County",
                    "48453": "Travis County", "12086": "Miami-Dade County"}
    assert cfgs["mi_statewide"]["statewide"] is True
    for c in cfgs.values():
        assert c["status"] == "manual_inbox" if not c.get("statewide") else True
        for section, src in c["sources"].items():
            assert section in county.SECTION_SOURCE and src["access"] == "manual_inbox"   # nothing automated until checked


def test_template_is_valid_yaml_with_every_section():
    t = yaml.safe_load((county.COUNTIES_DIR / "_template.yaml").read_text())
    assert set(t["sources"]) == {"court", "recorder", "public_notice"}
    assert t["sources"]["public_notice"]["selectors"]["document_link"].endswith("@href")


# ---- helper scripts ------------------------------------------------------------------------------------------------
def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def registry(con):
    con.execute("INSERT INTO county_registry VALUES ('MI','Washtenaw County','26161','not_started',NULL,NULL), "
                "('FL','Miami-Dade County','12086','not_started',NULL,NULL), "
                "('MD','Baltimore County','24005','not_started',NULL,NULL), "
                "('MD','Baltimore city','24510','not_started',NULL,NULL)")


def test_add_county_script_writes_a_safe_starter_file(con, env, tmp_path):
    add = load_script("add_county")
    registry(con)
    path = add.add_county(con, "26161", None, None, out_dir=tmp_path)
    assert path.name == "mi_washtenaw.yaml"
    cfg = yaml.safe_load(path.read_text())
    assert cfg["county"] == "Washtenaw County" and cfg["fips"] == "26161" and cfg["status"] == "manual_inbox"
    assert all(s["access"] == "manual_inbox" for s in cfg["sources"].values())
    assert add.add_county(con, None, "fl", "Miami-Dade", out_dir=tmp_path).name == "fl_miami_dade.yaml"
    with pytest.raises(ValueError, match="already exists"):
        add.add_county(con, "26161", None, None, out_dir=tmp_path)
    add.add_county(con, "26161", None, None, out_dir=tmp_path, force=True)
    with pytest.raises(ValueError, match="not found in the registry"):
        add.add_county(con, "99999", None, None, out_dir=tmp_path)
    assert add.add_county(con, "24510", None, None, out_dir=tmp_path).name == "md_baltimore_city.yaml"
    # the new file is picked up by the collector without any code change
    assert {c["id"] for c in county.load_configs(tmp_path)} == {"mi_washtenaw", "fl_miami_dade", "md_baltimore_city"}


def test_registry_status_follows_the_yaml_files(con, env, tmp_path):
    reg = load_script("load_county_registry")
    registry(con)
    (tmp_path / "mi_washtenaw.yaml").write_text(yaml.safe_dump({"county": "Washtenaw County", "fips": "26161",
                                                                "status": "configured", "priority": 1}))
    (tmp_path / "fl_miami_dade.yaml").write_text(yaml.safe_dump({"county": "Miami-Dade County", "fips": "12086",
                                                                 "status": "manual_inbox"}))
    (tmp_path / "mi_statewide.yaml").write_text(yaml.safe_dump({"statewide": True, "state": "MI"}))
    assert reg.sync_from_yaml(con, tmp_path) == 2
    status = dict(con.execute("SELECT fips, config_status FROM county_registry").fetchall())
    assert status["26161"] == "configured" and status["12086"] == "manual_inbox" and status["24005"] == "not_started"
