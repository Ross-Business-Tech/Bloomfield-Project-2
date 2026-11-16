"""
app.py - the researcher's dashboard (Part D).

    cd pipeline
    python -m scorer.weekly                     # this week's run on the real database
    streamlit run dashboard/app.py
    # made-up demo:
    python -m scorer.demo_week
    BLOOMFIELD_DB=data/demo.duckdb streamlit run dashboard/app.py

Pages: Ranked flags · Flag details · What changed this week · Research log · How the score works
Reads weekly_flags / weekly_runs (scorer/weekly.py). Writes only research_log.
"""
from __future__ import annotations

import html
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))

from scorer import equation as eq  # noqa: E402
from scorer import weekly  # noqa: E402
from scorer.schema import open_db  # noqa: E402

st.set_page_config(page_title="Bloomfield Flags", page_icon="🏚️", layout="wide")

from dashboard import style as _style  # noqa: E402
_style.inject()  # visual theme only; no data or scoring logic

TERMS = [("l_loan_clock", "L", "Loan clock", "loan_clock"), ("o_opening", "O", "Opening delay", "opening_delay"),
         ("c_creative", "C", "Creative signals", "creative")]
EX_KEY = {"L": "L_loan_clock", "O": "O_opening", "C": "C_creative"}
TIER_ICON = {"hot": "🔴 Priority Review", "watch": "🟠 Monitor", "low": "⚪ Limited Signals"}
# Presentation-only labels for stored tier words; suffixes like "(pre-default ...)" survive.
TIER_LABEL = {"Hot": "Priority Review", "Watch": "Monitor", "Low": "Limited Signals"}
# Words-only variant for tight spots (metric values) where the emoji dot crowds the fit.
TIER_WORDS = {"hot": "Priority Review", "watch": "Monitor", "low": "Limited Signals"}

def tier_label(t):
    if not t:
        return t
    first, _, rest = t.partition(" ")
    return " ".join(x for x in [TIER_LABEL.get(first, first), rest] if x)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def db_path():
    return os.environ.get("BLOOMFIELD_DB") or None


def get_con():
    if "con" not in st.session_state:
        con = open_db(db_path())
        weekly.create_tables(con)
        st.session_state.con = con
    return st.session_state.con


def runs(con):
    return [r[0] for r in con.execute("SELECT run_id FROM weekly_runs ORDER BY as_of DESC").fetchall()]


def flags(con, run_id):
    return con.execute("SELECT * FROM weekly_flags WHERE run_id = ? ORDER BY rank NULLS LAST, score DESC",
                       [run_id]).df()


def run_cfg(con, run_id):
    return json.loads(con.execute("SELECT settings_json FROM weekly_runs WHERE run_id = ?", [run_id]).fetchone()[0])


def latest_status(con):
    return dict(con.execute("""
        SELECT property_id, status FROM (SELECT property_id, status,
          row_number() OVER (PARTITION BY property_id ORDER BY logged_at DESC) n FROM research_log) WHERE n = 1
    """).fetchall())


def money(x):
    if x is None or pd.isna(x):
        return "size unknown"
    return f"${x / 1e6:.1f}M" if x >= 1e6 else f"${x:,.0f}"


# ---------------------------------------------------------------------------
# Token-gated research-ingest console (agent/ingest.py). Not linked anywhere;
# reachable only at ?ingest_key=<BLOOMFIELD_INGEST_KEY> while BLOOMFIELD_INGEST=1.
# Lets the authorized researcher download the flagged list (with property IDs)
# and upload evidence batches onto the volume, where the ingest thread verifies
# each row (fetch the cited page, verbatim-quote check) before storing it.
# No LLM calls and no paid search anywhere on this path.
# ---------------------------------------------------------------------------
def maybe_ingest_console():
    key = st.query_params.get("ingest_key")
    if key is None:
        return
    expected = os.environ.get("BLOOMFIELD_INGEST_KEY", "")
    st.title("Research ingest console")
    if os.environ.get("BLOOMFIELD_INGEST") != "1" or not expected or key != expected:
        st.error("Not authorized.")
        st.stop()
    con = get_con()
    all_runs = runs(con)
    if all_runs:
        run_id = all_runs[0]
        df = flags(con, run_id)
        shown = df[df["shown"]]
        st.subheader(f"Flagged list - {len(shown)} shown flags (run {run_id})")
        cols = [c for c in ["property_id", "rank", "property_name", "address", "city", "state",
                            "property_type", "borrower", "lender", "loan_amount", "score", "tier",
                            "deal_quality", "est_value", "ltv", "filter_reason", "summary", "explanation"]
                if c in shown.columns]
        st.download_button(f"Download flagged list (CSV, {len(shown)} rows)", shown[cols].to_csv(index=False),
                           f"flagged-{run_id}.csv", "text/csv")
        st.download_button(f"Download FULL universe (CSV, {len(df)} rows)", df[cols].to_csv(index=False),
                           f"universe-{run_id}.csv", "text/csv")
    st.subheader("Research ledger (latest assessment per property)")
    try:
        ra = con.execute(
            "SELECT property_id, research_score, research_tier, confidence, coverage, "
            "one_line, generated_at, ingested_at FROM research_assessments "
            "QUALIFY row_number() OVER (PARTITION BY property_id ORDER BY ingested_at DESC) = 1 "
            "ORDER BY property_id").fetchall()
        if ra:
            radf = pd.DataFrame(ra, columns=["property_id", "research_score", "research_tier",
                                             "confidence", "coverage", "one_line", "generated_at",
                                             "ingested_at"])
            st.download_button(f"Download research ledger (CSV, {len(radf)} properties)",
                               radf.to_csv(index=False), "research-ledger.csv", "text/csv")
        else:
            st.caption("No research assessments yet.")
    except Exception as e:
        st.caption(f"Could not read the research ledger: {e}")
    st.subheader("Upload a verified-evidence batch (JSONL)")
    st.caption("One JSON object per line: id, property_id, claim, value, url, quote, notes, summary. "
               "The ingest thread fetches each cited page and stores the claim only if the quote "
               "appears verbatim on the saved page - the same standard the research agent is held to.")
    up = st.file_uploader("JSONL batch", type=["jsonl"])
    if up is not None:
        from agent import ingest
        dest = ingest.incoming_dir(db_path()) / (
            f"ingest-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}.jsonl")
        dest.write_bytes(up.getvalue())
        st.success(f"Saved {dest.name}. The ingest thread verifies and stores each row within about "
                   "a minute; refusals and fetch failures are listed in the ingest_rows table.")
    st.subheader("Recent ingest rows")
    st.caption("Assessment rows (type=research_assessment) store the research-lane judgment; evidence "
               "rows are stored only after the quote is found verbatim on the fetched page.")
    try:
        led = con.execute(
            "SELECT id, property_id, status, detail, processed_at FROM ingest_rows "
            "ORDER BY processed_at DESC LIMIT 60").fetchall()
        if led:
            st.dataframe(pd.DataFrame(led, columns=["id", "property_id", "status", "detail", "processed_at"]),
                         hide_index=True, use_container_width=True)
        else:
            st.caption("Nothing ingested yet.")
    except Exception as e:
        st.caption(f"Could not read the ingest ledger: {e}")
    st.stop()


