"""
record.py - saves real AI readings of a few documents, so the demo can show them with no API key and no cost.

In plain English: the demo page has an "AI Reader" tab that shows what a real model pulled out of the fake Ann Arbor
documents. Those answers were recorded once from a real run (the Reader keeps every answer in its cache) and saved in
demo/recorded_readings.json. Record fresh ones whenever you have an API key with quota:

    python -m demo.record --data-dir path/to/data --file-ids fake-annarbor-hvac-lien fake-annarbor-permit

Only FAKE documents should ever be recorded: this file is committed to a public repository.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

RECORDED = Path(__file__).resolve().parent / "recorded_readings.json"


def record(con, file_ids: list[str], provider: str | None = None) -> list[dict]:
    """Collect document text + the cached model answer for each file id (the answer must already be cached)."""
    from collectors.common import config
    from llm import client
    from llm.settings import load_reader_config, model_for_task
    from reader.prompt import PROMPT_VERSION

    cfg = load_reader_config()
    model = model_for_task("extract", cfg)
    out = []
    for fid in file_ids:
        row = con.execute("SELECT file_id, source, doc_category, state, county, file_path, file_hash, file_type "
                          "FROM raw_files WHERE file_id = ?", [fid]).fetchone()
        if row is None:
            raise SystemExit(f"{fid}: not in raw_files")
        if "fake" not in fid:
            raise SystemExit(f"{fid}: refusing to record a document that is not marked FAKE (this file is public)")
        key = client.make_cache_key(row[6], PROMPT_VERSION, model, "extract")
        hit = con.execute("SELECT response_json FROM llm_cache WHERE cache_key = ?", [key]).fetchone()
        if hit is None:
            raise SystemExit(f"{fid}: no saved answer for model {model}; run the Reader on it first")
        cached = json.loads(hit[0])
        out.append({
            "file_id": row[0], "source": row[1], "doc_category": row[2], "state": row[3], "county": row[4],
            "file_type": row[7], "document_text": config.from_stored_path(row[5]).read_text(encoding="utf-8"),
            "reply_text": cached["text"], "model": model, "provider": provider or os.environ.get("LLM_PROVIDER", ""),
            "prompt_version": PROMPT_VERSION, "input_tokens": cached.get("input_tokens"),
            "output_tokens": cached.get("output_tokens"), "recorded_on": date.today().isoformat(),
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Record cached AI readings of FAKE documents for the demo.")
    parser.add_argument("--data-dir", required=True, help="the data folder whose database holds the cached answers")
    parser.add_argument("--file-ids", nargs="+", required=True)
    parser.add_argument("--out", type=Path, default=RECORDED)
    args = parser.parse_args()
    os.environ["BLOOMFIELD_DATA_DIR"] = args.data_dir
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scout_reader_schema import open_db
    records = record(open_db(read_only=True), args.file_ids)
    args.out.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Saved {len(records)} readings to {args.out}")


if __name__ == "__main__":
    main()
