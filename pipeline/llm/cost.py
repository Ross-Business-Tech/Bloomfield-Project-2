"""
cost.py - counting tokens, estimating dollars, and advisory spend warnings.

In plain English: AI services charge by "tokens" (roughly 4 characters each). Before
every call we estimate the worst-case cost and warn when it would pass the advisory
landmarks in reader.yaml - the provider account enforces the real dollar limit, so
the code does not stop calls. After every REAL call we write a row to llm_usage,
which is what the weekly total adds up. Answers served from the cache cost nothing.

See your spending any time:   python -m llm.cost
"""
from __future__ import annotations

import math
from datetime import timedelta

from collectors.common.db import utc_now
from collectors.common.log import get_logger

from .settings import load_reader_config

log = get_logger("llm.cost")


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
    """(dollars per input token, dollars per output token) for a model.

    Fail-closed: a model with no EXACT configured price is refused. Any guessed price
    (a default catch-all, the highest listed rate) can under-count the real bill, and
    the spending cap is only as honest as the prices behind it."""
    cfg = cfg or load_reader_config()
    table = cfg.get("prices_per_million_tokens") or {}
    entry = table.get(model)
    if entry is None:
        raise ValueError(
            f"no exact price configured for model {model!r} in reader.yaml. Add its "
            "authoritative per-million-token price to prices_per_million_tokens before "
            "spending on it.")
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
    Estimate the worst case for the next call (all the output tokens we allow it to
    write) and warn when it would pass the advisory caps in reader.yaml. It does NOT
    stop the call: the provider account enforces its own dollar limit (user decision,
    2026-11-15), so a second code-level cap would only block authorized work early.
    Usage accounting still records every real call. Returns the worst-case estimate.
    """
    cfg = cfg or load_reader_config()
    worst = compute_cost(model, est_input_tokens, max_output_tokens, cfg)
    weekly_cap = float(cfg.get("weekly_cost_cap_usd", 10))
    run_cap = float(cfg.get("run_cost_cap_usd", 5))
    spent_week = weekly_spend(con) if con is not None else 0.0
    if spent_week + worst > weekly_cap:
        log.warning("weekly AI spend passing advisory cap: $%.2f spent in the last 7 days; "
                    "the next call could cost up to $%.2f (advisory cap $%.2f, not enforced)",
                    spent_week, worst, weekly_cap)
    if _run_spend + worst > run_cap:
        log.warning("per-run AI spend passing advisory cap: $%.2f spent in this run; "
                    "the next call could cost up to $%.2f (advisory cap $%.2f, not enforced)",
                    _run_spend, worst, run_cap)
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
    cap = float(cfg.get("weekly_cost_cap_usd", 10))
    week = weekly_spend(con)
    print(f"\nLast 7 days: ${week:.2f} of the ${cap:.2f} weekly cap (${max(cap - week, 0):.2f} left).")


if __name__ == "__main__":
    main()
