"""Tests for the screenshare demo: it builds from the real pipeline, shows the right things, works offline, leaks nothing."""
import json
import re
import sys
from pathlib import Path

import pytest

from demo import build as demo_build
from demo import record as demo_record


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Build the demo once (it runs the real pipeline on fake data in a scratch folder) and load its embedded data."""
    import os
    saved = os.environ.get("BLOOMFIELD_DATA_DIR")
    os.environ["GEMINI_API_KEY"] = "not-a-real-key-must-never-appear-in-the-page"
    root = tmp_path_factory.mktemp("demo_build")
    out = root / "demo.html"
    try:
        data = demo_build.build(out, root / "demo_data", log=lambda s: None)
    finally:
        os.environ.pop("GEMINI_API_KEY", None)
        if saved is None:
            os.environ.pop("BLOOMFIELD_DATA_DIR", None)
        else:
            os.environ["BLOOMFIELD_DATA_DIR"] = saved
    html = out.read_text(encoding="utf-8")
    embedded = json.loads(re.search(r"const DATA = (.*?);\n", html, re.S).group(1))
    return {"html": html, "data": data, "embedded": embedded, "out": out}


def test_the_page_embeds_the_data_and_nothing_is_left_to_fill_in(built):
    assert "/*__DEMO_DATA__*/" not in built["html"]
    assert built["embedded"]["summary"]["properties"] == 49 and built["out"].stat().st_size > 50_000


def test_the_ann_arbor_project_is_the_top_hot_flag_with_quoted_evidence(built):
    props = built["embedded"]["properties"]
    top = props[0]
    assert top["name"] == "FAKE Maize Row Student Lofts" and top["tier"] == "hot" and top["score"] == 100
    assert [p["tier"] for p in props].count("hot") == 10 and [p["tier"] for p in props].count("watch") >= 15 and top["type"] == "student_housing"
    names = {s["signal"] for s in top["signals"]}
    assert {"construction_loan_no_takeout", "stale_opening", "mechanics_lien", "lender_nonaccruals_rising"} <= names
    lien = next(s for s in top["signals"] if s["signal"] == "mechanics_lien")
    assert lien["quote_found"] and lien["doc_id"] in built["embedded"]["documents"]
    assert "1,240,000" in built["embedded"]["documents"][lien["doc_id"]]["text"]
    assert top["loans"][0]["status"] == "open" and top["bank"]["series"][-1]["rising_two_quarters"]
    assert top["borrower"] == "Maize Row Holdings FAKE LLC" and top["timeline"]


def test_office_properties_are_identified_so_the_page_can_hide_them(built):
    offices = [p for p in built["embedded"]["properties"] if p["type"] == "office"]
    assert sorted(p["name"] for p in offices) == ["FAKE Camelback Office Plaza", "FAKE Capitol Square Offices", "FAKE Uptown Office Park"]
    assert 'p.type === \'office\'' in built["html"] and "Show office" in built["html"]     # hidden unless ticked


def test_the_ai_reader_tab_shows_real_recorded_answers_with_checks_run_on_them(built):
    reader = {r["file_id"]: r for r in built["embedded"]["reader"]}
    assert set(reader) == {"fake-annarbor-construction-mortgage", "fake-annarbor-modification",
                           "fake-annarbor-hvac-lien", "fake-annarbor-permit"}
    for fid in ("fake-annarbor-construction-mortgage", "fake-annarbor-hvac-lien", "fake-annarbor-permit"):
        assert reader[fid]["status"] == "ok"
        assert all(f["quote_found"] for f in reader[fid]["fields"] if f["quote"])
    bad = reader["fake-annarbor-modification"]               # the model took a city from our context block, not the document
    assert bad["status"] == "needs_review" and "quote not found for property_city" in bad["reason"]
    assert all(r["model"].startswith("gemini") and r["document_text"] for r in reader.values())


def test_sources_tab_is_honest_about_what_is_manual_or_unverified(built):
    by_name = {s["name"]: s for s in built["embedded"]["sources"]}
    counties = next(s for n, s in by_name.items() if "sample counties" in n)
    assert counties["status"] == "manual" and "Washtenaw County" in counties["note"]
    assert by_name["SEC EDGAR (CMBS loans)"]["status"] == "unverified"
    assert by_name["FFIEC call reports (bank health)"]["status"] == "unverified"
    assert by_name["Trepp screen (DSCR under 1.0x)"]["status"] == "manual"


def test_the_page_works_offline_and_loads_nothing_from_the_internet(built):
    html = built["html"]
    assert not re.search(r"""(src|href)\s*=\s*["']?https?:""", html)
    assert not re.search(r"url\(\s*['\"]?https?:|@import|<link\b|<script[^>]+\bsrc=|\bfetch\(|XMLHttpRequest|WebSocket", html)


