"""Direct-research ingestion: verified external evidence with no LLM and no search credits.

Research rows produced OUTSIDE the app (e.g. Instinct's own research, which costs the
user nothing) arrive as JSONL on the deployment volume via the token-gated upload
section in dashboard/app.py. Every row goes through the SAME verification as
agent-produced evidence: the cited page is fetched and saved (agent.tools.fetch_page)
and the claim is only stored if its quote appears verbatim on the saved page
(agent.tools.record_evidence). Nothing enters the database unverified, nothing is
paraphrased in, and no LLM call or paid search is involved anywhere in this path.

Row format (one JSON object per line):
  id           unique idempotency key (required)
  property_id  projects.project_id this evidence is about (required)
  claim        claim name (required). Scorer-consumable claims use the exact names
               agent/mapping.py reads: asking_rent, market_rent_index, permit_inspection.
               Anything else stays narrative (shown as research, never scored).
  value        claimed value (optional)
  url          public page the fact came from (required)
  quote        15-600 characters copied verbatim from that page (required)
  notes        reviewer notes; for scorer-consumable claims the mapping fields
  summary      short researcher summary shown on the property page (optional)

Each saved row creates one agent_runs entry (status done, 0 LLM calls, $0.00) carrying
the [property_id=...] marker so the property page shows it under "AI research" and its
verified evidence, exactly like agent-produced runs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from agent import tools as agent_tools


def create_tables(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS ingest_rows (
            id           TEXT PRIMARY KEY,
            property_id  TEXT,
            status       TEXT,           -- saved | refused | fetch_failed | invalid
            detail       TEXT,
            processed_at TIMESTAMP
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS research_assessments (
            id              TEXT PRIMARY KEY,   -- the JSONL row id (idempotency)
            property_id     TEXT,
            research_score  DOUBLE,
            research_tier   TEXT,               -- Hot | Watch | Low (research-lane thresholds)
            confidence      TEXT,
            coverage        TEXT,
            score_breakdown TEXT,               -- JSON object
            score_reasons   TEXT,               -- JSON array of strings
            one_line        TEXT,               -- one-line reason shown on the ranked-list card
            artifact        TEXT,
            generated_at    TEXT,
            ingested_at     TIMESTAMP
        )""")
    try:
        con.execute("ALTER TABLE research_assessments ADD COLUMN one_line TEXT")
    except Exception:
        pass   # column already there


def _save_assessment(con, row: dict, rid: str, ledger) -> bool:
    """Store a research-lane provisional assessment (rubric v2). NOT evidence and NOT a
    formula input: a structured judgment row displayed separately from the deterministic
    score. Needs property_id, research_score (0-100) and research_tier (Hot|Watch|Low)."""
    pid = str(row.get("property_id") or "").strip()
    if not pid or not _property(con, pid):
        ledger("invalid", f"unknown property_id {pid!r}; assessment must name an existing project")
        return False
    tier = str(row.get("research_tier") or "").strip()
    if tier.split(" ")[0] not in ("Hot", "Watch", "Low", "Unscored"):
        ledger("invalid", "research_tier must start with Hot, Watch, Low or Unscored (research-lane "
                          "label; a flag suffix like 'Low (pre-default ...)' is fine)")
        return False
    if tier.split(" ")[0] == "Unscored":
        # explicit unscored/conflict marker: a researched property whose filings cannot yet be
        # reconciled into one honest number (e.g. an unresolved debt-scope conflict). Stored for
        # the audit trail and the property page; never counted or displayed as a score.
        score = None
    else:
        try:
            score = float(row.get("research_score"))
            if not (0.0 <= score <= 100.0):
                raise ValueError
        except (TypeError, ValueError):
            ledger("invalid", "research_score must be a number 0-100")
            return False
    bd, reasons = row.get("score_breakdown"), row.get("score_reasons")
    con.execute(
        "INSERT INTO research_assessments (id, property_id, research_score, research_tier, "
        "confidence, coverage, score_breakdown, score_reasons, one_line, artifact, generated_at, ingested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [rid, pid, score, tier,
         str(row.get("confidence") or ""), str(row.get("coverage") or ""),
         json.dumps(bd) if isinstance(bd, (dict, list)) else str(bd or ""),
         json.dumps(reasons) if isinstance(reasons, (dict, list)) else str(reasons or ""),
         str(row.get("one_line") or ""),
         str(row.get("artifact") or ""), str(row.get("generated_at") or ""), _utcnow()])
    ledger("saved", f"research assessment stored: {tier} "
                    f"{f'{score:.0f}' if score is not None else 'unscored'} (research lane, not the formula)")
    return True


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _property(con, property_id: str):
    """Resolve a flag's property id to a display name, or None when unknown.

    property_id is the matcher-level id from weekly_flags; the underlying project
    record is reached through property_links (agent/mapping.py does the same).
    """
    try:
        row = con.execute(
            "SELECT pl.property_id, p.project_name FROM property_links pl "
            "JOIN projects p ON p.project_id = pl.record_id AND pl.record_table = 'projects' "
            "WHERE pl.property_id = ? LIMIT 1", [property_id]).fetchone()
        if row:
            return row
    except Exception:
        pass
    try:
        row = con.execute(
            "SELECT property_id, property_name FROM weekly_flags WHERE property_id = ? "
            "ORDER BY run_id DESC LIMIT 1", [property_id]).fetchone()
        if row:
            return row
    except Exception:
        pass
    try:
        return con.execute(
            "SELECT project_id, project_name FROM projects WHERE project_id = ?",
            [property_id]).fetchone()
    except Exception:
        return None


