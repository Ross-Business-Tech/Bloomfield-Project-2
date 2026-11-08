"""
app.py - the researcher's dashboard (Part D).

    cd pipeline
    streamlit run dashboard/app.py
    # demo data:  python -m scorer.demo_data  then
    BLOOMFIELD_DB=data/demo.duckdb streamlit run dashboard/app.py

Pages: Ranked flags · Flag details · What changed this week · Research log
Reads the flags written by `python -m scorer.rank`; writes only research_log.
"""
from __future__ import annotations

import html
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # so `scorer` imports work from any folder

from scorer import rank  # noqa: E402
from scorer.store import RESEARCH_STATUSES, STATUS_LABELS, connect, default_db_path  # noqa: E402

st.set_page_config(page_title="Bloomfield Flags", page_icon="🏚️", layout="wide")

PUSHER_LABEL = {"court_or_tax": "Court / tax", "lender": "Lender", "contractor": "Contractor", "market": "Market", None: "Context"}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def get_con():
    if "con" not in st.session_state:
        st.session_state.con = connect(default_db_path())
    return st.session_state.con


def runs(con) -> list[str]:
    return [r[0] for r in con.execute("SELECT run_id FROM score_runs ORDER BY run_date DESC").fetchall()]


def flags(con, run_id: str) -> pd.DataFrame:
    return con.execute("SELECT * FROM flags WHERE run_id = ? ORDER BY rank NULLS LAST, desperation DESC", [run_id]).df()


def log_df(con, key: str | None = None) -> pd.DataFrame:
    if key:
        return con.execute("SELECT * FROM research_log WHERE property_key = ? ORDER BY logged_at DESC", [key]).df()
    return con.execute("SELECT * FROM research_log ORDER BY logged_at DESC").df()


def latest_status(con) -> dict:
    rows = con.execute("""
        SELECT property_key, status FROM (
          SELECT property_key, status, row_number() OVER (PARTITION BY property_key ORDER BY logged_at DESC) AS n
          FROM research_log) WHERE n = 1""").fetchall()
    return dict(rows)


def money(x) -> str:
    if x is None or pd.isna(x):
        return "size unknown"
    return f"${x / 1e6:.1f}M" if x >= 1e6 else f"${x:,.0f}"


def esc(x) -> str:
    """Escape text that came from documents before showing it."""
    return html.escape(str(x)) if x is not None and not (isinstance(x, float) and pd.isna(x)) else ""


def open_flag(key: str):
    st.session_state.selected = key
    st.session_state.page = "Flag details"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def page_ranked(con, run_id):
    df = flags(con, run_id)
    status = latest_status(con)
    settings = json.loads(con.execute("SELECT settings_json FROM score_runs WHERE run_id = ?", [run_id]).fetchone()[0])
    f = settings["filters"]

    st.title("This week's flags")
    st.caption(
        f"Scored {run_id.replace('run-', '')}. Filters from settings.yaml: "
        f"{', '.join(f['property_types_excluded']) or 'no'} hidden · loans {money(f['min_loan_size'])}–{money(f['max_loan_size'])} · "
        f"{'states ' + ', '.join(f['states']) if f['states'] else 'all U.S.'}"
    )
    shown = df[df["shown"]]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Flags shown", len(shown))
    c2.metric("Desperation 70+", int((shown["desperation"] >= 70).sum()))
    c3.metric("Hard date within 30 days",
              int((pd.to_datetime(shown["next_deadline"]) - pd.Timestamp(run_id.replace("run-", ""))).dt.days.le(30).sum()))
    c4.metric("Filtered out", int((~df["shown"]).sum()))

    show_hidden = st.toggle("Show filtered-out flags (office, size, location)", value=False)
    view = df if show_hidden else shown
    for r in view.to_dict("records"):
        with st.container(border=True):
            left, mid, right = st.columns([0.9, 5, 1.6])
            left.markdown(f"### {int(r['rank']) if pd.notna(r['rank']) else '—'}")
            signals = json.loads(r["signals_json"])
            labels = " · ".join(dict.fromkeys(s["label"] for s in signals))
            place = ", ".join(x for x in [r["city"], r["state"]] if x and not pd.isna(x))
            mid.markdown(f"**{esc(r['property_name'])}** — {esc(r['property_type'] or 'Type unknown')} · {esc(place)} · {money(r['loan_amount'])}")
            mid.caption(labels)
            if pd.notna(r["next_deadline"]):
                mid.markdown(f":red[**{esc(r['next_deadline_what'])}: {pd.Timestamp(r['next_deadline']):%b %d, %Y}**]")
            if not r["shown"]:
                mid.markdown(f":gray[Hidden: {esc(r['filter_reason'])}]")
            if r["property_key"] in status:
                mid.markdown(f":green[Research: {STATUS_LABELS.get(status[r['property_key']], status[r['property_key']])}]")
            right.markdown(f"Desperation **{r['desperation']}**")
            right.progress(int(r["desperation"]) / 100)
            right.markdown(f"Deal quality **{r['deal_quality']}**")
            right.progress(int(r["deal_quality"]) / 100)
            right.button("Open", key=f"open-{r['property_key']}", on_click=open_flag, args=(r["property_key"],),
                         use_container_width=True)


