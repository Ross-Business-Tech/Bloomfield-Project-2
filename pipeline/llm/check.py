"""
check.py - a 30-second test that your AI setup works (provider, key, models, JSON answers).

In plain English: run this once after putting your key in .env. It makes TWO tiny real calls and costs a
fraction of a cent:
  1. the cheap model: "reply with a tiny JSON answer"
  2. the strong model: read a short FAKE mechanic's lien with the real extraction prompt, and run the
     reader's own checks (JSON valid? quotes really in the document?)
It prints what worked, what did not, the token counts and the estimated cost. It does not touch the database.

    python -m llm.check                      # uses LLM_PROVIDER / models from .env and reader.yaml
    LLM_PROVIDER=gemini python -m llm.check  # try a different provider just for this run
"""
from __future__ import annotations

import sys
import time

from llm import client
from llm.cost import compute_cost
from llm.settings import load_reader_config, model_for_task, provider_name

FAKE_LIEN = """CLAIM OF LIEN
Claimant: Acme Heating & Air FAKE Inc.
Owner: Maize Row Holdings FAKE LLC
Property: 1000 Example Campus Way, Ann Arbor, MI 48104
Amount claimed: $1,240,000 for unpaid HVAC installation work.
Recorded: 2026-10-14
"""


def _step(title: str) -> None:
    print(f"\n{title}")


def main() -> int:
    cfg = load_reader_config()
    provider = provider_name(cfg)
    print(f"Provider: {provider}")
    try:
        cheap, strong = model_for_task("relevance", cfg), model_for_task("extract", cfg)
    except ValueError as e:
        print(f"PROBLEM: {e}")
        return 1
    print(f"Cheap model:  {cheap}\nStrong model: {strong}")
    try:
        client.get_api_key(provider)
        print("API key:      found (not shown)")
    except client.LLMError as e:
        print(f"PROBLEM: {e}")
        return 1

    failures = 0

    _step("1/2  Cheap model: tiny JSON answer")
    try:
        start = time.time()
        r = client.call_llm_detailed("relevance", 'Reply with ONLY this JSON object: {"ok": true}',
                                     "Please reply now.", 50, cfg=cfg)
        print(f"  reply: {r.text.strip()[:120]!r}  ({r.input_tokens} in / {r.output_tokens} out tokens, "
              f"{time.time() - start:.1f}s, ~${r.cost_usd:.5f})")
        if "ok" not in r.text.lower():
            print("  WARNING: the answer was not the JSON we asked for.")
            failures += 1
    except client.LLMError as e:
        print(f"  FAILED: {e}")
        failures += 1

    _step("2/2  Strong model: read a fake lien with the real extraction prompt")
    try:
        from reader.check import validate
        from reader.prompt import SYSTEM_PROMPT, build_user_text
        row = {"source": "recorder", "doc_category": "mechanics_lien", "state": "MI", "county": "Washtenaw County"}
        start = time.time()
        r = client.call_llm_detailed("extract", SYSTEM_PROMPT, build_user_text(row, FAKE_LIEN),
                                     int(cfg["reader"]["max_output_tokens"]), cfg=cfg)
        print(f"  {r.input_tokens} in / {r.output_tokens} out tokens, {time.time() - start:.1f}s, ~${r.cost_usd:.5f}"
              + ("  [ANSWER WAS CUT OFF]" if r.truncated else ""))
        record, status, reason = validate(r.text, FAKE_LIEN, doc_category="mechanics_lien", cfg=cfg)
        if record is None:
            print(f"  FAILED: the answer was not usable JSON for our schema: {reason}")
            print(f"  first 300 characters of the answer: {r.text[:300]!r}")
            failures += 1
        else:
            print(f"  read it as: {record.filing_type.value}; claimant={record.lien_claimant!r}; "
                  f"amount={record.lien_amount}; property={record.property_address!r}")
            print(f"  checks: {status}" + (f" ({reason})" if reason else "  (every quote was found in the document)"))
            if status != "ok":
                failures += 1
    except client.LLMError as e:
        print(f"  FAILED: {e}")
        failures += 1

    print("\n" + ("All good: this setup works." if not failures else
                  f"{failures} problem(s) above. See the README section on switching AI provider."))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
