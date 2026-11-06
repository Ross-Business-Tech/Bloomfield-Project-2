from datetime import date

from sqlalchemy import text

from collectors import county
from collectors.common import count_rows
from conftest import FIXTURES

C = FIXTURES / "county"
BASE = "https://clerk.example.gov"
LIST = BASE + "/notices/list.html"
ROBOTS = b"User-agent: *\nDisallow: /private/\n"
CFG = f"""
counties:
  - name: fakeville_tx
    state: TX
    base_url: "{BASE}"
    pages: ["/notices/list.html"]
    fetch: links
    link_pattern: "(?i)\\\\.pdf($|\\\\?)"
    next_text_pattern: "^next"
  - name: walled_il
    state: IL
    base_url: "https://walled.example.gov"
    pages: ["/search"]
    fetch: page
  - name: snapshot_az
    state: AZ
    base_url: "https://notices.example.gov"
    pages: ["/today?from={{since:%m/%d/%Y}}"]
    fetch: page
  - name: disabled_fl
    state: FL
    enabled: false
    base_url: "https://never.example.gov"
    pages: ["/"]
    fetch: page
"""


def routes():
    pdf = lambda n: f"%PDF-1.4 fake notice {n}".encode()  # noqa: E731
    return {
        BASE + "/robots.txt": ROBOTS,
        LIST: (C / "sample_listing.html").read_bytes(),
        LIST + "?page=2": (C / "sample_listing_page2.html").read_bytes(),
        BASE + "/notices/2026/11/FAKE-2026-0001.pdf": pdf(1),
        BASE + "/notices/2026/11/FAKE-2026-0002.PDF": pdf(2),
        BASE + "/notices/2026/11/FAKE-2026-0004.pdf": pdf(4),
        "https://files.example.gov/receivership/FAKE-2026-0003.pdf": pdf(3),
        "https://walled.example.gov/search": (C / "login_page.html").read_bytes(),
        "https://notices.example.gov/today?from=10/01/2026": b"<html><body>Notice list for today</body></html>",
    }


def write_cfg(tmp_path):
    p = tmp_path / "counties.yaml"
    p.write_text(CFG)
    return p


def test_template_parses_saved_sample_page():
    html = (C / "sample_listing.html").read_text()
    links = county.extract_links(html, LIST, r"(?i)\.pdf($|\?)")
    assert [l.url for l in links] == [
        BASE + "/notices/2026/11/FAKE-2026-0001.pdf",
        BASE + "/notices/2026/11/FAKE-2026-0002.PDF",  # relative link resolved, #fragment duplicate dropped
        "https://files.example.gov/receivership/FAKE-2026-0003.pdf",
        BASE + "/private/FAKE-2026-0099.pdf",
    ]
    assert links[2].text == "Receivership order"
    assert county.find_next_page(html, LIST, "^next") == LIST + "?page=2"
    only_sales = county.extract_links(html, LIST, r"\.pdf", link_text_pattern="notice of sale")
    assert len(only_sales) == 2


def test_end_to_end_saves_filings_and_skips_blocked(engine, fake_client, tmp_path):
    stats = county.run(fake_client(routes()), engine, date(2026, 10, 1), date(2026, 10, 31), config_path=write_cfg(tmp_path))
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(text("SELECT * FROM raw_files ORDER BY source_url")).mappings()]
    fake = [r for r in rows if r["source"] == "COUNTY:fakeville_tx"]
    assert len(fake) == 4  # 3 on page 1 (robots blocks /private/) + 1 on page 2
    assert all(r["state"] == "TX" and r["needs_reading"] == 1 and r["local_path"].endswith(".pdf") for r in fake)
    snap = [r for r in rows if r["source"] == "COUNTY:snapshot_az"]
    assert len(snap) == 1 and snap[0]["local_path"].endswith(".html")
    assert not any(r["source"] == "COUNTY:walled_il" for r in rows)
    assert len(stats.errors) == 1 and "login or CAPTCHA" in stats.errors[0]  # walled_il skipped, others continued
    assert not any("never.example.gov" in c for c in stats.errors)


def test_rerun_is_idempotent(engine, fake_client, tmp_path):
    cfg = write_cfg(tmp_path)
    county.run(fake_client(routes()), engine, date(2026, 10, 1), date(2026, 10, 31), config_path=cfg)
    client = fake_client(routes())
    stats = county.run(client, engine, date(2026, 10, 1), date(2026, 10, 31), config_path=cfg)
    assert count_rows(engine, "raw_files") == 5
    assert stats.files_downloaded == 0 and stats.files_skipped_duplicate == 5
    assert not any(c.endswith(".pdf") or c.endswith(".PDF") for c in client.session.calls)  # PDFs not re-requested


def test_robots_disallow_whole_county(engine, fake_client, tmp_path):
    r = routes()
    r[BASE + "/robots.txt"] = b"User-agent: *\nDisallow: /\n"
    stats = county.run(fake_client(r), engine, date(2026, 10, 1), date(2026, 10, 31),
                       config_path=write_cfg(tmp_path), only=["fakeville_tx"])
    assert "robots.txt disallows" in stats.errors[0] and count_rows(engine, "raw_files") == 0


def test_shipped_config_has_five_counties_in_five_states():
    from collectors.common import load_yaml

    counties = load_yaml(county.CONFIG_PATH)["counties"]
    assert len(counties) >= 5 and len({c["state"] for c in counties}) >= 5
    for c in counties:
        assert {"name", "state", "base_url", "pages", "fetch"} <= set(c)
        assert c["fetch"] in {"links", "page", "direct"}
        if c["fetch"] == "links":
            assert c.get("link_pattern")