def process_rows(con, rows: list[dict], *, fetcher=None, recorder=None) -> dict:
    """Verify and save a batch of research rows. Idempotent by row id.

    fetcher/recorder are injectable for tests; production uses agent.tools.fetch_page
    and agent.tools.record_evidence so ingestion and the agent share one standard.
    Returns counts plus per-row outcomes.
    """
    fetcher = fetcher or agent_tools.fetch_page
    recorder = recorder or agent_tools.record_evidence
    create_tables(con)
    agent_tools.create_tables(con)

    out = {"saved": 0, "refused": 0, "fetch_failed": 0, "invalid": 0, "skipped": 0, "rows": []}
    for i, row in enumerate(rows):
        rid = str(row.get("id") or "").strip() or f"missing-id-{i}"
        if con.execute("SELECT 1 FROM ingest_rows WHERE id = ?", [rid]).fetchone():
            out["skipped"] += 1
            continue

        def ledger(status: str, detail: str) -> None:
            con.execute(
                "INSERT INTO ingest_rows (id, property_id, status, detail, processed_at) "
                "VALUES (?,?,?,?,?)",
                [rid, row.get("property_id"), status, detail[:500], _utcnow()])
            out["rows"].append({"id": rid, "status": status, "detail": detail[:200]})

        if str(row.get("type") or "").strip() == "research_assessment":
            if _save_assessment(con, row, rid, ledger):
                out["saved"] += 1
            else:
                out["invalid"] += 1
            continue

        pid = str(row.get("property_id") or "").strip()
        claim = str(row.get("claim") or "").strip()
        url = str(row.get("url") or "").strip()
        quote = str(row.get("quote") or "").strip()
        prop = _property(con, pid) if pid else None
        if not prop:
            ledger("invalid", f"unknown property_id {pid!r}; evidence must name an existing project")
            out["invalid"] += 1
            continue
        if not claim or not url or not quote:
            ledger("invalid", "row needs claim, url and quote")
            out["invalid"] += 1
            continue

        fetched = fetcher(con, url)
        if not fetched.get("ok"):
            ledger("fetch_failed", fetched.get("error", "fetch failed"))
            out["fetch_failed"] += 1
            continue

        run_id = f"ingest-{rid}"
        subject = pid          # mapping.py resolves scorer-consumable claims via the property id
        summary = str(row.get("summary") or "").strip()
        res = recorder(con, run_id, subject=subject, claim=claim,
                       value=(None if row.get("value") in (None, "") else str(row.get("value"))),
                       file_id=fetched["file_id"], source_url=fetched["url"],
                       quote=quote, notes=(None if not row.get("notes") else str(row.get("notes"))),
                       model_name="direct-research")
        if not res.get("ok"):
            ledger("refused", res.get("error", "record_evidence refused the row"))
            out["refused"] += 1
            continue

        con.execute(
            "INSERT INTO agent_runs (run_id, goal, status, started_at, finished_at, steps, "
            "llm_calls, cost_usd, final_answer) VALUES (?,?,?,?,?,?,?,?,?)",
            [run_id,
             f"Direct research ingest [property_id={pid}]",
             "done", _utcnow(), _utcnow(), 0, 0, 0.0,
             summary or f"Verified external evidence ingested: {claim}"
                        + (f" = {row.get('value')}" if row.get("value") not in (None, "") else "")])
        ledger("saved", f"evidence saved for {subject}")
        out["saved"] += 1
    return out


def parse_jsonl(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def process_file(con, path, *, fetcher=None, recorder=None) -> dict:
    """Process one JSONL file from the incoming directory."""
    from pathlib import Path
    p = Path(path)
    result = process_rows(con, parse_jsonl(p.read_text()), fetcher=fetcher, recorder=recorder)
    result["file"] = p.name
    return result


def incoming_dir(db_path: str | None):
    from pathlib import Path
    base = Path(db_path).resolve().parent if db_path else Path(__file__).resolve().parents[1] / "data"
    d = base / "incoming"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sweep(db_path: str | None = None, *, rescore: bool = True) -> dict:
    """Process every pending *.jsonl in the incoming dir, then re-score once.

    The re-score is the ordinary deterministic scorer (scorer.weekly.run): it folds the
    newly verified scorer-consumable claims through agent/mapping.py and recomputes
    flags - no LLM anywhere. Processed files are renamed so a file is never ingested twice.
    """
    from collectors.common.db import connect
    from scorer import weekly

    incoming = incoming_dir(db_path)
    totals = {"files": 0, "saved": 0, "refused": 0, "fetch_failed": 0, "invalid": 0, "skipped": 0}
    pending = sorted(incoming.glob("*.jsonl"))
    if not pending:
        return totals
    con = connect(db_path)
    try:
        for p in pending:
            try:
                r = process_file(con, p)
                con.commit()
                for k in ("saved", "refused", "fetch_failed", "invalid", "skipped"):
                    totals[k] += r[k]
                p.rename(p.with_suffix(".jsonl.done"))
            except Exception as e:  # a bad file must not block the others
                try:
                    con.rollback()
                except Exception:
                    pass
                totals["invalid"] += 1
                p.rename(p.with_suffix(".jsonl.failed"))
                (p.parent / (p.name + ".error.txt")).write_text(str(e)[:1000])
            totals["files"] += 1
        if rescore and totals["saved"]:
            weekly.run(con)
            con.commit()
            totals["rescored"] = True
    finally:
        con.close()
    return totals
