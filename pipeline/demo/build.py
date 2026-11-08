"""
build.py - builds the screenshare demo: ONE self-contained web page, made from the real pipeline.

In plain English: it runs the same steps the weekly run uses (database tables, FAKE collected data, the AI Reader's
output, Part C matching, Part D scoring) in a separate demo folder, then writes everything the page needs into
demo/demo.html. The page works offline (no internet, no API key, no server): double-click it, or run
    python -m demo --open

Nothing real is involved. Every name, address and number is invented, and the demo never touches your real database
(it uses pipeline/data/demo/). The "AI Reader" tab shows REAL Gemini answers that were recorded earlier
(demo/recorded_readings.json), so there is nothing to call and nothing to pay for.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Callable

PIPELINE = Path(__file__).resolve().parents[1]
DEMO_DIR = Path(__file__).resolve().parent
TEMPLATE = DEMO_DIR / "template.html"
RECORDED = DEMO_DIR / "recorded_readings.json"
CONTENT = DEMO_DIR / "content.json"        # the story, test results and asks (shared with the slide deck)
DEFAULT_OUT = DEMO_DIR / "demo.html"
DEFAULT_DATA = PIPELINE / "data" / "demo"

# The steps of a weekly run, as plain commands (the same ones a person would type).
STEPS = [
    ("Create the shared database tables", ["scripts/setup_db.py"]),
    ("Part A: load FAKE collected data (loans, liens, permits, banks)", ["scripts/make_fake_data.py"]),
    ("Part B: the AI Reader's output (pre-recorded sample readings)", ["scripts/make_fake_readings.py"]),
    ("Part C: link records about the same property, loan, bank and company", ["-m", "matcher"]),
    ("Part D: score and rank every property", ["-m", "scorer"]),
]

CMBS_TYPES = {"OF": "office", "RT": "retail", "MF": "multifamily", "IN": "industrial", "LO": "hotel", "HC": "senior_housing",
              "MU": "mixed_use", "SS": "other", "WH": "industrial", "MH": "multifamily", "SF": "other"}


# ---------------------------------------------------------------------------
# Running the pipeline in an isolated demo folder
# ---------------------------------------------------------------------------
def _reset(data_dir: Path) -> None:
    """Empty the demo folder. Refuses to delete anything that is not clearly a demo folder."""
    if data_dir.name != "demo" and "demo" not in data_dir.name.lower():
        raise ValueError(f"refusing to wipe {data_dir}: the demo data folder must have 'demo' in its name")
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)


def run_pipeline(data_dir: Path, live_limit: int = 0, log: Callable[[str], None] = print) -> list[dict]:
    """Run every step as its own command. Returns [{step, ok, seconds}]. A failing step stops the build."""
    env = {**os.environ, "BLOOMFIELD_DATA_DIR": str(data_dir)}
    steps = list(STEPS)
    results = []
    for title, args in steps:
        log(f"  {title} ...")
        started = datetime.now()
        proc = subprocess.run([sys.executable, *args], cwd=PIPELINE, env=env, capture_output=True, text=True)
        results.append({"step": title, "ok": proc.returncode == 0, "seconds": (datetime.now() - started).total_seconds()})
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout)[-1200:]
            raise RuntimeError(f"demo step failed: {title}\n{tail}")
    if live_limit:
        log(f"  LIVE: the real AI Reader reads the first {live_limit} documents (needs an API key with quota) ...")
        proc = subprocess.run([sys.executable, "-m", "reader.batch", "--limit", str(live_limit), "--no-progress"],
                              cwd=PIPELINE, env=env, capture_output=True, text=True)
        log("    " + (proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "no output"))
        if proc.returncode != 0:
            log("    (the live step failed; the page uses the pre-recorded readings instead)")
        else:
            subprocess.run([sys.executable, "-m", "scorer"], cwd=PIPELINE, env=env, capture_output=True, text=True)
    return results


# ---------------------------------------------------------------------------
# Exporting what the page shows
# ---------------------------------------------------------------------------
def _rows(con, sql: str, params=None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def _clean(row: dict) -> dict:
    return {k: _iso(v) for k, v in row.items()}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _read_text(path_str: str, limit: int = 6000) -> str:
    from collectors.common import config
    try:
        text = config.from_stored_path(path_str).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text if len(text) <= limit else text[:limit] + "\n... (shortened)"


def _property_type(con, pid: str) -> str:
    """Best guess at the property type from the records linked to it (reader first, then the CMBS code)."""
    rows = con.execute(
        "SELECT f.property_type FROM property_links l JOIN filing_fields f ON l.record_table='filing_fields' AND l.record_id=f.file_id "
        "WHERE l.property_id=? AND f.property_type IS NOT NULL AND f.property_type NOT IN ('unknown','other')", [pid]).fetchall()
    if rows:
        return rows[0][0]
    rows = con.execute(
        "SELECT p.property_type FROM property_links l JOIN projects p ON l.record_table='projects' AND l.record_id=p.project_id "
        "WHERE l.property_id=? AND p.property_type IS NOT NULL", [pid]).fetchall()
    if rows:
        return rows[0][0]
    rows = con.execute(
        "SELECT x.property_type FROM property_links l JOIN loans x ON l.record_table='loans' AND l.record_id=x.loan_key "
        "WHERE l.property_id=? AND x.property_type IS NOT NULL", [pid]).fetchall()
    if rows:
        code = str(rows[0][0])
        return CMBS_TYPES.get(code.upper(), code.lower())
    name = (con.execute("SELECT property_name FROM property_scores WHERE property_id=?", [pid]).fetchone() or [""])[0] or ""
    for word, kind in (("office", "office"), ("hotel", "hotel"), ("student", "student_housing"), ("apartment", "multifamily")):
        if word in name.lower():
            return kind
    return "unknown"


def _document(con, table: str, rid: str) -> dict | None:
    """The 'source document' behind a piece of evidence: a saved file, or a plain summary of a table row."""
    if table == "recorded_loans":
        r = con.execute("SELECT file_id FROM recorded_loans WHERE record_id=?", [rid]).fetchone()
        if r and r[0]:
            return _document(con, "filing_fields", r[0])
    if table == "filing_fields":
        r = con.execute("SELECT file_id, source, doc_category, file_path, state, county, city FROM raw_files WHERE file_id=?", [rid]).fetchone()
        if r:
            return {"id": r[0], "kind": "document", "title": f"{(r[2] or 'document').replace('_', ' ')} ({r[1]})",
                    "where": ", ".join(x for x in (r[6], r[5], r[4]) if x), "text": _read_text(r[3])}
    if table == "projects":
        p = _rows(con, "SELECT * FROM projects WHERE project_id=?", [rid])
        if p:
            p = p[0]
            lines = [f"PROJECT PAGE CHECK (Scout) - {p['project_name']}", f"Website: {p['main_url']}",
                     f"Page says: \"{p['stated_opening_phrase']}\"", f"Earliest date we saw: \"{p['first_stated_phrase']}\"",
                     f"Latest reasonable opening deadline: {_iso(p['opening_deadline'])}",
                     f"Page still says coming soon / pre-leasing: {p['page_still_unopened']}",
                     f"Months overdue: {p['months_overdue']}   Date changed {p['date_change_count']} times",
                     f"Student housing missed its August move-in: {p['missed_lease_up']}"]
            return {"id": f"project:{rid}", "kind": "scout", "title": "Scout: opening-date check", "where": p["city"], "text": "\n".join(lines)}
    if table == "bank_stats":
        rows = _rows(con, "SELECT report_date, cre_total, cre_nonaccrual, cre_nonaccrual_ratio, rising_two_quarters, bank_name "
                          "FROM bank_stats WHERE rssd_id=? ORDER BY report_date", [rid])
        if rows:
            lines = [f"FFIEC CALL REPORTS - {rows[0]['bank_name']} (bank id {rid})", "quarter      CRE loans      nonaccrual   ratio   rising 2 qtrs"]
            lines += [f"{_iso(r['report_date'])}  ${r['cre_total'] or 0:>12,.0f}  ${r['cre_nonaccrual'] or 0:>10,.0f}  "
                      f"{(r['cre_nonaccrual_ratio'] or 0) * 100:5.1f}%  {'YES' if r['rising_two_quarters'] else ''}" for r in rows]
            return {"id": f"bank:{rid}", "kind": "table", "title": "FFIEC call reports (lender health)", "where": "", "text": "\n".join(lines)}
    if table == "loans":
        r = _rows(con, "SELECT * FROM loans WHERE loan_key=?", [rid])
        if r:
            r = r[0]
            lines = [f"CMBS LOAN {r['loan_key']}", f"Deal: {r['deal_name']}   Source: {r['source']}", f"Property: {r['property_name']}, {r['property_address']}",
                     f"Balance: ${r['current_balance'] or 0:,.0f}   Maturity: {_iso(r['maturity_date'])}", f"DSCR: {r['dscr']}   Occupancy: {r['occupancy']}",
                     f"Payment status: {r['payment_status']}   Watchlist: {r['watchlist_flag']}", f"Servicer commentary: {r['servicer_commentary'] or '-'}"]
            return {"id": f"loan:{rid}", "kind": "table", "title": "CMBS loan record", "where": r["property_city"] or "", "text": "\n".join(lines)}
    return None


def _timeline(signals: list[dict], histories: list[dict], project: dict | None) -> list[dict]:
    events = []
    for h in histories:
        if h.get("recording_date"):
            events.append({"date": h["recording_date"], "label": f"{(h['loan_kind'] or 'loan').title()} loan recorded"
                           + (f" (${h['amount'] / 1e6:,.1f}M)" if h.get("amount") else ""), "kind": "loan"})
        if h.get("last_modification_date"):
            events.append({"date": h["last_modification_date"], "label": "Loan modified / extended", "kind": "loan"})
        if h.get("takeout_date"):
            events.append({"date": h["takeout_date"], "label": "Permanent loan recorded", "kind": "good"})
        if h.get("release_date"):
            events.append({"date": h["release_date"], "label": "Loan released", "kind": "good"})
    for s in signals:
        if s["evidence_table"] in ("filing_fields",) and s.get("evidence_date"):
            events.append({"date": s["evidence_date"], "label": s["detail"][:90], "kind": s["family"]})
    if project and project.get("opening_deadline"):
        events.append({"date": project["opening_deadline"], "label": f"Stated opening deadline: \"{project['stated_opening_phrase']}\"", "kind": "opening"})
    seen, out = set(), []
    for e in sorted(events, key=lambda e: e["date"]):
        if (e["date"], e["label"]) not in seen:
            seen.add((e["date"], e["label"]))
            out.append(e)
    return out


def reader_examples(recorded: list[dict]) -> list[dict]:
    """Run the Reader's own checks on the recorded real AI answers (the quote check, sanity checks...)."""
    from reader.check import find_missing_quotes, parse_and_validate, validate
    from reader.schema import ExtractionOutput
    out = []
    for rec in recorded:
        record, error = parse_and_validate(rec["reply_text"])
        _, status, reason = validate(rec["reply_text"], rec["document_text"], doc_category=rec["doc_category"])
        fields = []
        if record is not None:
            missing = set(find_missing_quotes(record.quotes, rec["document_text"]))
            for name in ExtractionOutput.model_fields:
                value = getattr(record, name)
                if name in ("quotes", "confidence", "other_parties", "distress_tags") or value in (None, "", []):
                    continue
                value = value.value if hasattr(value, "value") else value
                quote = record.quotes.get(name)
                fields.append({"name": name, "value": _iso(value), "quote": quote,
                               "quote_found": None if quote is None else name not in missing})
        out.append({"file_id": rec["file_id"], "source": rec["source"], "doc_category": rec["doc_category"],
                    "document_text": rec["document_text"], "fields": fields, "status": status, "reason": reason,
                    "parse_error": error, "model": rec["model"], "provider": rec.get("provider"),
                    "input_tokens": rec.get("input_tokens"), "output_tokens": rec.get("output_tokens"),
                    "recorded_on": rec.get("recorded_on"), "confidence": getattr(getattr(record, "confidence", None), "value", None)})
    return out