def esc(x):
    """Records come from outside documents: escape before showing."""
    return "" if x is None or (isinstance(x, float) and pd.isna(x)) else html.escape(str(x))


def research_assessments(con) -> dict:
    """Latest research-lane assessment per property: {property_id: (score, tier)}.
    Provisional judgment under rubric v2 - never the deterministic formula score."""
    try:
        rows = con.execute(
            "SELECT property_id, research_score, research_tier, one_line FROM research_assessments "
            "QUALIFY row_number() OVER (PARTITION BY property_id ORDER BY ingested_at DESC) = 1").fetchall()
        # tier stored may carry a flag suffix ("Low (pre-default ...)"); the word is the tier
        # null-score rows are explicit unscored/conflict markers: researched properties whose
        # filings cannot yet be reconciled into one honest number. Visible on the property
        # page; never counted as reviewed research and never a displayed score.
        return {r[0]: (r[1], (r[2] or "").split(" ")[0], r[3] or "") for r in rows if r[1] is not None}
    except Exception:
        return {}


def conflict_properties(con) -> set:
    """property_ids whose latest research assessment is an explicit unscored/conflict marker."""
    try:
        rows = con.execute(
            "SELECT property_id FROM research_assessments "
            "QUALIFY row_number() OVER (PARTITION BY property_id ORDER BY ingested_at DESC) = 1 "
            "AND research_score IS NULL").fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def assessed_only_properties(con, flag_ids: set) -> dict:
    """property_id -> display name for reviewed properties NOT in this weekly run
    (e.g. senior-housing reviews). Same resolution chain as the ingest path:
    property_links -> projects, then weekly_flags, then projects."""
    out = {}
    try:
        pids = [r[0] for r in con.execute("SELECT DISTINCT property_id FROM research_assessments").fetchall()]
    except Exception:
        return out
    for p in pids:
        if p in flag_ids:
            continue
        name = None
        for q in ("SELECT p.project_name FROM property_links pl "
                  "JOIN projects p ON p.project_id = pl.record_id AND pl.record_table = 'projects' "
                  "WHERE pl.property_id = ? LIMIT 1",
                  "SELECT property_name FROM weekly_flags WHERE property_id = ? LIMIT 1",
                  "SELECT project_name FROM projects WHERE project_id = ? LIMIT 1"):
            try:
                row = con.execute(q, [p]).fetchone()
            except Exception:
                row = None
            if row and row[0]:
                name = row[0]
                break
        out[p] = name or p
    return out


def _evidence_caveat(claim) -> str:
    """Inline honesty label: quote-matching proves the words are on the page, not that
    the claim is current or true. Allegations, leads and dated news stay labeled."""
    c = (claim or "").lower()
    if "allegation" in c:
        return "  :orange[allegation - a request or claim, not an adjudicated finding]"
    if "lead" in c:
        return "  :orange[unconfirmed lead - identity or facts not fully established]"
    if "news" in c:
        return "  :gray[dated news report - may not reflect current conditions]"
    return ""


def open_flag(pid):
    st.session_state.selected = pid
    st.session_state.selected_request = pid
    st.session_state.page = "Flag details"


def tags(r, assessed=False, r_tier=""):
    out = []
    if "STANDOUT" in (r["flags"] or ""):
        out.append(":violet-badge[⭐ STANDOUT (formula)]")
    if "COUNTER-SIGNAL" in (r["flags"] or ""):
        out.append(":blue-badge[↩ COUNTER-SIGNAL (formula)]")
    if assessed:
        # Research lane reviewed this property; the badge must agree with the
        # provisional research score on the right, not the legacy formula score.
        out.append({"hot": ":red-badge[Reviewed: Priority Review]",
                    "watch": ":orange-badge[Reviewed: Monitor]",
                    "low": ":gray-badge[Reviewed: Limited Signals]"}.get(
                        r_tier, ":gray-badge[Reviewed]"))
        return " ".join(out)
    if r["stage"] == "ready_to_act":
        out.append(":red-badge[✅ READY TO ACT (formula, unreviewed)]")
    elif r["score"] and r["score"] > 0:
        out.append(":green-badge[Early warning (formula, unreviewed)]")
    return " ".join(out)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def loan_data_counts(con) -> dict:
    """How much loan data this database actually has (the loan clock and loan amounts depend on it)."""
    def count(sql):
        try:
            return con.execute(sql).fetchone()[0]
        except Exception:                           # table not created yet
            return 0
    return {
        "recorded_loans": count("SELECT count(*) FROM recorded_loans"),
        "reader_loans": count("SELECT count(*) FROM filing_fields WHERE lower(filing_type) = 'recorded_loan'"),
        "recorder_files": count("SELECT count(*) FROM raw_files WHERE source = 'recorder'"),
        "unread_recorder_files": count("SELECT count(*) FROM raw_files r WHERE r.source = 'recorder' AND NOT EXISTS "
                                       "(SELECT 1 FROM filing_fields f WHERE f.file_id = r.file_id)"),
    }


def loan_notice(con):
    """Say plainly when there is no loan data, instead of letting every loan value read 0."""
    c = loan_data_counts(con)
    loans = c["recorded_loans"] + c["reader_loans"]
    if loans:
        st.caption(f"Loan data in this run: {loans} recorded loan(s) "
                   f"({c['recorded_loans']} from recorder index rows, {c['reader_loans']} read from recorder documents).")
        return
    if c["unread_recorder_files"]:
        st.warning(f"**No loan values yet:** {c['unread_recorder_files']} recorder document(s) are waiting for the AI reader. "
                   "Run `python -m bot --full` with `OPENAI_API_KEY` set so they become loans; until then the loan clock "
                   "(L) and loan amounts read 0 / size unknown.")
    else:
        st.warning("**No loan values yet:** this run has no recorder documents, so the loan clock (L) is 0 and loan "
                   "amounts read 'size unknown' for every property. The county recorders are still manual: drop recorder "
                   "documents in `data/inbox/recorder/<ST>/<county>/` (or switch a checked recorder to automatic), then "
                   "run `python -m bot --full` with `OPENAI_API_KEY` set.")

