"""
ingest.py - refresh the research layer from what is already stored, then re-derive the gates.

This is deliberately an EXPLICIT command (python -m research ingest), not part of the
default weekly collector run: it parses whole national CMS files, and an automatic bulk
run should be a decision, not a side effect. It makes no network calls and no AI calls -
it reads the files collectors/state.py and the manual inbox already stored, rebuilds the
evidence rows (reruns replace, never duplicate), reconciles the CMS pool, and re-derives
every property's research depth.
"""
from __future__ import annotations

from collectors.common.log import RunTracker, get_logger

from . import cost_reports, cms_pool, gates
from .schema import create_research_tables

log = get_logger("research.ingest")
COLLECTOR = "research"


def ingest(con) -> RunTracker:
    """Run every stored-file lane, then the gates. Returns the run summary."""
    create_research_tables(con)
    with RunTracker(con, COLLECTOR) as run:
        for lane in (cms_pool.collect, cost_reports.collect):
            inner = lane(con)
            run.files_new += inner.files_new
            run.errors.extend(inner.errors)
            run.fallbacks.extend(inner.fallbacks)
        counts = gates.evaluate_all(con)
        log.info("%s: research depth %s", COLLECTOR,
                 {k: v for k, v in counts.items() if v})
    return run