def sources_table() -> list[dict]:
    """What is automatic, what is manual, and what is not verified yet: straight from the settings files."""
    from collectors.common.parse import load_yaml
    from collectors import county
    rows = []
    cfgs = county.load_configs()
    named = [c for c in cfgs if not c.get("statewide")]
    rows.append({"group": "Counties", "name": f"{len(named)} sample counties + {sum(1 for c in cfgs if c.get('statewide'))} statewide file(s)",
                 "how": "Court, recorder and public-notice sites, driven by one settings file per county",
                 "status": "manual", "note": ", ".join(c["county"] for c in named) + ". Every site is set to manual inbox until a person checks its terms of use."})
    rows.append({"group": "Counties", "name": "All ~3,100 U.S. counties", "how": "County registry + add-county helper (one file per county)",
                 "status": "built", "note": "Adding a county is adding a settings file, not writing code."})
    rows += [
        {"group": "National", "name": "SEC EDGAR (CMBS loans)", "how": "Automatic download, 4 requests/second", "status": "unverified",
         "note": "XML field names still need checking against a real filing (the tool prints an audit)."},
        {"group": "National", "name": "FFIEC call reports (bank health)", "how": "Download the zips by hand, drop in the inbox", "status": "unverified",
         "note": "Item codes still need checking against the official dictionary (the tool prints an audit)."},
        {"group": "National", "name": "CMS nursing home data", "how": "Automatic download (open data)", "status": "unverified",
         "note": "Dataset IDs still need checking."},
        {"group": "National", "name": "Trepp screen (DSCR under 1.0x)", "how": "Researcher uploads the export", "status": "manual",
         "note": "Stays on this computer. Never committed, never sent to an AI."},
        {"group": "Scout", "name": "Project and leasing websites, local news, Ten-X, WSJ legal notices", "how": "Fixed list of approved sites, read-only",
         "status": "planned", "note": "Not built yet. WSJ pages are always a weekly manual upload; nothing logs in."},
        {"group": "AI", "name": "AI Reader (Claude, Gemini or OpenAI-style)", "how": "One wrapper; cheap and strong models; answers cached; weekly cost cap",
         "status": "built", "note": "Tested live against Gemini. The free key allows only 20 requests/day, so this demo uses recorded answers."},
    ]
    return rows