def research_status(con) -> str:
    """One honest line about the sourcing agent's work on THIS database: how many research
    goals finished, how much verified evidence they saved, and the estimated model cost so
    far. Shown on the weekly list so research coverage is visible next to the ranks. The
    agent writes its own tables; if they do not exist here, it has never run on this data."""
    try:
        goals, ev = con.execute(
            "SELECT (SELECT count(*) FROM agent_runs WHERE status IN ('done', 'step_cap')), "
            "       (SELECT count(*) FROM evidence)").fetchone()
        usd = con.execute("SELECT COALESCE(sum(est_cost_usd), 0) FROM llm_usage WHERE task = 'agent'").fetchone()[0]
    except Exception:
        goals, ev, usd = 0, 0, 0.0
    reviewed = len(research_assessments(con))
    n_conf = len(conflict_properties(con))
    conf_tail = (f"; {n_conf} conflict{'s' if n_conf != 1 else ''} under review, "
                 "unscored until reconciled" if n_conf else "")
    tail = (f" · Properties with reviewed research: {reviewed} propert{'y' if reviewed == 1 else 'ies'} "
            "(reviewed scores drive the list; depth varies - each page shows its coverage" + conf_tail + ")"
            if reviewed else "")
    if not goals:
        return ("🔎 Automated first pass (earlier, unreviewed): has not run on this database yet "
                "(0 properties researched, 0 evidence records)." + tail)
    return (f"🔎 Automated first pass (earlier, unreviewed): {goals} propert{'y' if goals == 1 else 'ies'} researched · "
            f"{ev} evidence record{'s' if ev != 1 else ''} · model usage ≈ ${usd:.2f} (estimate)" + tail)


def research_diagnostics(con) -> str:
    """Non-secret live diagnostics for the research pass: whether the search service and the
    model key are CONFIGURED (yes/no only - never the values), the latest pass row, the most
    recent goals and the most recent FAILED tool calls (which carry the plain-words reason,
    e.g. 'No search service is configured'). Read-only; safe to show on the public app."""
    lines = []
    try:
        import yaml
        bot_cfg = yaml.safe_load((PIPELINE / "config" / "bot.yaml").read_text()) or {}
        provider = ((bot_cfg.get("search") or {}).get("provider", "none"))
    except Exception as e:
        provider = f"unreadable ({e})"
    key_env = "TAVILY_API_KEY" if provider == "tavily" else "BRAVE_SEARCH_API_KEY"
    key_set = bool(os.environ.get(key_env, "").strip())
    try:
        from collectors.common import config as _c  # loads .env into os.environ
        _c.load_env()
    except Exception:
        pass
    llm_set = any(os.environ.get(n, "").strip()
                  for n in ("LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"))
    lines.append(f"search provider: {provider} · {key_env} set: {'yes' if key_set else 'NO'} · "
                 f"LLM key set: {'yes' if llm_set else 'NO'}")
    try:
        row = con.execute("SELECT started_at, status, goals_run, goals_skipped, evidence_new, "
                          "llm_calls, search_calls FROM weekly_research_passes ORDER BY started_at DESC "
                          "LIMIT 1").fetchone()
        if row:
            lines.append(f"latest pass: started {row[0]} · status {row[1]} · goals {row[2]} "
                         f"(skipped {row[3]}) · new evidence {row[4]} · llm calls {row[5]} · searches {row[6]}")
    except Exception:
        pass
    try:
        nfiles = con.execute("SELECT count(*) FROM raw_files").fetchone()[0]
        cruns = con.execute("SELECT collector_name, status, started_at, files_new, errors "
                            "FROM collector_runs ORDER BY started_at DESC LIMIT 4").fetchall()
        lines.append(f"collected documents in this database: {nfiles}")
        for c in cruns:
            lines.append(f"collector {c[0]}: {c[1]} at {c[2]} · {c[3]} new · {c[4]} errors")
    except Exception:
        pass
    try:
        runs = con.execute("SELECT started_at, status, llm_calls, cost_usd, "
                           "substr(goal, 1, 90), substr(coalesce(final_answer, ''), 1, 220) "
                           "FROM agent_runs ORDER BY started_at DESC LIMIT 3").fetchall()
        for r in runs:
            lines.append(f"goal {r[0]}: {r[1]} · {r[2]} calls · ${r[3]:.4f} · {r[4]}")
            if r[5]:
                lines.append(f"  last words: {r[5]}")
    except Exception:
        pass
    try:
        fails = con.execute("SELECT created_at, name, substr(detail, 1, 220) FROM agent_steps "
                            "WHERE kind = 'tool_result' AND detail LIKE '%\"ok\": false%' "
                            "ORDER BY created_at DESC LIMIT 3").fetchall()
        for f_ in fails:
            lines.append(f"FAILED tool {f_[1]} at {f_[0]}: {f_[2]}")
    except Exception:
        pass
    try:
        evs = con.execute("SELECT subject, claim, source_url FROM evidence "
                          "ORDER BY observed_at DESC LIMIT 3").fetchall()
        for e in evs:
            lines.append(f"latest evidence: {e[0]} - {e[1]} - {e[2]}")
    except Exception:
        pass
    return "\n".join(lines) if len(lines) > 1 else lines[0]


