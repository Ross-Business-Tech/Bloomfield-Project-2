"""
cost.py - counting tokens, estimating dollars, and enforcing the spending cap.

In plain English: AI services charge by "tokens" (roughly 4 characters each). Before
every call we estimate the worst-case cost and refuse to go over the weekly or
per-run cap in reader.yaml. After every REAL call we write a row to llm_usage, which
is what the weekly total adds up. Answers served from the cache cost nothing.

See your spending any time:   python -m llm.cost
"""
from __future__ import annotations

import math
from datetime import timedelta

from collectors.common.db import utc_now
from collectors.common.log import get_logger

from .settings import load_reader_config

log = get_logger("llm.cost")


_warned_models: set[str] = set()      # say "no price listed" once per model, not on every call


class CostCapExceeded(RuntimeError):
    """Raised when the next AI call would push spending over a cap."""


# Dollars spent by REAL calls in this program run (reset at the start of each run).
_run_spend = 0.0


def reset_run_spend() -> None:
    global _run_spend
    _run_spend = 0.0


def get_run_spend() -> float:
    return _run_spend


def estimate_tokens(text: str, cfg: dict | None = None) -> int:
    """Rough token count: characters divided by 4 (set chars_per_token in reader.yaml)."""
    cfg = cfg or load_reader_config()
    per = float((cfg.get("llm") or {}).get("chars_per_token", 4)) or 4
    return int(math.ceil(len(text or "") / per))


def get_prices(model: str, cfg: dict | None = None) -> tuple[float, float]:
    """(dollars per input token, dollars per output token) for a model."""
    cfg = cfg or load_reader_config()
    table = cfg.get("prices_per_million_tokens") or {}
    entry = table.get(model)
    if entry is None:
        entry = table.get("default")
        if model not in _warned_models:
            _warned_models.add(model)
            log.warning("no price listed for model %r in reader.yaml; using the default price (add the real one)", model)
    if entry is None:
        raise ValueError("reader.yaml needs prices_per_million_tokens (at least a 'default')")
    return float(entry["input"]) / 1_000_000, float(entry["output"]) / 1_000_000


def compute_cost(model: str, input_tokens: int, output_tokens: int, cfg: dict | None = None) -> float:
    pin, pout = get_prices(model, cfg)
    return input_tokens * pin + output_tokens * pout


def weekly_spend(con, now=None) -> float:
    """Dollars spent by real AI calls in the last 7 days."""
    now = now or utc_now()
    row = con.execute("SELECT COALESCE(SUM(est_cost_usd), 0) FROM llm_usage WHERE called_at >= ?",
                      [now - timedelta(days=7)]).fetchone()
    return float(row[0])


def check_budget(con, model: str, est_input_tokens: int, max_output_tokens: int,
                 cfg: dict | None = None) -> float:
    """
    Raise CostCapExceeded if the worst case for the next call (all the output tokens
    we allow it to write) would take us over the weekly or per-run cap.
    Returns the worst-case cost estimate.
    """
    cfg = cfg or load_reader_config()
    worst = compute_cost(model, est_input_tokens, max_output_tokens, cfg)
    weekly_cap = float(cfg.get("weekly_cost_cap_usd", 25))
    run_cap = float(cfg.get("run_cost_cap_usd", 10))
    spent_week = weekly_spend(con) if con is not None else 0.0
    if spent_week + worst > weekly_cap:
        raise CostCapExceeded(
            f"STOPPED: weekly AI cost cap reached. Spent ${spent_week:.2f} in the last 7 days; "
            f"the next call could cost up to ${worst:.2f}, which would pass the ${weekly_cap:.2f} cap. "
            "Raise weekly_cost_cap_usd in config/reader.yaml if this is intended.")
    if _run_spend + worst > run_cap:
        raise CostCapExceeded(
            f"STOPPED: per-run AI cost cap reached. Spent ${_run_spend:.2f} in this run; "
            f"the next call could cost up to ${worst:.2f}, which would pass the ${run_cap:.2f} cap. "
            "Raise run_cost_cap_usd in config/reader.yaml, or run again later.")
    return worst


def record_usage(con, task: str, provider: str, model: str, input_tokens: int,
                 output_tokens: int, file_hash: str | None = None, cfg: dict | None = None) -> float:
    """Log one REAL call in llm_usage and add it to this run's total. Returns its cost."""
    global _run_spend
    cost = compute_cost(model, input_tokens, output_tokens, cfg)
    _run_spend += cost
    if con is not None:
        con.execute(
            "INSERT INTO llm_usage (called_at, task, provider, model, input_tokens, output_tokens, "
            "est_cost_usd, file_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [utc_now(), task, provider, model, input_tokens, output_tokens, cost, file_hash])
    return cost


def cost_summary(con, days: int = 7) -> list[dict]:
    """Spending grouped by job and model for the last `days` days."""
    rows = con.execute(
        "SELECT task, model, count(*), SUM(input_tokens), SUM(output_tokens), SUM(est_cost_usd) "
        "FROM llm_usage WHERE called_at >= ? GROUP BY 1, 2 ORDER BY 6 DESC",
        [utc_now() - timedelta(days=days)]).fetchall()
    return [{"task": t, "model": m, "calls": c, "input_tokens": i or 0,
             "output_tokens": o or 0, "cost_usd": cost or 0.0} for t, m, c, i, o, cost in rows]


def main() -> None:
    """Print the spending report:  python -m llm.cost  [--days N]"""
    import argparse

    from scout_reader_schema import open_db
    parser = argparse.ArgumentParser(description="Show AI spending.")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    cfg = load_reader_config()
    con = open_db()
    rows = cost_summary(con, args.days)
    print(f"AI spending, last {args.days} days (estimates from token counts and reader.yaml prices)")
    print(f"{'job':<16}{'model':<22}{'calls':>6}{'input tok':>12}{'output tok':>12}{'cost $':>10}")
    for r in rows:
        print(f"{r['task']:<16}{r['model']:<22}{r['calls']:>6}{r['input_tokens']:>12,}"
              f"{r['output_tokens']:>12,}{r['cost_usd']:>10.4f}")
    cap = float(cfg.get("weekly_cost_cap_usd", 25))
    week = weekly_spend(con)
    print(f"\nLast 7 days: ${week:.2f} of the ${cap:.2f} weekly cap (${max(cap - week, 0):.2f} left).")


if __name__ == "__main__":
    main()