def page_detail(con, run_id):
    df = flags(con, run_id)
    if df.empty:
        st.info("No flags in this run.")
        return
    options = df["property_key"].tolist()
    names = dict(zip(df["property_key"], df["property_name"]))
    sel = st.session_state.get("selected")
    idx = options.index(sel) if sel in options else 0
    key = st.selectbox("Flag", options, index=idx, format_func=lambda k: names[k])
    st.session_state.selected = key
    r = df[df["property_key"] == key].iloc[0].to_dict()
    signals = json.loads(r["signals_json"])
    parts = json.loads(r["score_parts_json"])
    angles = json.loads(r["angles_json"])

    st.title(r["property_name"])
    place = ", ".join(x for x in [r["address"], r["city"], r["state"]] if x and not pd.isna(x))
    st.caption(f"{esc(r['property_type'] or 'Type unknown')} · {esc(place)}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Desperation", r["desperation"])
    c2.metric("Deal quality", r["deal_quality"])
    c3.metric("Loan", money(r["loan_amount"]))
    c4.metric("Next hard date", f"{pd.Timestamp(r['next_deadline']):%b %d}" if pd.notna(r["next_deadline"]) else "None")
    if not r["shown"]:
        st.warning(f"Filtered out by settings: {r['filter_reason']}")
    st.info(r["summary"])

    st.subheader("Evidence")
    for s in signals:
        with st.container(border=True):
            st.markdown(f"**{esc(s['label'])}** · :gray[{PUSHER_LABEL.get(s['pusher'], s['pusher'])}]")
            st.write(s["detail"])
            if s.get("quote"):
                st.markdown(f"> {esc(s['quote'])}")
            st.caption(f"Source: {s['source']}" + (f" · {s['event_date']}" if s.get("event_date") else ""))

    left, right = st.columns(2)
    with left:
        st.subheader("Why this desperation score")
        st.dataframe(pd.DataFrame(parts["desperation"]), hide_index=True, use_container_width=True)
    with right:
        st.subheader("Why this deal quality score")
        st.dataframe(pd.DataFrame(parts["deal_quality"]), hide_index=True, use_container_width=True)

    st.subheader("Deal angle for Bloomfield")
    for a in angles or ["No clear angle yet: keep watching."]:
        st.markdown(f"- {a}")
    st.markdown(f"**Sponsor:** {esc(r['sponsor']) or 'unknown'} · **Lender:** {esc(r['lender']) or 'unknown'}")
    st.caption("Principals and registered agent will appear here once Part C's contacts matcher runs.")

    st.subheader("Research log")
    with st.form(f"log-{key}", clear_on_submit=True):
        a, b = st.columns([2, 1])
        status = a.selectbox("What happened", RESEARCH_STATUSES, format_func=lambda s: STATUS_LABELS[s])
        who = b.text_input("Researcher", value=st.session_state.get("researcher", ""))
        note = st.text_area("Note", placeholder="e.g. Called the principal; open to a bridge loan.")
        amount = st.number_input("Deal amount ($, only for Deal closed)", min_value=0, step=100_000)
        if st.form_submit_button("Save to research log"):
            st.session_state.researcher = who
            con.execute(
                "INSERT INTO research_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [uuid.uuid4().hex, key, r["property_name"], status, note,
                 float(amount) if status == "deal_closed" and amount else None, who or None,
                 datetime.now(timezone.utc).replace(tzinfo=None)],
            )
            st.success("Saved.")
    hist = log_df(con, key)
    if hist.empty:
        st.caption("Nothing logged for this flag yet.")
    for h in hist.to_dict("records"):
        amt = f" · {money(h['deal_amount'])}" if pd.notna(h["deal_amount"]) else ""
        st.markdown(f"- **{STATUS_LABELS.get(h['status'], h['status'])}**{amt} — {esc(h['note'])} "
                    f":gray[({pd.Timestamp(h['logged_at']):%b %d, %Y}{', ' + esc(h['researcher']) if h['researcher'] else ''})]")


def _flag_list(rows, prefix, extra=None):
    if not rows:
        st.caption("None.")
        return
    for r in rows:
        c1, c2 = st.columns([5, 1])
        line = f"**{esc(r['property_name'])}** — desperation {r['desperation']}"
        if extra:
            line += f" · {extra(r)}"
        c1.markdown(line)
        c1.caption(esc(r.get("summary", "")))
        c2.button("Open", key=f"{prefix}-{r['property_key']}", on_click=open_flag, args=(r["property_key"],))