def page_ranked(con, run_id):
    df, cfg = flags(con, run_id), run_cfg(con, run_id)
    f = cfg["filters"]
    status = latest_status(con)
    st.title("This week's flags")
    st.caption(f"Bloomfield Opportunity Score {cfg['version']} · {run_id.replace('week-', 'week of ')} · "
               f"{(', '.join(f['property_types_excluded']) + ' hidden') if f['property_types_excluded'] else 'all property types shown'} · loans from {money(f['min_loan_size'])}"
               f"{' to ' + money(f['max_loan_size']) if f.get('max_loan_size') else ' (no upper limit)'} · {'states ' + ', '.join(f['states']) if f['states'] else 'all U.S.'}")
    loan_notice(con)

    st.caption(research_status(con))
    with st.expander("Research diagnostics"):
        st.text(research_diagnostics(con))
    hide_office = st.toggle("Hide office properties", value=False,
                            help="Office is shown by default. Turn this on to filter it out of the list and the counts.")
    if hide_office:
        df = df[df["property_type"].fillna("") != "Office"]
    ranked = df[df["rank"].notna()]
    live = eq.load_equation_config()["filters"]
    if {k: f.get(k) for k in ("min_loan_size", "max_loan_size", "property_types_excluded")} != \
            {k: live.get(k) for k in ("min_loan_size", "max_loan_size", "property_types_excluded")}:
        st.warning("This run was made with older filter settings than the ones now in config/equation.yaml "
                   f"(it used loans {money(f['min_loan_size'])}–{money(f['max_loan_size']) if f.get('max_loan_size') else 'no limit'}, "
                   f"hiding {', '.join(f['property_types_excluded']) or 'nothing'}). "
                   "Pull the latest code and run `python -m scorer.weekly` again to apply the current settings.")
    if len(ranked) and not (ranked["score"] > 0).any():
        st.info("None of these properties has an early warning sign yet. They are listed because they pass the filters; "
                "they are known only from public loan data. Add county records or run the web bot to find signals.")
    # Displayed scores/tiers/ranking come from the reviewed research assessments
    # (provisional judgment, rubric v2) where one exists. The deterministic formula
    # still runs underneath (its rows are untouched); unresearched properties are
    # unscored - never zero and never Low.
    rassess = research_assessments(con)
    conflicts = conflict_properties(con)
    ranked = ranked.assign(
        _eff_score=ranked["property_id"].map(lambda p: rassess.get(p, (None, None))[0]),
        _eff_tier=ranked["property_id"].map(lambda p: (rassess.get(p, (None, None))[1] or "unscored").lower()))
    c = st.columns(5)
    c[0].metric("Listed", len(ranked))
    c[1].metric("Priority Review (call this week)", int((ranked["_eff_tier"] == "hot").sum()))
    c[2].metric("Monitor", int((ranked["_eff_tier"] == "watch").sum()))
    c[3].metric("Reviewed (this list)", int(ranked["_eff_score"].notna().sum()))
    c[4].metric("Filtered out", int((~df["shown"]).sum()))
    if rassess:
        st.caption("Scores and tiers are the reviewed research assessments (provisional judgment, "
                   "rubric v3.3). Unreviewed properties show as unscored - missing research is not a "
                   "clean bill of health. The deterministic formula still runs underneath for audit. "
                   "Coverage depth varies by property; each assessment lists its checked lanes. "
                   f"Priority Review / Monitor / Reviewed counts cover this flagged list; {len(rassess)} properties have "
                   "been reviewed in total, including unflagged senior-housing properties (open them "
                   "from the Flag details page)." +
                   (f" {len(conflicts)} conflict{'s' if len(conflicts) != 1 else ''} under review - "
                    "unscored until the filings reconcile, never counted as reviewed research."
                    if conflicts else ""))

    if len(ranked) == 0 and len(df) and not df["shown"].any():
        why = df["filter_reason"].fillna("unknown").value_counts()
        st.warning("Every flag is hidden by the filters. Reasons: " + "; ".join(f"{n} × {r}" for r, n in why.items())
                   + ". Turn on 'Show filtered-out' to see them, or change `filters:` in config/equation.yaml.")

    assessed_first = ranked.assign(_a=ranked["_eff_score"].notna()).sort_values(
        ["_a", "_eff_score", "score"], ascending=[False, False, False])
    tier_pick = st.multiselect("Tiers", ["hot", "watch", "low", "unscored"],
                               default=["hot", "watch", "low", "unscored"],
                               format_func=lambda t: TIER_ICON.get(t, "⚪ Unscored (not researched yet)"))
    view = assessed_first[assessed_first["_eff_tier"].isin(tier_pick)]
    # Cards are expensive to render; hundreds of rows kept the script "Running" for
    # minutes and every button stayed a skeleton. Show the strongest flags as cards
    # and point to the All properties table for the full scored universe.
    MAX_CARDS = 50
    if len(view) > MAX_CARDS:
        st.caption(f"Showing the top {MAX_CARDS} of {len(view)} ranked flags. "
                   "The **All properties** page lists every scored property.")
        view = view.head(MAX_CARDS)
    if (df["rank"].isna()).any():
        st.caption(f"{int(df['rank'].isna().sum())} more properties were screened by the deterministic formula "
                   "(no distress signals or context found yet) and not yet reviewed in depth "
                   "(missing data is not proof of absence). They are on the **All properties** page.")
    for pos, r in enumerate(view.to_dict("records"), start=1):
        with st.container(border=True):
            a, b, c3 = st.columns([0.7, 5, 1.8])
            a.markdown(f"### {pos}")
            place = ", ".join(x for x in [r["city"], r["state"]] if x and not pd.isna(x))
            b.markdown(f"**{esc(r['property_name'])}** — {esc(r['property_type'] or 'type unknown')} · "
                       f"{esc(place)} · {money(r['loan_amount'])}")
            eff_score, eff_tier = r["_eff_score"], r["_eff_tier"]
            assessed = pd.notna(eff_score) and eff_score is not None
            b.markdown(tags(r, assessed, eff_tier))
            if assessed:
                one = rassess.get(r["property_id"], (None, None, ""))[2]
                b.caption(esc(one) if one else "Reviewed research on file - see the assessment on the property page.")
            elif r["property_id"] in conflicts:
                b.caption("Conflicting filings under review - no honest single score yet; see the property page.")
            else:
                parts = [f"{code} {r[col]:.0%}" for col, code, _, _ in TERMS if r.get(col) and r[col] > 0]
                b.caption(("Formula signals (unreviewed): " + " · ".join(parts)) if parts else ("No formula signals (late filings only)" if r["score"] and r["score"] > 0 else "No formula signals found; not yet reviewed in depth"))
            if not r["shown"]:
                b.markdown(f":gray[Hidden: {esc(r['filter_reason'])}]")
            if r["property_id"] in status:
                b.markdown(f":green[Research: {weekly.STATUS_LABELS[status[r['property_id']]]}]")
            if assessed:
                c3.markdown(f"**{eff_score:.0f}** / 100 · {TIER_ICON[eff_tier]}")
                c3.progress(min(1.0, eff_score / 100))
                c3.caption("Research score (provisional)")
            elif r["property_id"] in conflicts:
                c3.markdown("**—** / 100 · ⚪ Unscored (conflict)")
                c3.progress(0.0)
                c3.caption("Filings conflict - under review")
            else:
                c3.markdown("**—** / 100 · ⚪ Unscored")
                c3.progress(0.0)
                c3.caption("Not researched yet - not a clean bill of health")
            c3.caption(f"Deal quality {r['deal_quality']}")
            c3.button("Open", key=f"open-{r['property_id']}", on_click=open_flag, args=(r["property_id"],),
                      use_container_width=True)


