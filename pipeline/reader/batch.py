"""
batch.py - runs the Reader over every file that has not been read yet.

In plain English, for each file in raw_files that is waiting:
  1. read its text (OCR if it is a scan),
  2. cut long documents down to the important pages,
  3. ask the AI to fill in the fields, with a quote for each,
  4. check the answer (check.py) and save one row in filing_fields: ok / needs_review / failed.

Safe to stop and restart: each file is saved as soon as it is done, and answers already paid for
come from the cache. It skips files that Part A already turns into tables (SEC EDGAR, FFIEC, CMS,
Trepp) and anything marked confidential. It stops cleanly if the spending cap is reached.

Run from the pipeline/ folder:
    python -m reader.batch --limit 50 --state MI
    python -m reader.batch --source recorder
    python -m reader.batch --retry-failed
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime

from tqdm import tqdm

from collectors.common import config
from collectors.common.db import utc_now
from collectors.common.inbox import CONFIDENTIAL_MARKER
from collectors.common.log import get_logger, setup_logging
from llm import cost
from llm.client import LLMConfigError, LLMError, call_llm_detailed
from llm.settings import load_reader_config
from scout_reader_schema import open_db, upsert

from . import chunk, ocr
from .check import validate
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_retry_text, build_user_text
from .schema import FilingRecord, FilingType, ReviewStatus

log = get_logger("reader.batch")

_PAGE_MARK = re.compile(r"\[PAGE \d+\]")


# ---------------------------------------------------------------------------
# Which files are private?
# ---------------------------------------------------------------------------
def is_confidential(row: dict) -> bool:
    """
    True for private Bloomfield material: notes marked [CONFIDENTIAL] (set by the manual inbox for
    manual_upload/bloomfield/ files or a sidecar with "confidential": true), a saved path inside a
    'bloomfield' folder, or a saved sidecar (.meta.json) that says confidential.
    """
    if CONFIDENTIAL_MARKER.lower() in (row.get("notes") or "").lower():
        return True
    stored = row.get("file_path") or ""
    if "/bloomfield/" in stored.replace("\\", "/").lower():
        return True
    if stored:
        path = config.from_stored_path(stored)
        meta = path.with_name(path.name + ".meta.json")
        if meta.exists():
            try:
                return json.loads(meta.read_text(encoding="utf-8")).get("confidential") is True
            except (OSError, ValueError):
                return True          # can't tell, so play safe
    return False


# ---------------------------------------------------------------------------
# Picking the files to read
# ---------------------------------------------------------------------------
def select_files(con, cfg: dict, state=None, source=None, retry_failed=False) -> list[dict]:
    """raw_files rows with no filing_fields row for the current prompt version."""
    rcfg = cfg.get("reader") or {}
    allowed = set(rcfg.get("process_sources") or [])
    if cfg.get("trepp_ai_allowed", False):
        allowed.add("trepp")                         # Trepp is only read if config explicitly allows it
    if not allowed:
        return []
    marks = ", ".join("?" for _ in allowed)
    retry_clause = " OR f.status = 'failed'" if retry_failed else ""
    sql = (f"SELECT r.* FROM raw_files r LEFT JOIN filing_fields f ON f.file_id = r.file_id "
           f"WHERE r.source IN ({marks}) "
           f"AND (f.file_id IS NULL OR f.prompt_version IS DISTINCT FROM ?{retry_clause})")
    params = sorted(allowed) + [PROMPT_VERSION]
    if state:
        sql += " AND r.state = ?"
        params.append(state.upper())
    if source:
        sql += " AND r.source = ?"
        params.append(source)
    sql += " ORDER BY r.collected_at, r.file_id"
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Reading one file
# ---------------------------------------------------------------------------
class _Outcome:
    """What happened to one file (used for the summary)."""

    def __init__(self, status: str):
        self.status = status          # ok / needs_review / failed
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost = 0.0
        self.real_calls = 0
        self.cached_calls = 0


def _save_row(con, file_id: str, *, record=None, status: str, reason: str | None, model=None,
              tokens=(0, 0), cost_usd=0.0) -> None:
    """Write the filing_fields row. If there is no usable answer, filing_type is 'other'."""
    data = record.model_dump() if record is not None else {"filing_type": FilingType.other}
    rec = FilingRecord(**data, file_id=file_id, status=ReviewStatus(status), review_reason=reason,
                       model_name=model, prompt_version=PROMPT_VERSION, extracted_at=utc_now(),
                       input_tokens=tokens[0], output_tokens=tokens[1], est_cost_usd=cost_usd)
    upsert(con, "filing_fields", rec.to_db_row())


def process_file(con, row: dict, cfg: dict, engine, restricted: str | None) -> _Outcome:
    """Read one raw file and save its filing_fields row. Infrastructure problems are raised."""
    rcfg = cfg.get("reader") or {}
    file_id = row["file_id"]

    # 1. text (raises OcrUnavailableError if a scan cannot be read: no row is written, so it retries later)
    try:
        tr = ocr.get_text(config.from_stored_path(row["file_path"]), row.get("file_type"),
                          engine=engine, cfg=cfg)
    except ocr.OcrUnavailableError:
        raise
    except (ocr.UnsupportedFileError, FileNotFoundError) as e:
        _save_row(con, file_id, status="failed", reason=f"could not read the file: {e}")
        return _Outcome("failed")
    except Exception as e:                                           # corrupt PDF, bad image, ...
        _save_row(con, file_id, status="failed", reason=f"could not read the file: {type(e).__name__}: {e}")
        return _Outcome("failed")

    plain = _PAGE_MARK.sub("", tr.text).strip()
    if len(plain) < int(rcfg.get("min_text_chars", 20)):
        why = "; ".join(["no readable text in the document"] + tr.warnings)
        _save_row(con, file_id, status="needs_review", reason=why)
        return _Outcome("needs_review")

    # 2. long documents: pick the important pages
    sent_text, trunc_note = chunk.select_text(
        tr.text, int(rcfg.get("max_chars", 120000)), int(rcfg.get("max_pages", 40)),
        int(rcfg.get("chunk_first_pages", 3)))

    # 3. ask the AI (once, plus one retry if its answer is unusable)
    user_text = build_user_text(row, sent_text)
    max_out = int(rcfg.get("max_output_tokens", 2500))
    kwargs = dict(con=con, file_hash=row["file_hash"], prompt_version=PROMPT_VERSION,
                  restricted=restricted, cfg=cfg)
    calls = [call_llm_detailed("extract", SYSTEM_PROMPT, user_text, max_out, **kwargs)]
    check_args = dict(doc_category=row.get("doc_category"), ocr_share=tr.ocr_share, cfg=cfg)
    record, status, reason = validate(calls[0].text, sent_text, **check_args)
    if record is None:
        hint = " (the answer was cut off; it may be too long)" if calls[0].truncated else ""
        calls.append(call_llm_detailed(
            "extract", SYSTEM_PROMPT, build_retry_text(user_text, calls[0].text, (reason or "") + hint),
            max_out, variant="retry1", **kwargs))
        record, status, reason = validate(calls[1].text, sent_text, **check_args)
        if record is None and calls[1].truncated:
            reason += " (the answer was cut off; it may be too long)"

    if record is None:                      # don't keep unusable answers, so a later retry asks again
        con.execute(f"DELETE FROM llm_cache WHERE cache_key IN ({', '.join('?' for _ in calls)})",
                    [c.cache_key for c in calls])

    # 4. extra warnings from reading the file
    notes = [reason] if reason else []
    if status != "failed":
        if trunc_note:
            notes.append(trunc_note)
            if status == "ok" and rcfg.get("truncation_needs_review", True):
                status = "needs_review"
        for w in tr.warnings:
            notes.append(w)
            status = "needs_review"
    elif trunc_note:
        notes.append(trunc_note)

    out = _Outcome(status)
    out.input_tokens = sum(c.input_tokens for c in calls)
    out.output_tokens = sum(c.output_tokens for c in calls)
    out.cost = sum(c.cost_usd for c in calls)
    out.real_calls = sum(1 for c in calls if not c.cached)
    out.cached_calls = sum(1 for c in calls if c.cached)
    _save_row(con, file_id, record=record, status=status, reason="; ".join(notes) or None,
              model=calls[-1].model, tokens=(out.input_tokens, out.output_tokens), cost_usd=out.cost)
    return out


# ---------------------------------------------------------------------------
# The whole batch
# ---------------------------------------------------------------------------
def run(limit: int | None = None, state: str | None = None, source: str | None = None,
        retry_failed: bool = False, con=None, cfg: dict | None = None, engine=None,
        progress: bool = True, quiet: bool = False) -> dict:
    """
    Read every waiting file (up to `limit`). Returns and prints a summary dict.
    Pass con / cfg / engine only in tests.
    """
    cfg = cfg or load_reader_config()
    own_con = con is None
    con = con or open_db()
    cost.reset_run_spend()
    started = datetime.now()

    summary = {"candidates": 0, "processed": 0, "ok": 0, "needs_review": 0, "failed": 0,
               "not_processed": 0, "skipped_confidential": 0, "real_calls": 0, "cached_calls": 0,
               "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "stopped": None}

    # Private material is left out unless Bloomfield has switched it on in reader.yaml.
    rows = select_files(con, cfg, state, source, retry_failed)
    summary["candidates"] = len(rows)
    todo = []
    for r in rows:
        if is_confidential(r) and not cfg.get("bloomfield_material_ai_allowed", False):
            summary["skipped_confidential"] += 1
        else:
            todo.append(r)
    if limit:
        todo = todo[:limit]

    engine = engine or ocr.get_engine(cfg)
    max_llm_errors = int((cfg.get("reader") or {}).get("max_consecutive_llm_errors", 3))
    llm_errors, warned_ocr = 0, False

    bar = tqdm(todo, desc="Reading", unit="file", disable=not progress or quiet)
    for row in bar:
        restricted = "trepp" if row["source"] == "trepp" else ("bloomfield" if is_confidential(row) else None)
        try:
            out = process_file(con, row, cfg, engine, restricted)
        except cost.CostCapExceeded as e:
            summary["stopped"] = str(e)
            break
        except LLMConfigError as e:                                  # e.g. no API key: nothing can work
            summary["stopped"] = str(e)
            break
        except ocr.OcrUnavailableError as e:                         # not saved, so it is retried next run
            summary["not_processed"] += 1
            if not warned_ocr:
                tqdm.write(f"NOTE: {e}")
                warned_ocr = True
            continue
        except LLMError as e:                                        # the AI service is struggling
            summary["not_processed"] += 1
            llm_errors += 1
            log.error("AI call failed for %s: %s", row["file_id"], e)
            if llm_errors >= max_llm_errors:
                summary["stopped"] = f"stopped after {llm_errors} AI service errors in a row: {e}"
                break
            continue
        llm_errors = 0
        summary["processed"] += 1
        summary[out.status] += 1
        summary["real_calls"] += out.real_calls
        summary["cached_calls"] += out.cached_calls
        summary["input_tokens"] += out.input_tokens
        summary["output_tokens"] += out.output_tokens
        summary["cost_usd"] += out.cost
    bar.close()

    summary["seconds"] = round((datetime.now() - started).total_seconds(), 1)
    if not quiet:
        print_summary(summary, limit)
    if own_con:
        con.close()
    return summary


def print_summary(s: dict, limit=None) -> None:
    print("\nReader summary")
    print(f"  files waiting to be read:   {s['candidates']}" + (f"  (limit {limit})" if limit else ""))
    print(f"  processed:                  {s['processed']}   "
          f"ok {s['ok']}, needs_review {s['needs_review']}, failed {s['failed']}")
    if s["not_processed"]:
        print(f"  NOT processed (will retry): {s['not_processed']}  (OCR missing or AI service errors)")
    if s["skipped_confidential"]:
        print(f"  skipped (confidential):     {s['skipped_confidential']}  (never sent to an outside AI)")
    print(f"  AI calls:                   {s['real_calls']} new, {s['cached_calls']} answered from cache (free)")
    print(f"  tokens:                     {s['input_tokens']:,} in, {s['output_tokens']:,} out")
    print(f"  estimated cost:             ${s['cost_usd']:.4f}")
    if s["stopped"]:
        print(f"\n  *** {s['stopped']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read documents in raw_files and fill filing_fields.")
    parser.add_argument("--limit", type=int, help="read at most this many files")
    parser.add_argument("--state", help="only files from this state (2-letter code)")
    parser.add_argument("--source", help="only this source, e.g. recorder")
    parser.add_argument("--retry-failed", action="store_true", help="also re-try files that failed before")
    parser.add_argument("--no-progress", action="store_true")
    args = parser.parse_args()
    setup_logging()
    run(args.limit, args.state, args.source, args.retry_failed, progress=not args.no_progress)


if __name__ == "__main__":
    main()