def page_changes(con, run_id):
    st.title("What changed this week")
    ch = rank.changes(con, run_id)
    if not ch["previous"]:
        st.info("This is the first scoring run, so there is nothing to compare yet. Run the scorer again next week.")
        return
    st.caption(f"Comparing {run_id.replace('run-', '')} with {ch['previous'].replace('run-', '')}.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("New flags", len(ch["new"]))
    c2.metric("Got worse", len(ch["worse"]))
    c3.metric("Got better", len(ch["better"]))
    c4.metric("Dropped off", len(ch["dropped"]))

    st.subheader("🆕 New flags")
    _flag_list(ch["new"], "new")
    st.subheader("🔺 Got worse")
    _flag_list(ch["worse"], "worse", lambda r: f"up {r['delta']} from {r['previous_desperation']}")
    st.subheader("➕ New warning signs on flags we already had")
    _flag_list(ch["new_signals"], "sig", lambda r: "new: " + ", ".join(esc(a["label"]) for a in r["added"]))
    st.subheader("🔻 Got better")
    _flag_list(ch["better"], "better", lambda r: f"down {-r['delta']} from {r['previous_desperation']}")
    st.subheader("✔️ Dropped off")
    if not ch["dropped"]:
        st.caption("None.")
    for r in ch["dropped"]:
        st.markdown(f"- **{esc(r['property_name'])}** — no longer has any warning signs this week "
                    "(for example, the city issued a certificate of occupancy).")


def page_log(con, run_id):
    st.title("Research log")
    settings = rank.load_settings()
    goal, last = settings["goal"]["annual_deal_volume"], settings["goal"]["last_year_volume"]
    log = log_df(con)
    closed = log[log["status"] == "deal_closed"]
    closed_total = float(closed["deal_amount"].fillna(0).sum())
    status = latest_status(con)

    st.subheader("Progress toward the $300M goal")
    c1, c2, c3 = st.columns(3)
    c1.metric("Closed from flags", money(closed_total))
    c2.metric("Growth needed this year", money(goal - last))
    c3.metric("Share of growth from flags", f"{closed_total / (goal - last):.0%}" if goal > last else "—")
    st.progress(min(1.0, closed_total / (goal - last)) if goal > last else 0.0)

    st.subheader("Funnel")
    counts = pd.Series(list(status.values())).value_counts() if status else pd.Series(dtype=int)
    funnel = pd.DataFrame({"Stage": [STATUS_LABELS[s] for s in RESEARCH_STATUSES],
                           "Flags": [int(counts.get(s, 0)) for s in RESEARCH_STATUSES]})
    st.bar_chart(funnel, x="Stage", y="Flags", horizontal=True, sort=False)
    st.caption("Each flag counts once, at its most recent stage.")

    st.subheader("Every entry")
    if log.empty:
        st.caption("Nothing logged yet. Open a flag and use the research log form.")
        return
    view = log.assign(
        When=pd.to_datetime(log["logged_at"]).dt.strftime("%b %d, %Y"),
        Stage=log["status"].map(STATUS_LABELS),
        Amount=log["deal_amount"].map(lambda x: money(x) if pd.notna(x) else ""),
    )[["When", "property_name", "Stage", "Amount", "note", "researcher"]].rename(
        columns={"property_name": "Flag", "note": "Note", "researcher": "Researcher"})
    st.dataframe(view, hide_index=True, use_container_width=True)
    st.download_button("Download as CSV", view.to_csv(index=False), "research_log.csv", "text/csv")


PAGES = {
    "Ranked flags": page_ranked,
    "Flag details": page_detail,
    "What changed this week": page_changes,
    "Research log": page_log,
}


def main():
    con = get_con()
    all_runs = runs(con)
    with st.sidebar:
        st.markdown("## 🏚️ Bloomfield Flags")
        if "page" not in st.session_state:
            st.session_state.page = "Ranked flags"
        st.radio("Page", list(PAGES), key="page")
        if not all_runs:
            st.warning("No scoring runs yet.")
        run_id = st.selectbox("Week", all_runs, format_func=lambda x: x.replace("run-", "Week of ")) if all_runs else None
        st.caption(f"Database: {Path(default_db_path()).name}")
        st.caption("Sample data only. Outreach is done by Bloomfield's team; this tool only reads and flags.")
    if not run_id:
        st.title("No flags yet")
        st.write("Run the weekly scorer first: `python -m scorer.rank` (or `python -m scorer.demo_data` for made-up demo data).")
        return
    PAGES[st.session_state.page](con, run_id)


main()