def test_no_secret_or_real_data_leaks_into_the_page(built):
    html = built["html"]
    assert "not-a-real-key" not in html and "API_KEY" not in html
    assert not re.search(r"AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{20,}", html)
    assert "FAKE" in html and built["embedded"]["summary"]["documents"] == 120


def test_data_cannot_break_out_of_the_script_tag(tmp_path):
    out = tmp_path / "x.html"
    demo_build.render({"note": "</script><img src=x onerror=alert(1)>"}, out)
    html = out.read_text(encoding="utf-8")
    assert html.count("</script>") == demo_build.TEMPLATE.read_text(encoding="utf-8").count("</script>")
    assert "<\\/script>" in html


def test_the_demo_will_only_wipe_a_demo_folder(tmp_path):
    victim = tmp_path / "important_data"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    with pytest.raises(ValueError, match="demo"):
        demo_build._reset(victim)
    assert (victim / "keep.txt").exists()


def test_recorded_readings_are_fake_and_hold_no_keys():
    text = demo_build.RECORDED.read_text(encoding="utf-8")
    records = json.loads(text)
    assert len(records) == 4 and all("fake" in r["file_id"] and "FAKE DOCUMENT" in r["document_text"] for r in records)
    assert not re.search(r"AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{20,}", text)


def test_recording_refuses_documents_not_marked_fake(con):
    con.execute("INSERT INTO raw_files (file_id, source, doc_category, file_path, file_hash, file_type) "
                "VALUES ('real-looking-doc', 'recorder', 'other', 'x', 'h', 'txt')")
    with pytest.raises(SystemExit, match="not marked FAKE"):
        demo_record.record(con, ["real-looking-doc"])


def test_demo_command_explains_itself_when_the_page_is_missing(tmp_path, monkeypatch):
    from demo import __main__ as cli
    monkeypatch.setattr(sys, "argv", ["demo", "--no-build", "--out", str(tmp_path / "missing.html")])
    with pytest.raises(SystemExit, match="python -m demo"):
        cli.main()


# ---- the client-call version: light by default, guided walkthrough, results and asks --------------------------------
def test_the_page_opens_in_light_mode_even_if_the_computer_is_in_dark_mode(built):
    html = built["html"]
    assert '<html lang="en" data-theme="light">' in html                  # light is the default
    assert "prefers-color-scheme" not in html                             # the computer's dark mode never overrides it
    assert 'id="th-light"' in html and 'id="th-dark"' in html and ':root[data-theme="dark"]' in html


def test_the_walkthrough_covers_every_part_of_the_call_in_order(built):
    html = built["html"]
    steps = re.findall(r"\{ part: '([^']+)', title: '((?:[^'\\]|\\.)+)'", html)
    assert len(steps) == 14
    parts = [p for p, _ in steps]
    assert parts == sorted(parts, key=["Our build", "Demo", "Results", "Feedback", "Ship"].index)      # agenda order
    assert [t.replace("\\'", "'") for _, t in steps][0] == "Bloomfield's problem"
    assert any("Nov 17" in t for _, t in steps) and any("working" in t for _, t in steps)


def test_the_shared_content_is_embedded_and_honest(built):
    c = built["embedded"]["content"]
    assert c["event"]["client"] == "Bloomfield Capital" and len(c["asks"]) == 4
    assert {s["status"] for s in c["signals"]} <= {"coded", "in_progress", "not_built"} and len(c["signals"]) == 9
    assert c["results"]["live_reader"]["ok"] + c["results"]["live_reader"]["review"] + c["results"]["live_reader"]["failed"] == \
        c["results"]["live_reader"]["documents"] == 18
    assert "Not measured yet" in c["results"]["accuracy"]                  # we do not claim an accuracy we have not measured
    assert any(w["status"] == "Need from you" for w in c["weak_spots"]) and len(c["ships"]["in"]) >= 3
    assert any("outreach" in x.lower() for x in c["ships"]["later"])
    assert [s["when"] for s in c["next_steps"]][-1] == "NOV 17"


def test_the_flags_page_does_not_call_sample_readings_real_ai_output(built):
    assert "documents turned into fields (sample readings" in built["html"]
    assert "read by the AI Reader (" not in built["html"]


def test_the_recorded_numbers_match_what_the_demo_actually_built(built):
    s, r = built["embedded"]["summary"], built["embedded"]["content"]["results"]["pipeline"]
    assert (s["documents"], s["records_linked"], s["properties"], s["hot"], s["watch"]) == \
        (r["documents"], r["linked"], r["properties"], r["hot"], r["watch"])