def export(con, recorded: list[dict]) -> dict:
    """Everything the page shows, as plain data."""
    scores = _rows(con, "SELECT * FROM property_scores ORDER BY score DESC, property_name")
    signals_by = {}
    for s in _rows(con, "SELECT * FROM score_signals ORDER BY counted_points DESC"):
        signals_by.setdefault(s["property_id"], []).append(s)
    documents: dict[str, dict] = {}
    properties = []
    for rank, p in enumerate(scores, 1):
        pid = p["property_id"]
        sigs = []
        for s in signals_by.get(pid, []):
            doc = _document(con, s["evidence_table"], s["evidence_id"])
            if doc:
                documents.setdefault(doc["id"], doc)
            quote = s.get("quote")
            sigs.append({"signal": s["signal"], "family": s["family"], "points": s["points"], "counted": s["counted_points"],
                         "detail": s["detail"], "evidence_table": s["evidence_table"], "evidence_id": s["evidence_id"],
                         "evidence_date": _iso(s["evidence_date"]), "quote": quote, "doc_id": doc["id"] if doc else None,
                         "quote_found": bool(doc and quote and _norm(quote) in _norm(doc["text"]))})
        histories = [_clean(h) for h in _rows(con, "SELECT * FROM loan_histories WHERE property_id=? ORDER BY recording_date", [pid])]
        project = None
        for l in _rows(con, "SELECT record_id FROM property_links WHERE property_id=? AND record_table='projects'", [pid]):
            pr = _rows(con, "SELECT * FROM projects WHERE project_id=?", [l["record_id"]])
            project = _clean(pr[0]) if pr else project
        bank = None
        if p["lender_name"]:
            lk = con.execute("SELECT rssd_id FROM lender_links WHERE lender_name=? AND rssd_id IS NOT NULL", [p["lender_name"]]).fetchone()
            if lk:
                series = _rows(con, "SELECT report_date, cre_nonaccrual_ratio, rising_two_quarters FROM bank_stats WHERE rssd_id=? ORDER BY report_date", [lk[0]])
                bank = {"name": p["lender_name"], "rssd_id": lk[0], "series": [_clean(s) for s in series]}
        ents = _rows(con, "SELECT DISTINCT e.entity_id, e.canonical_name, e.property_count, e.name_variants, l.role FROM entity_links l "
                          "JOIN entities e USING(entity_id) WHERE l.property_id=? AND l.role='borrower'", [pid])
        lookups = [r["status"] for r in _rows(con, "SELECT status FROM entity_lookup_requests WHERE entity_name=?", [p["borrower_name"]])] if p["borrower_name"] else []
        properties.append({
            "id": pid, "rank": rank, "name": p["property_name"] or p["property_address"] or pid, "address": p["property_address"],
            "city": p["city"], "state": p["state"], "county": p["county"], "parcel": p["parcel_number"],
            "type": _property_type(con, pid), "borrower": p["borrower_name"], "lender": p["lender_name"],
            "loan_amount": p["loan_amount"], "score": p["score"], "tier": p["tier"], "summary": p["summary"],
            "signals": sigs, "timeline": _timeline(sigs, histories, project), "loans": histories, "bank": bank,
            "entity": ents[0] if ents else None, "contact_lookup": lookups[0] if lookups else None,
            "grouped_by": p["grouped_by"]})
    tiers = {t: sum(1 for p in scores if p["tier"] == t) for t in ("hot", "watch", "low")}
    count = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    ff = dict(con.execute("SELECT status, count(*) FROM filing_fields GROUP BY 1").fetchall())
    summary = {
        "documents": count("SELECT count(*) FROM raw_files"), "recorded_loans": count("SELECT count(*) FROM recorded_loans"),
        "cmbs_loans": count("SELECT count(*) FROM loans"), "banks": count("SELECT count(DISTINCT rssd_id) FROM bank_stats"),
        "read": count("SELECT count(*) FROM filing_fields"), "read_ok": ff.get("ok", 0), "read_review": ff.get("needs_review", 0),
        "properties": len(scores), "hot": tiers["hot"], "watch": tiers["watch"], "low": tiers["low"],
        "records_linked": count("SELECT count(*) FROM property_links"), "entities": count("SELECT count(*) FROM entities"),
        "match_reviews": count("SELECT count(*) FROM match_review WHERE decision IS NULL")}
    return {
        "meta": {"built_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "as_of": _iso(scores[0]["as_of"]) if scores else None,
                 "scoring_version": scores[0]["scoring_version"] if scores else None},
        "summary": summary, "properties": properties, "documents": documents,
        "reader": reader_examples(recorded),
        "review": {"matches": [_clean(r) for r in _rows(con, "SELECT review_id, kind, left_label, right_label, score, reason FROM match_review WHERE decision IS NULL")]},
        "sources": sources_table(),
        "weights": _weights(),
        "content": json.loads(CONTENT.read_text(encoding="utf-8")) if CONTENT.exists() else {}}