def page_detail(con, run_id):
    df = flags(con, run_id)
    if df.empty:
        st.info("No flags in this run.")
        return
    ids = df["property_id"].tolist()
    names = dict(zip(df["property_id"], df["property_name"]))
    extra = assessed_only_properties(con, set(ids))
    names.update(extra)
    ids = ids + sorted(extra, key=lambda p: names[p].lower())
    # Unique labels keyed on property_id: name + address + loan amount disambiguate
    # rows that share a property name (each option routes to its own row).
    amt = dict(zip(df["property_id"], df["loan_amount"]))
    addr = dict(zip(df["property_id"], df["address"]))
    def _flag_label(k):
        base = names[k]
        if k in extra:
            return base
        av = addr.get(k)
        a = av.strip() if isinstance(av, str) else ""
        if len(a) > 42:
            a = a[:39].rstrip() + "..."
        v = amt.get(k)
        m = money(v) if isinstance(v, (int, float)) and not pd.isna(v) else ""
        parts = [base] + ([a] if a else []) + ([m] if m else [])
        return " - ".join(parts)
    # Widget keyed on property_id. External "Open" buttons set selected_request
    # (one-shot), applied to the widget state before the widget is created this
    # run; the widget itself owns the state afterwards, so a user's pick can
    # never be forced back to a stale value on rerun.
    req = st.session_state.pop("selected_request", None)
    if req in ids:
        st.session_state["flag_pid"] = req
    if st.session_state.get("flag_pid") not in ids:
        st.session_state["flag_pid"] = ids[0]
    pid = st.selectbox("Property", ids, key="flag_pid", format_func=_flag_label)
    st.session_state.selected = pid
    if pid in extra:
        # Reviewed property outside the weekly run: no formula audit row exists.
        # The reviewed assessment and evidence sections below work off property_id alone.
        r = {"property_id": pid, "property_name": names[pid],
             "property_type": "Senior housing / care", "address": "",
             "borrower": "", "lender": "", "flags": "", "stage": "", "score": None,
             "shown": True, "filter_reason": None, "est_value": None, "ltv": None,
             "loan_amount": None, "deal_quality": "-", "summary": ""}
        ex = None
    else:
        r = df[df["property_id"] == pid].iloc[0].to_dict()
        ex = json.loads(r["explanation"])
    cfg = run_cfg(con, run_id)
    w = cfg["weights"]

    st.title(r["property_name"])
    st.caption(f"{esc(r['property_type'] or 'type unknown')} · {esc(r['address'] or '')} · "
               f"borrower {esc(r['borrower']) or 'unknown'} · lender {esc(r['lender']) or 'unknown'}")
    st.markdown(tags(r))
    ra_head = research_assessments(con).get(pid)
    conflict = pid in conflict_properties(con)
    c = st.columns(4)
    if ra_head:
        c[0].metric("Research score (provisional)", f"{ra_head[0]:.0f}")
        c[1].metric("Tier (reviewed)", TIER_WORDS.get(ra_head[1].lower().split(" ")[0], tier_label(ra_head[1])))
    else:
        c[0].metric("Research score (provisional)", "—")
        c[1].metric("Tier", "⚪ Unscored")
    c[2].metric("Deal quality", r["deal_quality"] or "-")
    c[3].metric("Loan", money(r["loan_amount"]) if r.get("loan_amount") else "-")
    if conflict:
        st.warning("Conflicting filings under review - no honest single score yet. This is NOT a "
                   "scored or cleared review: the unreconciled debt scopes are laid out in the research "
                   "assessment below. The formula summary and equation further down are the automated "
                   "audit view only, not a reviewed score.")
    else:
        st.caption("Displayed score/tier is the reviewed research assessment (provisional judgment, "
                   "rubric v3.3); unreviewed properties are unscored. Where a formula audit row exists "
                   "it is kept below as the audit view, not the displayed score.")
    if r.get("est_value") and not pd.isna(r["est_value"]):
        st.caption(f"Estimated stabilized value {money(r['est_value'])} (revenue at stabilized occupancy − latest "
                   f"12-month expenses, ÷ the {r['property_type']} cap rate, − cost to stabilize) · owed / value {r['ltv']:.0%}")
    if not r["shown"]:
        st.warning(f"Filtered out by settings: {r['filter_reason']}")
    if ex:
        if conflict:
            pass   # conflict notice above carries the status; the raw formula summary is not shown
        elif ra_head:
            st.info("Older automated formula summary (audit view only - the reviewed research "
                    "assessment below drives the displayed score): " + r["summary"])
        else:
            st.info(r["summary"])

        st.subheader("The equation for this property" + (" (formula audit view)" if (ra_head or conflict) else ""))
        rows = []
        for col, code, label, key in TERMS:
            t = ex[EX_KEY[code]]
            rows.append({"Term": f"{code} · {label}", "Strength": t["strength"], "Weight": w[key],
                         "Points before anti-fluke": round(w[key] * t["strength"], 1), "Why": t["why"]})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                     column_config={"Strength": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
        cs_rows = ex.get("creative_signals", [])
        if cs_rows:
            st.markdown("**Inside C: each creative signal** — C = 1 − Π(1 − reliability × strength)")
            st.dataframe(pd.DataFrame([{"Signal": f"{x['code']} · {x['name']}", "Strength": x["strength"],
                                        "Reliability": x["reliability"], "Counts as": x["effective"],
                                        "Why": x["why"] if x["known"] else "no data yet"} for x in cs_rows]),
                         hide_index=True, use_container_width=True,
                         column_config={"Strength": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
        a, b = st.columns(2)
        with a:
            st.markdown(f"**Anti-fluke:** {ex['anti_fluke']}")
            for s in ex.get("standouts", []):
                st.markdown(f"⭐ **Standout** (counts ×max({cfg['standout']['boost']}, rescale), skips anti-fluke): {esc(s)}")
            for s in ex.get("counter_signals", []):
                st.markdown(f"↩ **Counter-signal** (×{cfg['counter_signal']['factor']}): {esc(s)}")
            st.markdown(f"**Early points:** {ex['early_points']}")
        with b:
            st.markdown(f"**Ready-to-act confirmation (court/lien filings, max {cfg['late']['max_points']}):** {ex['late_points']}")
            for s in ex.get("late_why", []):
                st.markdown(f"- {esc(s)}")
            st.markdown(f"**Score = min(100, {ex['early_points']} + {ex['late_points']}) = {ex['score']}**")

        ctx = json.loads(r["context"])
        if ctx:
            st.subheader("Context (shown, not scored)")
            st.caption("Bloomfield: auctions and CMBS data are public or everyone has them.")
            for s in ctx:
                st.markdown(f"- {esc(s)}")

        a, b = st.columns(2)
        with a:
            st.subheader("Why this deal quality")
            st.dataframe(pd.DataFrame(json.loads(r["deal_parts"])), hide_index=True, use_container_width=True)
        with b:
            st.subheader("Deal angle for Bloomfield")
            for s in json.loads(r["angles"]) or ["No clear angle yet: keep watching."]:
                st.markdown(f"- {s}")

    else:
        st.caption("This property is not part of the weekly flagged run, so there is no formula audit view - "
                   "the reviewed assessment below is the complete evaluation.")

    st.subheader("AI research")
    aruns = con.execute(
        "SELECT status, started_at, final_answer FROM agent_runs "
        "WHERE goal LIKE '%[property_id=' || ? || ']%' "
        "ORDER BY started_at DESC LIMIT 3", [pid]).fetchall()
    if not aruns:
        st.caption("The research agent has not covered this property yet.")
    for a_status, a_started, a_final in aruns:
        when = pd.Timestamp(a_started).strftime("%b %d %H:%M") + " UTC"
        if a_status == "running":
            st.markdown(f"**Researcher is working on this property now** (started {when}).")
        elif a_final:
            st.markdown(f"**Researcher ({when}, {esc(a_status)}):** {esc(a_final)}")
        else:
            st.markdown(f"**Researcher ({when}):** run ended with status `{esc(a_status)}`, no summary written.")
    ev_rows = con.execute(
        "SELECT e.claim, e.value, e.source_kind, e.source_url, e.quote, e.observed_at "
        "FROM evidence e JOIN agent_runs r ON r.run_id = e.run_id "
        "WHERE r.goal LIKE '%[property_id=' || ? || ']%' "
        "ORDER BY e.observed_at DESC", [pid]).fetchall()
    if ev_rows:
        st.markdown(f"**Source-matched evidence ({len(ev_rows)}):** the quote appears verbatim on the "
                    "saved page at the address shown. That proves the words are on that page - it does "
                    "NOT by itself prove the claim is current or true. Allegations, unconfirmed leads "
                    "and dated reports are labeled next to each row.")
        for e_claim, e_value, e_kind, e_url, e_quote, e_obs in ev_rows:
            val = f" = **{esc(str(e_value))}**" if e_value not in (None, "") else ""
            st.markdown(f"- {esc(e_claim)}{val}{_evidence_caveat(e_claim)}")
            if e_kind == "web_page":
                if isinstance(e_url, str) and e_url.startswith(("https://", "http://")):
                    st.caption(f'web proof: "{esc(e_quote)}" - {esc(e_url)} · {pd.Timestamp(e_obs):%b %d %H:%M} UTC')
                else:
                    st.caption(f'web proof: "{esc(e_quote)}" (source address on file) · {pd.Timestamp(e_obs):%b %d %H:%M} UTC')
            else:
                st.caption(f"database proof: {esc(e_url)} · {pd.Timestamp(e_obs):%b %d %H:%M} UTC")
    elif aruns:
        latest = aruns[0][0]
        if latest == "done":
            st.caption("The researcher completed this property and saved no verified evidence: "
                       "it found nothing it could verify, or sources blocked it. An honest empty "
                       "result, not a skipped property.")
        elif latest == "running":
            st.caption("Research in progress; no verified evidence saved yet.")
        else:
            st.caption(f"The last research run ended with status '{esc(latest)}' and saved no "
                       "verified evidence. The property stays pending and will be retried.")

    # Research-lane provisional assessment (rubric v2): a structured review of the evidence,
    # kept strictly separate from the deterministic formula score above.
    try:
        ra_row = con.execute(
            "SELECT research_score, research_tier, confidence, coverage, score_breakdown, "
            "score_reasons, artifact, generated_at FROM research_assessments "
            "WHERE property_id = ? ORDER BY ingested_at DESC LIMIT 1", [pid]).fetchone()
    except Exception:
        ra_row = None
    if ra_row:
        r_score, r_tier, r_conf, r_cov, r_bd, r_reasons, r_artifact, r_gen = ra_row
        st.subheader("Research assessment (provisional)")
        st.caption("A structured independent research review under rubric v3.3, separate from the "
                   "formula score above. Reviewed-score thresholds (Priority Review 60+, Monitor 20-59, Limited Signals under 20) are not "
                   "the formula's audit thresholds (hot 50+ / watch 25+). Versioned provisional judgment, not a "
                   "probability. Unchecked lanes are unknowns, never counted as healthy.")
        r_color = {"Hot": "red", "Watch": "orange", "Low": "gray"}.get((r_tier or "").split(" ")[0], "gray")
        if r_score is None:
            st.markdown(f"**Unscored - conflicting filings under review · :gray[{esc(tier_label(r_tier))}]**")
        else:
            st.markdown(f"**Research score {r_score:.0f} / 100 · :{r_color}[{esc(tier_label(r_tier))} (reviewed)]**")
        if r_conf:
            st.markdown(f"**Confidence:** {esc(r_conf)}")
        if r_cov:
            st.markdown(f"**Coverage:** {esc(r_cov)}")
        try:
            bd = json.loads(r_bd or "{}")
            if isinstance(bd, dict) and bd:
                st.markdown("**Score breakdown:** " + " · ".join(f"{esc(k)} {v}" for k, v in bd.items()))
        except Exception:
            pass
        try:
            reasons = json.loads(r_reasons or "[]")
            if isinstance(reasons, list) and reasons:
                with st.expander("Reasons, qualifications and counter-evidence", expanded=True):
                    for reason in reasons:
                        st.markdown(f"- {esc(str(reason))}")
        except Exception:
            pass
        if r_gen:
            st.caption(f"Research lane · assessment generated {esc(r_gen)}")

    st.subheader("Research log")
    with st.form(f"log-{pid}", clear_on_submit=True):
        x, y = st.columns([2, 1])
        status = x.selectbox("What happened", weekly.RESEARCH_STATUSES, format_func=lambda s: weekly.STATUS_LABELS[s])
        who = y.text_input("Researcher", value=st.session_state.get("researcher", ""))
        note = st.text_area("Note", placeholder="e.g. Called the principal; open to a bridge loan.")
        amount = st.number_input("Deal amount ($, only for Deal closed)", min_value=0, step=100_000)
        if st.form_submit_button("Save to research log"):
            st.session_state.researcher = who
            con.execute("INSERT INTO research_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
                uuid.uuid4().hex, pid, r["property_name"], status, note,
                float(amount) if status == "deal_closed" and amount else None, who or None,
                datetime.now(timezone.utc).replace(tzinfo=None)])
            st.success("Saved.")
    hist = con.execute("SELECT * FROM research_log WHERE property_id = ? ORDER BY logged_at DESC", [pid]).df()
    if hist.empty:
        st.caption("Nothing logged for this flag yet.")
    for h in hist.to_dict("records"):
        amt = f" · {money(h['deal_amount'])}" if pd.notna(h["deal_amount"]) else ""
        st.markdown(f"- **{weekly.STATUS_LABELS.get(h['status'], h['status'])}**{amt} — {esc(h['note'])} "
                    f":gray[({pd.Timestamp(h['logged_at']):%b %d, %Y}{', ' + esc(h['researcher']) if h['researcher'] else ''})]")


def _list(rows, prefix, extra=None):
    if not rows:
        st.caption("None.")
    for r in rows:
        a, b = st.columns([5, 1])
        line = f"**{esc(r['property_name'])}** — score {r['score']:.0f} ({TIER_ICON[r['tier']]})"
        if extra:
            line += f" · {extra(r)}"
        a.markdown(line)
        a.caption(esc(r["summary"]))
        b.button("Open", key=f"{prefix}-{r['property_id']}", on_click=open_flag, args=(r["property_id"],))


def page_changes(con, run_id):
    st.title("What changed this week")
    ch = weekly.changes(con, run_id, run_cfg(con, run_id))
    if not ch["previous"]:
        st.info("This is the first weekly run, so there's nothing to compare yet.")
        return
    st.caption(f"{ch['current'].replace('week-', 'Week of ')} compared with {ch['previous'].replace('week-', 'week of ')}.")
    c = st.columns(5)
    for col, (label, key) in zip(c, [("New", "new"), ("Got worse", "worse"), ("New standouts", "new_standouts"),
                                     ("New filings", "new_late"), ("Dropped off", "dropped")]):
        col.metric(label, len(ch[key]))
    st.subheader("🆕 New flags")
    _list(ch["new"], "new")
    st.subheader("🔺 Got worse")
    _list(ch["worse"], "worse", lambda r: f"up {r['delta']} from {r['previous_score']:.0f}")
    st.subheader("⭐ Became a standout")
    _list(ch["new_standouts"], "so")
    st.subheader("✅ Became ready to act (new court filing or lien)")
    _list(ch["new_late"], "late", lambda r: "now READY TO ACT")
    st.subheader("🔻 Got better")
    _list(ch["better"], "better", lambda r: f"down {-r['delta']} from {r['previous_score']:.0f}")
    st.subheader("✔️ Dropped off")
    if not ch["dropped"]:
        st.caption("None.")
    for r in ch["dropped"]:
        st.markdown(f"- **{esc(r['property_name'])}** — no longer flagged (e.g. it opened or refinanced).")


def page_log(con, run_id):
    st.title("Research log")
    cfg = run_cfg(con, run_id)
    goal, last = cfg["goal"]["annual_deal_volume"], cfg["goal"]["last_year_volume"]
    log = con.execute("SELECT * FROM research_log ORDER BY logged_at DESC").df()
    closed = float(log.loc[log["status"] == "deal_closed", "deal_amount"].fillna(0).sum()) if len(log) else 0.0
    st.subheader("Progress toward the $300M goal")
    c = st.columns(3)
    c[0].metric("Closed from flags", money(closed))
    c[1].metric("Growth needed this year", money(goal - last))
    c[2].metric("Share of growth from flags", f"{closed / (goal - last):.0%}")
    st.progress(min(1.0, closed / (goal - last)))
    status = latest_status(con)
    counts = pd.Series(list(status.values())).value_counts() if status else pd.Series(dtype=int)
    st.subheader("Funnel")
    st.bar_chart(pd.DataFrame({"Stage": [weekly.STATUS_LABELS[s] for s in weekly.RESEARCH_STATUSES],
                               "Flags": [int(counts.get(s, 0)) for s in weekly.RESEARCH_STATUSES]}),
                 x="Stage", y="Flags", horizontal=True, sort=False)
    st.caption("Each flag counts once, at its latest stage.")
    st.subheader("Every entry")
    if log.empty:
        st.caption("Nothing logged yet.")
        return
    view = pd.DataFrame({
        "When": pd.to_datetime(log["logged_at"]).dt.strftime("%b %d, %Y"), "Flag": log["property_name"],
        "Stage": log["status"].map(weekly.STATUS_LABELS),
        "Amount": log["deal_amount"].map(lambda x: money(x) if pd.notna(x) else ""),
        "Note": log["note"], "Researcher": log["researcher"]})
    st.dataframe(view, hide_index=True, use_container_width=True)
    st.download_button("Download as CSV", view.to_csv(index=False), "research_log.csv", "text/csv")


def page_how(con, run_id):
    cfg = run_cfg(con, run_id)
    w, so, cs, late = cfg["weights"], cfg["standout"], cfg["counter_signal"], cfg["late"]
    st.title("How the score works")
    with st.expander("What we heard from Bloomfield, and what we changed", expanded=True):
        st.markdown("""
**What we heard**
- Construction loan still on the clock is the **strongest** signal. A project that opened with its construction loan still outstanding, or a loan from five years ago never paid off, is distress.
- Opening date watch is good but **weaker**. A 3–6 month delay is normal.
- Creative signals (rent vs. the city, a site gone quiet) are very promising once we can detect them reliably.
- Court and lien filings come **late**: the situation is already actionable, not an early warning.
- Forced and public sales are not really a signal, and CMBS data gives little edge because everyone has it.

**What we changed**
- The construction-loan clock (50) and opening date watch (20) outweigh every other signal; a building that opened with its loan still outstanding gets an extra boost.
- Court and lien filings add at most a few points and tag the flag **✅ READY TO ACT**.
- Auctions and CMBS are shown as context only, never scored.
- County records are found through **NETR Online** (`python -m collectors.discover`), recorders first because they feed the loan clock.
""")
    st.latex(r"C = 1-\prod_{j\in\{R,I,Q,V,X,K,L_q,W\}}\left(1-q_j\,s_j\right)")
    st.latex(r"\text{Early}=\sum_{L,O,C} w_i\, s_i \times \begin{cases}\max(\text{boost}(s_i),\ \text{rescale}) & s_i\ge %s\\ \text{rescale} & \text{otherwise}\end{cases}"
             % so.get("boost_starts_at", 0.6))
    st.latex(r"\text{boost}(s)=1+%s\times\frac{s-%s}{%s-%s}" % (so.get("max_boost", 1.5) - 1, so.get("boost_starts_at", 0.6),
                                                             so.get("boost_full_at", 1.0), so.get("boost_starts_at", 0.6)))
    st.latex(r"\text{rescale}=\min\!\left(%s,\ \frac{100}{\text{weights of terms that can apply}}\right)" % cfg["applicability"]["max_rescale"])
    st.latex(r"\text{Score}=\min\!\left(100,\ \text{Early}\times %s^{\,\#\text{counter-signals}}+\text{Late}\right)" % cs["factor"])
    lc, od, rd, ig = cfg["loan_clock"], cfg["opening_delay"], cfg["rent_decline"], cfg["inspection_gap"]
    c = cfg["creative"]
    qo, iv, xs, kc, lq = (c["occupancy_slide"], c["income_erosion"], c["cost_squeeze"], c["concession_creep"],
                          c["licensing_quality"])
    st.dataframe(pd.DataFrame([
        {"Term": "L · Loan clock (construction, or any short-term loan that needed an extension)", "Weight": w["loan_clock"], "0 → 1": f"{lc['start_months']} → {lc['full_months']} months unpaid, +{lc['per_extension']} per extension, +{lc.get('opened_still_unpaid_bonus', 0)} if the building opened with the loan still outstanding", "From": "County recorder (recorded_loans / Reader) + public SEC loan filings: past maturity = 1.0, 'modified' flag = an extension"},
        {"Term": "O · Opening delay", "Weight": w["opening_delay"], "0 → 1": f"{od['start_months']} → {od['full_months']} months late (under {od['start_months']} is normal)", "From": f"Project websites (Part B projects); if none, a public new-construction permit with no certificate of occupancy (months since issue − {od.get('expected_build_months', 24)})"},
        {"Term": "C · Creative signals (combined)", "Weight": w["creative"], "0 → 1": "1 − Π(1 − q·s) over the signals below", "From": "see below"},
    ]), hide_index=True, use_container_width=True)
    st.markdown(f"**Creative signals** (weight {w['creative']} combined; each counts as reliability q × strength s; together at most {c['max_confirmations']} anti-fluke confirmations)")
    st.dataframe(pd.DataFrame([
        {"Signal": "R · Rent vs. city", "s = 0 → 1": f"(city rent change − property rent change) ÷ {rd['full_gap']:.0%}", "q": rd["reliability"], "From": "B rent_observations + A market_rents"},
        {"Signal": "I · Site inactivity", "s = 0 → 1": f"{ig['start_days']} → {ig['full_days']} days since a city inspection", "q": ig["reliability"], "From": "A permit_inspections"},
        {"Signal": "Q · Occupancy slide", "s = 0 → 1": f"(peak occ. last {qo['lookback_months']} mo − occ. now − market drop) ÷ {qo['full_gap']:.0%}", "q": qo["reliability"], "From": "operating_metrics + market_occupancy"},
        {"Signal": "V · Income erosion", "s = 0 → 1": f"NOI drop from its best year (last {iv['lookback_months']} mo) ÷ {iv['full_noi_drop']:.0%}", "q": iv["reliability"], "From": "operating_metrics (public disclosures; no DSCR)"},
        {"Signal": "X · Cost squeeze", "s = 0 → 1": f"(expense growth − revenue growth since best year) ÷ {xs['full_gap']:.0%}", "q": xs["reliability"], "From": "operating_metrics"},
        {"Signal": "K · Concession creep", "s = 0 → 1": f"(free months per lease-year now − 6+ months ago) ÷ {kc['full_per_year']}", "q": kc["reliability"], "From": "B rent_observations (free rent + lease term)"},
        {"Signal": "Lq · Licensing / care", "s = 0 → 1": "CMS 1-star 0.8 · 2-star 0.5 · abuse +0.2 · state distress 0.8", "q": lq["reliability"], "From": "A licensing_records"},
        {"Signal": "W · Value cut", "s = 0 → 1": f"(value at securitization − most recent value) ÷ value at securitization ÷ {c.get('value_cut', {}).get('full_drop', 0.5):.0%}", "q": c.get("value_cut", {}).get("reliability", 1.0), "From": "Public SEC CMBS loan filings (Schedule AL valuation fields)"},
    ]), hide_index=True, use_container_width=True)
    cor = {int(k): v for k, v in cfg["corroboration"].items()}   # JSON turns the keys into text
    st.markdown(f"""
- **No lone-signal discount:** a single signal counts in full (1 signal → {cor[1]:.0%}), because much of the data isn't public.
- **Strength boost:** a signal at ≥ {so.get('boost_starts_at', 0.6):.0%} strength counts more, rising to ×{so.get('max_boost', 1.5)} at {so.get('boost_full_at', 1.0):.0%} (for C, the strongest single creative signal decides). The boost and the rescale don't stack: the larger is used.
- **⭐ STANDOUT** tag: a signal at ≥ {so['at_strength']:.0%} strength.
- **↩ COUNTER-SIGNAL:** strong evidence the other way (rent beating its city by {cs['rent_beats_city_by']:.0%}+, or a city inspection in the last {cs['inspected_within_days']} days) multiplies early points by {cs['factor']} each.
- **Ready to act (max {late['max_points']}):** court/legal filing {late['court_points']}, contractor liens {late['lien_points']} each up to {late['max_lien_points']}. Older than {late['recent_days']} days count half.
- **Not scored:** Ten-X auctions and CMBS data are shown as context only.
- **Tiers (formula audit names):** hot ≥ {cfg['tiers']['hot']}, watch ≥ {cfg['tiers']['watch']} - displayed in the app as Priority Review / Monitor.

Edit every number in `pipeline/config/equation.yaml`, then rerun `python -m scorer.weekly`.
""")


def page_all(con, run_id):
    st.title("All properties")
    df = flags(con, run_id)
    if df.empty:
        st.info("No properties in this run.")
        return
    st.caption(
        "Every property scored in this run, including the ones with no distress signals yet. "
        "A blank score means the property has not been reviewed yet - missing data is not proof of "
        "absence. A reviewed score of 0 can still carry verified losses that other factors balanced "
        "out - open the property from the Flag details page for the full review. Reviewed "
        "senior-housing properties outside this run are on that page too.")
    # Displayed Score/Tier are the reviewed research assessments (provisional judgment,
    # rubric v2) where one exists; unresearched properties are blank/unscored, never 0/Low.
    # The deterministic formula's rows are untouched underneath (audit trail in the export).
    rassess = research_assessments(con)
    out = pd.DataFrame({
        "Rank": df["rank"],
        "Property": df["property_name"],
        "Type": df["property_type"],
        "City": df["city"],
        "State": df["state"],
        "Loan": df["loan_amount"],
        "Score": df["property_id"].map(lambda p: rassess.get(p, (pd.NA, ""))[0]),
        "Tier": df["property_id"].map(lambda p: TIER_LABEL.get((rassess.get(p, (pd.NA, ""))[1] or "unscored").split(" ")[0], "unscored")),
        "Listed": df["shown"].map({True: "yes", False: "no"}),
        "Why hidden": df["filter_reason"].fillna(""),
    }).sort_values(["Score", "Rank"], ascending=[False, True], na_position="last")   # research scores first; columns stay user-sortable
    c1, c2 = st.columns(2)
    _ALL_TIERS = ["Priority Review", "Monitor", "Limited Signals", "unscored"]
    _TIER_DISP = {"Priority Review": TIER_ICON["hot"], "Monitor": TIER_ICON["watch"],
                  "Limited Signals": TIER_ICON["low"]}
    tier_pick = c1.multiselect("Tiers", _ALL_TIERS,
                               default=_ALL_TIERS,
                               format_func=lambda t: _TIER_DISP.get(t, "⚪ Unscored (not researched yet)"),
                               key="all_tiers")
    listed_pick = c2.multiselect("Listed status", ["yes", "no"], default=["yes", "no"], key="all_listed")
    out = out[out["Tier"].isin(tier_pick) & out["Listed"].isin(listed_pick)]
    st.caption(f"{len(out)} of {len(df)} properties shown")
    st.dataframe(out, hide_index=True, use_container_width=True)


PAGES = {"Ranked flags": page_ranked, "All properties": page_all, "Flag details": page_detail,
         "What changed this week": page_changes, "Research log": page_log, "How the score works": page_how}


def main():
    maybe_ingest_console()
    con = get_con()
    all_runs = runs(con)
    with st.sidebar:
        # brand block is rendered by dashboard/style.py at inject() time
        st.session_state.setdefault("page", "Ranked flags")
        st.radio("Page", list(PAGES), key="page")
        run_id = st.selectbox("Week", all_runs, format_func=lambda x: x.replace("week-", "Week of ")) if all_runs else None
        st.caption(f"Database: {Path(db_path() or 'data/bloomfield.duckdb').name}")
        if "demo" in Path(db_path() or "").name:
            st.caption("Sample data only. The tool reads and flags; Bloomfield's team does all outreach.")
        else:
            st.caption("Live data from public SEC filings (CMBS loans). County sources are not turned on. "
                       "The tool reads and flags; Bloomfield's team does all outreach.")
    if not run_id:
        st.title("No weekly run yet")
        st.write("Run `python -m scorer.weekly` (real data) or `python -m scorer.demo_week` (made-up demo).")
        st.caption(research_status(con))
        with st.expander("Research diagnostics"):
            st.text(research_diagnostics(con))
        return
    PAGES[st.session_state.page](con, run_id)


main()
