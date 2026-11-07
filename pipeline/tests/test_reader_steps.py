"""
Tests for the Reader building blocks: OCR, chunking, prompt, checks, and the batch runner.
No network, no API key and no Tesseract needed (the AI and the OCR engine are faked).
"""
import json
from datetime import date

import pytest
from PIL import Image

from collectors.common import config, files, inbox
from llm import cost
from reader import batch, chunk, ocr
from reader.check import find_missing_quotes, normalize_for_quote, validate
from reader.prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_user_text
from reader.schema import (DistressTag, FilingType, LoanKind, PropertyType, values)
from tests.b_fixtures import bcon, fake_llm, make_cfg  # noqa: F401


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_pdf(pages: list[str]) -> bytes:
    """A tiny but valid PDF. A page given as '' has no text (like a scan)."""
    esc = lambda t: t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
            3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    for i, text in enumerate(pages):
        stream = ("BT /F1 12 Tf 14 TL 50 700 Td " + " ".join(f"({esc(l)}) Tj T*" for l in text.split("\n")) + " ET"
                  if text else "")
        objs[4 + 2 * i] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
                           f"/Resources << /Font << /F1 3 0 R >> >> >>").encode()
        objs[5 + 2 * i] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream".encode()
    out, offsets = b"%PDF-1.4\n", {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    size, xref = max(objs) + 1, len(out)
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{offsets[i]:010d} 00000 n \n".encode() for i in range(1, size))
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


class FakeEngine:
    def __init__(self, text="SCANNED TEXT FROM OCR"):
        self.text, self.calls = text, 0

    def ocr_image(self, image):
        self.calls += 1
        return self.text


class BrokenEngine:
    def ocr_image(self, image):
        raise ocr.OcrUnavailableError("Tesseract OCR is not installed")


LIEN_TEXT = ("CLAIM OF LIEN\nClaimant: Acme HVAC Inc.\nAmount claimed: $5,000\n"
             "Property: 1000 Example Way, Ann Arbor, MI\nOwner: Maize Row Holdings LLC\n")


def lien_reply(**over):
    r = {"filing_type": "mechanics_lien", "lien_claimant": "Acme HVAC Inc.", "lien_amount": "$5,000",
         "property_address": "1000 Example Way, Ann Arbor, MI", "confidence": "high",
         "quotes": {"lien_claimant": "Acme HVAC Inc.", "lien_amount": "$5,000",
                    "property_address": "1000 Example Way, Ann Arbor, MI"}}
    r.update(over)
    return r


def add_raw(con, text=LIEN_TEXT, *, source="recorder", category="mechanics_lien", ext="txt",
            notes=None, data=None):
    res = files.store_file(con, data if data is not None else text.encode(), source=source,
                           doc_category=category, collector_name="t", state="MI",
                           county="Washtenaw County", ext=ext, notes=notes)
    return res.file_id


def rows(con):
    return con.execute("SELECT file_id, status, review_reason, filing_type FROM filing_fields "
                       "ORDER BY file_id").fetchall()


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
def test_text_files_are_read_with_page_marker(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello\n\n\n\nworld")
    r = ocr.get_text(p, "txt")
    assert r.text == "[PAGE 1]\nhello\n\nworld" and r.ocr_share == 0


def test_html_is_cleaned_but_keeps_title_and_hero_text(tmp_path):
    p = tmp_path / "a.html"
    p.write_text("""<html><head><title>Maize Row Lofts</title><meta name="description" content="Coming Winter 2026">
        <script>var x=1;</script></head><body><nav>Home About Contact</nav>
        <div class="hero"><h1>Opening Winter 2026</h1><p>Now pre-leasing. Join the waitlist today for the
        best student housing near campus, with studios and shared units available. """ + "More words. " * 20 + """
        </p></div><footer>Copyright</footer></body></html>""")
    r = ocr.get_text(p, "html")
    assert "PAGE TITLE: Maize Row Lofts" in r.text and "Opening Winter 2026" in r.text
    assert "var x" not in r.text and "Home About" not in r.text and "Copyright" not in r.text
    assert r.warnings == []


def test_html_with_no_text_gets_a_warning(tmp_path):
    p = tmp_path / "a.html"
    p.write_text("<html><body><div id='app'></div><script>render()</script></body></html>")
    assert "almost no readable text" in ocr.get_text(p, "html").warnings[0]


def test_pdf_with_text_layer_needs_no_ocr(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(make_pdf(["First page has plenty of readable words in it.",
                            "Second page also has plenty of readable words."]))
    engine = FakeEngine()
    r = ocr.get_text(p, "pdf", engine=engine)
    assert "[PAGE 1]" in r.text and "[PAGE 2]" in r.text and "First page" in r.text
    assert r.page_count == 2 and r.ocr_pages == 0 and engine.calls == 0


def test_scanned_pdf_page_goes_through_ocr(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(make_pdf(["This page has a normal text layer with enough words.", ""]))
    engine = FakeEngine()
    r = ocr.get_text(p, "pdf", engine=engine)
    assert engine.calls == 1 and r.ocr_pages == 1 and r.ocr_share == 0.5
    assert "[PAGE 2]\nSCANNED TEXT FROM OCR" in r.text


def test_images_are_read_with_ocr(tmp_path):
    p = tmp_path / "photo.png"
    Image.new("RGB", (50, 50), "white").save(p)
    r = ocr.get_text(p, "image", engine=FakeEngine("NOW LEASING"))
    assert r.text == "[PAGE 1]\nNOW LEASING" and r.ocr_share == 1.0


def test_missing_tesseract_gives_a_clear_message(monkeypatch):
    import pytesseract

    def boom():
        raise pytesseract.TesseractNotFoundError()
    monkeypatch.setattr(pytesseract, "get_tesseract_version", boom)
    with pytest.raises(ocr.OcrUnavailableError, match="brew install tesseract"):
        ocr.TesseractEngine().ocr_image(object())


def test_unsupported_file_type(tmp_path):
    p = tmp_path / "a.xlsx"
    p.write_bytes(b"x")
    with pytest.raises(ocr.UnsupportedFileError):
        ocr.get_text(p, "xlsx")


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------
def make_doc(n_pages, special=None):
    special = special or {}
    return "\n\n".join(f"[PAGE {i}]\n" + special.get(i, f"filler text page {i} " * 20) for i in range(1, n_pages + 1))


def test_short_documents_are_sent_whole():
    text = make_doc(3)
    assert chunk.select_text(text, 100_000, 40) == (text, None)


def test_long_documents_keep_first_pages_and_keyword_pages():
    doc = make_doc(30, {17: "The borrower signed the mortgage. " * 10, 25: "lien claimant foreclosure sale " * 10})
    out, note = chunk.select_text(doc, max_chars=3000, max_pages=40)
    sent = [int(n) for n in __import__("re").findall(r"\[PAGE (\d+)\]", out)]
    assert sent[:3] == [1, 2, 3] and 17 in sent and 25 in sent and 10 not in sent
    assert sent == sorted(sent) and len(out) <= 3000
    assert note.startswith("truncated: sent pages") and "of 30" in note


def test_max_pages_is_respected():
    doc = make_doc(20, {i: "mortgage lender borrower " * 3 for i in range(4, 21)})
    out, note = chunk.select_text(doc, max_chars=1_000_000, max_pages=5)
    assert out.count("[PAGE ") == 5 and note


def test_one_giant_page_is_cut():
    out, note = chunk.select_text("x" * 5000, 1000, 40)
    assert len(out) == 1000 and "truncated" in note


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------
def test_prompt_lists_every_allowed_value():
    for enum in (FilingType, PropertyType, LoanKind, DistressTag):
        for v in values(enum):
            assert v in SYSTEM_PROMPT
    assert PROMPT_VERSION == "v2" and "Null is always better than a guess" in SYSTEM_PROMPT


def test_user_text_has_context_and_document():
    t = build_user_text({"source": "recorder", "doc_category": "mechanics_lien", "state": "MI",
                         "county": "Washtenaw County", "city": None, "source_medium": "print"}, "DOC BODY")
    assert "source: recorder" in t and "county: Washtenaw County" in t and "source_medium: print" in t
    assert "city" not in t and "DOC BODY" in t


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------
def test_good_answer_is_ok():
    rec, status, reason = validate(lien_reply(), LIEN_TEXT, doc_category="mechanics_lien")
    assert status == "ok" and reason is None and rec.lien_amount == 5000


def test_made_up_quote_is_needs_review():
    bad = lien_reply(quotes={"lien_claimant": "Totally Invented Plumbing Co.", "lien_amount": "$5,000",
                             "property_address": "1000 Example Way, Ann Arbor, MI"})
    rec, status, reason = validate(bad, LIEN_TEXT, doc_category="mechanics_lien")
    assert status == "needs_review" and "quote not found for lien_claimant" in reason
    assert rec is not None                                  # data is kept, just flagged


def test_quote_matching_ignores_case_whitespace_curly_quotes_and_hyphenation():
    doc = "The owner’s  CONSTRUC-\ntION lender\nis “First Bank” — per page [PAGE 2] 3"
    assert find_missing_quotes({"a": "owner's construction lender is \"first bank\" - per page 3"}, doc) == []
    assert find_missing_quotes({"a": "not in there"}, doc) == ["a"]
    assert normalize_for_quote("A  B") == "a b"


def test_value_that_does_not_match_its_quote_is_flagged():
    bad = lien_reply(lien_claimant="Completely Different Company")
    bad["quotes"]["lien_claimant"] = "Acme HVAC Inc."
    _, status, reason = validate(bad, LIEN_TEXT)
    assert status == "needs_review" and "value for lien_claimant does not match its quote" in reason


def test_filled_field_without_a_quote_is_flagged():
    r = lien_reply()
    del r["quotes"]["lien_amount"]
    _, status, reason = validate(r, LIEN_TEXT)
    assert status == "needs_review" and "no supporting quote for lien_amount" in reason


def test_unparseable_answers_fail():
    for junk in ("sorry I cannot", '{"filing_type": "banana"}', '{"filing_type": "other", "loan_amount": "lots"}'):
        rec, status, reason = validate(junk, LIEN_TEXT)
        assert rec is None and status == "failed" and reason


@pytest.mark.parametrize("over,expected", [
    ({"confidence": "low"}, "confidence is low"),
    ({"filing_type": "other"}, "filing_type is other"),
    ({"lien_amount": "$9,000,000,000", "quotes": {"lien_claimant": "Acme HVAC Inc.", "lien_amount": "$5,000",
      "property_address": "1000 Example Way, Ann Arbor, MI"}}, "not a believable amount"),
    ({"lien_amount": None}, "without a lien_amount"),
    ({"recording_date": "1985-01-01"}, "outside 1990-2040"),
    ({"recording_date": "2026-05-01", "maturity_date": "2026-04-01"}, "maturity_date is not after"),
])
def test_warning_signs_need_review(over, expected):
    reply = lien_reply(**over)
    if over.get("recording_date"):
        reply["quotes"]["recording_date"] = "x"
    _, status, reason = validate(reply, LIEN_TEXT)
    assert status == "needs_review" and expected in reason


def test_nothing_identifying_the_property_needs_review():
    r = {"filing_type": "mechanics_lien", "lien_amount": 5000, "lien_claimant": "Acme HVAC Inc.",
         "confidence": "high", "quotes": {"lien_amount": "$5,000", "lien_claimant": "Acme HVAC Inc."}}
    _, status, reason = validate(r, LIEN_TEXT)
    assert "no borrower, developer, project or property address" in reason


def test_poor_scan_needs_review():
    _, status, reason = validate(lien_reply(), LIEN_TEXT, ocr_share=0.8)
    assert status == "needs_review" and "80% of pages needed OCR" in reason


def test_disagreement_with_part_a_label_keeps_ai_answer_but_flags_both():
    rec, status, reason = validate(lien_reply(), LIEN_TEXT, doc_category="foreclosure_notice")
    assert rec.filing_type == FilingType.mechanics_lien and status == "needs_review"
    assert "foreclosure_notice" in reason and "mechanics_lien" in reason


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------
def test_batch_reads_a_file_and_saves_a_row(bcon, fake_llm):
    fid = add_raw(bcon)
    fake_llm.reply = lien_reply()
    s = batch.run(con=bcon, progress=False, quiet=True)
    assert s["processed"] == 1 and s["ok"] == 1 and s["real_calls"] == 1
    row = bcon.execute("SELECT filing_type, lien_claimant, lien_amount, status, model_name, prompt_version, "
                       "input_tokens, output_tokens, est_cost_usd, quotes::TEXT FROM filing_fields "
                       "WHERE file_id = ?", [fid]).fetchone()
    assert row[:6] == ("mechanics_lien", "Acme HVAC Inc.", 5000, "ok", "claude-sonnet-5-5", PROMPT_VERSION)
    assert row[6:8] == (200, 100) and row[8] > 0 and "Acme HVAC" in row[9]
    assert "source: recorder" in fake_llm.calls[0] and "Acme HVAC Inc." in fake_llm.calls[0]


def test_files_part_a_already_parses_are_skipped(bcon, fake_llm):
    for src, cat in [("sec_edgar", "other"), ("ffiec", "other"), ("cms", "other"), ("trepp", "other")]:
        add_raw(bcon, f"{src} file", source=src, category=cat)
    s = batch.run(con=bcon, progress=False, quiet=True)
    assert s["candidates"] == 0 and fake_llm.calls == []


def test_trepp_files_are_read_only_if_config_allows(bcon, fake_llm):
    add_raw(bcon, "trepp loan commentary text goes here, long enough", source="trepp", category="other")
    fake_llm.reply = lien_reply()
    batch.run(con=bcon, progress=False, quiet=True)
    assert fake_llm.calls == []
    batch.run(con=bcon, cfg=make_cfg(trepp_ai_allowed=True), progress=False, quiet=True)
    assert len(fake_llm.calls) == 1


def test_confidential_files_are_never_sent(bcon, fake_llm, env):
    add_raw(bcon, "private deal text one " * 5, source="manual_upload", category="other",
            notes=f"{inbox.CONFIDENTIAL_MARKER} redacted sample")
    # a file whose saved sidecar says confidential
    fid = add_raw(bcon, "private deal text two " * 5, source="manual_upload", category="other")
    saved = config.from_stored_path(bcon.execute("SELECT file_path FROM raw_files WHERE file_id=?", [fid]).fetchone()[0])
    saved.with_name(saved.name + ".meta.json").write_text(json.dumps({"confidential": True}))
    add_raw(bcon, "public text three " * 5, source="manual_upload", category="other")
    fake_llm.reply = lien_reply()
    s = batch.run(con=bcon, progress=False, quiet=True)
    assert s["skipped_confidential"] == 2 and s["processed"] == 1 and len(fake_llm.calls) == 1
    assert all("private deal" not in c for c in fake_llm.calls)
    # only an explicit switch in config lets them through
    s = batch.run(con=bcon, cfg=make_cfg(bloomfield_material_ai_allowed=True), progress=False, quiet=True)
    assert s["skipped_confidential"] == 0 and s["processed"] == 2


def test_bloomfield_inbox_folder_loads_files_marked_confidential(bcon, env):
    d = config.inbox_dir() / "manual_upload" / "bloomfield" / "MI" / "washtenaw"
    d.mkdir(parents=True)
    (d / "deal.txt").write_text("redacted sample deal text")
    plain = config.inbox_dir() / "manual_upload" / "MI"
    plain.mkdir(parents=True)
    (plain / "public.txt").write_text("a public document")
    sidecar_file = plain / "flagged.txt"
    sidecar_file.write_text("flagged by sidecar")
    (plain / "flagged.txt.json").write_text(json.dumps({"confidential": True, "notes": "from client"}))
    totals = inbox.load_inbox(bcon)
    assert totals == {"new": 3, "skipped": 0, "errors": 0}
    got = {r[0]: r for r in bcon.execute("SELECT notes, state, county, source FROM raw_files").fetchall()}
    conf = [r for r in got.values() if (r[0] or "").startswith(inbox.CONFIDENTIAL_MARKER)]
    assert len(conf) == 2 and all(batch.is_confidential({"notes": r[0]}) for r in conf)
    assert any(r[1] == "MI" and r[3] == "manual_upload" for r in conf)
    assert [r for r in got.values() if r[0] is None]       # the public one is not marked


def test_same_file_read_twice_makes_one_ai_call(bcon, fake_llm):
    fid = add_raw(bcon)
    fake_llm.reply = lien_reply()
    batch.run(con=bcon, progress=False, quiet=True)
    bcon.execute("DELETE FROM filing_fields WHERE file_id = ?", [fid])       # pretend we lost the row
    s = batch.run(con=bcon, progress=False, quiet=True)
    assert len(fake_llm.calls) == 1 and s["cached_calls"] == 1 and s["cost_usd"] == 0
    assert rows(bcon)[0][1] == "ok"


def test_batch_is_restartable_and_never_rereads(bcon, fake_llm):
    for i in range(3):
        add_raw(bcon, LIEN_TEXT + f"Instrument {i}\n")
    fake_llm.reply = lien_reply()
    assert batch.run(limit=2, con=bcon, progress=False, quiet=True)["processed"] == 2
    assert batch.run(con=bcon, progress=False, quiet=True)["processed"] == 1
    assert batch.run(con=bcon, progress=False, quiet=True)["candidates"] == 0
    assert len(fake_llm.calls) == 3


def test_new_prompt_version_rereads_everything(bcon, fake_llm, monkeypatch):
    add_raw(bcon)
    fake_llm.reply = lien_reply()
    batch.run(con=bcon, progress=False, quiet=True)
    monkeypatch.setattr(batch, "PROMPT_VERSION", "v3")
    assert batch.run(con=bcon, progress=False, quiet=True)["processed"] == 1
    assert len(fake_llm.calls) == 2
    assert bcon.execute("SELECT prompt_version FROM filing_fields").fetchone()[0] == "v3"


def test_bad_json_is_retried_once_with_the_error_then_ok(bcon, fake_llm):
    add_raw(bcon)
    fake_llm.queue = ["Sorry, here is some text", lien_reply()]
    s = batch.run(con=bcon, progress=False, quiet=True)
    assert s["ok"] == 1 and len(fake_llm.calls) == 2
    assert "Your previous reply could not be used" in fake_llm.calls[1]


def test_bad_json_twice_is_marked_failed(bcon, fake_llm):
    add_raw(bcon)
    fake_llm.reply = "no json at all"
    s = batch.run(con=bcon, progress=False, quiet=True)
    assert s["failed"] == 1 and len(fake_llm.calls) == 2
    assert rows(bcon)[0][1] == "failed" and "JSON" in rows(bcon)[0][2]


def test_made_up_quote_ends_up_needs_review_in_the_table(bcon, fake_llm):
    add_raw(bcon)
    fake_llm.reply = lien_reply(quotes={"lien_claimant": "Invented Co", "lien_amount": "$5,000",
                                        "property_address": "1000 Example Way, Ann Arbor, MI"})
    batch.run(con=bcon, progress=False, quiet=True)
    assert rows(bcon)[0][1] == "needs_review" and "quote not found for lien_claimant" in rows(bcon)[0][2]


def test_unreadable_scan_is_not_saved_so_it_retries_later(bcon, fake_llm, tmp_path):
    add_raw(bcon, "", ext="pdf", category="other", data=make_pdf(["", ""]))
    fake_llm.reply = lien_reply()
    s = batch.run(con=bcon, engine=BrokenEngine(), progress=False, quiet=True)
    assert s["not_processed"] == 1 and s["processed"] == 0 and rows(bcon) == [] and fake_llm.calls == []
    s = batch.run(con=bcon, engine=FakeEngine(LIEN_TEXT), progress=False, quiet=True)   # tesseract now "installed"
    assert s["processed"] == 1 and len(fake_llm.calls) == 1


def test_blank_documents_are_flagged_without_calling_the_ai(bcon, fake_llm):
    add_raw(bcon, "   ")
    batch.run(con=bcon, progress=False, quiet=True)
    assert fake_llm.calls == [] and rows(bcon)[0][1] == "needs_review" and "no readable text" in rows(bcon)[0][2]


def test_truncated_documents_are_flagged(bcon, fake_llm):
    add_raw(bcon, "[PAGE 1]\n" + LIEN_TEXT + "\n\n".join(f"[PAGE {i}]\nfiller " * 50 for i in range(2, 12)))
    fake_llm.reply = lien_reply()
    cfg = make_cfg()
    cfg["reader"]["max_chars"] = 800
    batch.run(con=bcon, cfg=cfg, progress=False, quiet=True)
    assert rows(bcon)[0][1] == "needs_review" and "truncated: sent pages" in rows(bcon)[0][2]


def test_javascript_only_web_page_is_flagged(bcon, fake_llm):
    add_raw(bcon, "<html><body><div id='app'></div></body></html>", source="project_site",
            category="project_website", ext="html")
    fake_llm.reply = {"filing_type": "project_website", "confidence": "low"}
    batch.run(con=bcon, progress=False, quiet=True)
    assert "almost no readable text" in rows(bcon)[0][2]


def test_cost_cap_stops_the_batch_with_a_clear_message(bcon, fake_llm):
    for i in range(3):
        add_raw(bcon, LIEN_TEXT + f"Instrument {i}\n")
    fake_llm.reply = lien_reply()
    s = batch.run(con=bcon, cfg=make_cfg(weekly_cost_cap_usd=0.001), progress=False, quiet=True)
    assert s["processed"] == 0 and "weekly AI cost cap" in s["stopped"] and fake_llm.calls == []


def test_run_cap_stops_midway_and_keeps_finished_work(bcon, fake_llm):
    for i in range(4):
        add_raw(bcon, LIEN_TEXT + f"Instrument {i}\n")
    fake_llm.reply = lien_reply()
    # each real call costs ~$0.0021; the worst-case estimate before a call is higher, so a $0.04 cap
    # allows a few files and then stops
    s = batch.run(con=bcon, cfg=make_cfg(run_cost_cap_usd=0.045), progress=False, quiet=True)
    assert 0 < s["processed"] < 4 and "per-run AI cost cap" in s["stopped"]
    assert len(rows(bcon)) == s["processed"]


def test_missing_api_key_stops_cleanly(bcon, fake_llm, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    add_raw(bcon)
    s = batch.run(con=bcon, progress=False, quiet=True)
    assert "ANTHROPIC_API_KEY" in s["stopped"] and rows(bcon) == []


def test_ai_service_down_stops_after_three_errors_and_saves_nothing(bcon, fake_llm):
    for i in range(6):
        add_raw(bcon, LIEN_TEXT + f"Instrument {i}\n")
    fake_llm.status = 503
    s = batch.run(con=bcon, progress=False, quiet=True)
    assert s["not_processed"] == 3 and "AI service errors" in s["stopped"] and rows(bcon) == []


def test_filters_by_state_and_source(bcon, fake_llm):
    files.store_file(bcon, LIEN_TEXT.encode(), source="recorder", doc_category="mechanics_lien",
                     collector_name="t", state="IL", county="Cook County", ext="txt")
    add_raw(bcon, LIEN_TEXT + "x")
    fake_llm.reply = lien_reply()
    assert batch.run(state="IL", con=bcon, progress=False, quiet=True)["processed"] == 1
    assert batch.run(source="permit", con=bcon, progress=False, quiet=True)["candidates"] == 0
    assert batch.run(source="recorder", con=bcon, progress=False, quiet=True)["processed"] == 1


def test_retry_failed_option(bcon, fake_llm):
    add_raw(bcon)
    fake_llm.reply = "garbage"
    batch.run(con=bcon, progress=False, quiet=True)
    fake_llm.reply = lien_reply()
    assert batch.run(con=bcon, progress=False, quiet=True)["candidates"] == 0
    s = batch.run(con=bcon, retry_failed=True, progress=False, quiet=True)
    assert s["ok"] == 1