def _weights() -> list[dict]:
    from scorer.score import load_scoring_config
    cfg = load_scoring_config()
    # (the settings file also holds a few non-signal entries, such as a name-match threshold: skip those)
    return [{"signal": k, "family": v["family"], "points": v["points"]}
            for k, v in cfg["signals"].items() if isinstance(v, dict) and "points" in v]


# ---------------------------------------------------------------------------
# Writing the page
# ---------------------------------------------------------------------------
def render(data: dict, out: Path) -> None:
    blob = json.dumps(data, ensure_ascii=False, default=str)
    blob = blob.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DEMO_DATA__*/null", blob)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")


def build(out: Path = DEFAULT_OUT, data_dir: Path = DEFAULT_DATA, live_limit: int = 0,
          log: Callable[[str], None] = print) -> dict:
    """Run the pipeline into the demo folder, export, write the page. Returns the exported data."""
    log("Building the demo (all data is FAKE) ...")
    _reset(Path(data_dir))
    run_pipeline(Path(data_dir), live_limit, log)
    if str(PIPELINE) not in sys.path:
        sys.path.insert(0, str(PIPELINE))
    os.environ["BLOOMFIELD_DATA_DIR"] = str(data_dir)
    from scout_reader_schema import open_db
    con = open_db(read_only=True)
    try:
        recorded = json.loads(RECORDED.read_text(encoding="utf-8")) if RECORDED.exists() else []
        data = export(con, recorded)
    finally:
        con.close()
    render(data, Path(out))
    log(f"Wrote {out} ({Path(out).stat().st_size / 1024:,.0f} KB): {data['summary']['properties']} properties, "
        f"{data['summary']['hot']} hot, {len(data['reader'])} recorded AI readings.")
    return data
