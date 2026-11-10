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

TERMS = [("l_loan_clock", "L", "Loan clock", "loan_clock"), ("o_opening", "O", "Opening delay", "opening_delay"),
         ("r_rent", "R", "Rent vs. city", "rent_decline"), ("i_inspection", "I", "Site inactivity", "inspection_gap")]
TIER_ICON = {"hot": "🔴 Hot", "watch": "🟠 Watch", "low": "⚪ Low"}


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


def esc(x):
    """Records come from outside documents: escape before showing."""
    return "" if x is None or (isinstance(x, float) and pd.isna(x)) else html.escape(str(x))


def open_flag(pid):
    st.session_state.selected = pid
    st.session_state.page = "Flag details"


def tags(r):
    out = []
    if "STANDOUT" in (r["flags"] or ""):
        out.append(":violet-badge[⭐ STANDOUT]")
    if "COUNTER-SIGNAL" in (r["flags"] or ""):
        out.append(":blue-badge[↩ COUNTER-SIGNAL]")
    out.append(":red-badge[Actionable]" if r["stage"] == "actionable" else ":green-badge[Early]")
    return " ".join(out)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def page_ranked(con, run_id):
    df, cfg = flags(con, run_id), run_cfg(con, run_id)
    f = cfg["filters"]
    status = latest_status(con)
    st.title("This week's flags")
    st.caption(f"Bloomfield Opportunity Score {cfg['version']} · {run_id.replace('week-', 'week of ')} · "
               f"{', '.join(f['property_types_excluded'])} hidden · loans {money(f['min_loan_size'])}–"
               f"{money(f['max_loan_size'])} · {'states ' + ', '.join(f['states']) if f['states'] else 'all U.S.'}")
    ranked = df[df["rank"].notna()]
    c = st.columns(5)
    c[0].metric("Ranked", len(ranked))
    c[1].metric("Hot (call this week)", int((ranked["tier"] == "hot").sum()))
    c[2].metric("Watch", int((ranked["tier"] == "watch").sum()))
    c[3].metric("Early (before any filing)", int((ranked["stage"] == "early").sum()))
    c[4].metric("Filtered out", int((~df["shown"]).sum()))

    left, right = st.columns([2, 1])
    tier_pick = left.multiselect("Tiers", ["hot", "watch", "low"], default=["hot", "watch", "low"],
                                 format_func=lambda t: TIER_ICON[t])
    show_hidden = right.toggle("Show filtered-out (office, size, location)")
    view = df if show_hidden else ranked
    view = view[view["tier"].isin(tier_pick)]
    for r in view.to_dict("records"):
        with st.container(border=True):
            a, b, c3 = st.columns([0.7, 5, 1.8])
            a.markdown(f"### {int(r['rank']) if pd.notna(r['rank']) else '—'}")
            place = ", ".join(x for x in [r["city"], r["state"]] if x and not pd.isna(x))
            b.markdown(f"**{esc(r['property_name'])}** — {esc(r['property_type'] or 'type unknown')} · "
                       f"{esc(place)} · {money(r['loan_amount'])}")
            b.markdown(tags(r))
            parts = [f"{code} {r[col]:.0%}" for col, code, _, _ in TERMS if r[col] > 0]
            b.caption(("Early signals: " + " · ".join(parts)) if parts else "No early signals (late filings only)")
            if not r["shown"]:
                b.markdown(f":gray[Hidden: {esc(r['filter_reason'])}]")
            if r["property_id"] in status:
                b.markdown(f":green[Research: {weekly.STATUS_LABELS[status[r['property_id']]]}]")
            c3.markdown(f"**{r['score']:.0f}** / 100 · {TIER_ICON[r['tier']]}")
            c3.progress(min(1.0, r["score"] / 100))
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
    sel = st.session_state.get("selected")
    pid = st.selectbox("Flag", ids, index=ids.index(sel) if sel in ids else 0, format_func=lambda k: names[k])
    st.session_state.selected = pid
    r = df[df["property_id"] == pid].iloc[0].to_dict()
    ex = json.loads(r["explanation"])
    cfg = run_cfg(con, run_id)
    w = cfg["weights"]

    st.title(r["property_name"])
    st.caption(f"{esc(r['property_type'] or 'type unknown')} · {esc(r['address'] or '')} · "
               f"borrower {esc(r['borrower']) or 'unknown'} · lender {esc(r['lender']) or 'unknown'}")
    st.markdown(tags(r))
    c = st.columns(4)
    c[0].metric("Opportunity score", f"{r['score']:.1f}")
    c[1].metric("Tier", TIER_ICON[r["tier"]])
    c[2].metric("Deal quality", r["deal_quality"])
    c[3].metric("Loan", money(r["loan_amount"]))
    if not r["shown"]:
        st.warning(f"Filtered out by settings: {r['filter_reason']}")
    st.info(r["summary"])

    st.subheader("The equation for this property")
    rows = []
    for col, code, label, key in TERMS:
        t = ex[{"L": "L_loan_clock", "O": "O_opening", "R": "R_rent", "I": "I_inspection"}[code]]
        rows.append({"Term": f"{code} · {label}", "Strength": t["strength"], "Weight": w[key],
                     "Points before anti-fluke": round(w[key] * t["strength"], 1), "Why": t["why"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                 column_config={"Strength": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
    a, b = st.columns(2)
    with a:
        st.markdown(f"**Anti-fluke:** {ex['anti_fluke']}")
        for s in ex.get("standouts", []):
            st.markdown(f"⭐ **Standout** (counts ×{cfg['standout']['boost']}, skips anti-fluke): {esc(s)}")
        for s in ex.get("counter_signals", []):
            st.markdown(f"↩ **Counter-signal** (×{cfg['counter_signal']['factor']}): {esc(s)}")
        st.markdown(f"**Early points:** {ex['early_points']}")
    with b:
        st.markdown(f"**Late points (already happened, max {cfg['late']['max_points']}):** {ex['late_points']}")
        for s in ex.get("late_why", []):
            st.markdown(f"- {esc(s)}")
        st.markdown(f"**Score = min(100, {ex['early_points']} + {ex['late_points']}) = {ex['score']}**")

    ctx = json.loads(r["context"])
    if ctx:
        st.subheader("Context (shown, not scored)")
        st.caption("Bloomfield: auctions and Trepp/CMBS data are public or everyone has them.")
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
    st.subheader("⚖️ New court filings or liens (already happened)")
    _list(ch["new_late"], "late", lambda r: f"late points {r['late_points']:.0f}")
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
    st.markdown("Built from Bloomfield's feedback: **early signals find the opportunity; court filings and "
                "liens only confirm it.**")
    st.latex(r"\text{Early}=\sum_{L,O,R,I} w_i\, s_i \times \begin{cases}%s & s_i\ge %s\ (\text{STANDOUT})\\ C(k) & \text{otherwise}\end{cases}"
             % (so["boost"], so["at_strength"]))
    st.latex(r"\text{Score}=\min\!\left(100,\ \text{Early}\times %s^{\,\#\text{counter-signals}}+\text{Late}\right)" % cs["factor"])
    lc, od, rd, ig = cfg["loan_clock"], cfg["opening_delay"], cfg["rent_decline"], cfg["inspection_gap"]
    st.dataframe(pd.DataFrame([
        {"Term": "L · Loan clock", "Weight": w["loan_clock"], "0 → 1": f"{lc['start_months']} → {lc['full_months']} months unpaid, +{lc['per_extension']} per extension", "From": "Part A recorded_loans"},
        {"Term": "O · Opening delay", "Weight": w["opening_delay"], "0 → 1": f"{od['start_months']} → {od['full_months']} months late (under {od['start_months']} is normal)", "From": "Part B projects"},
        {"Term": "R · Rent vs. city", "Weight": w["rent_decline"], "0 → 1": f"0 → {rd['full_gap']:.0%} worse than the city", "From": "Part B rent_observations + Part A market_rents"},
        {"Term": "I · Site inactivity", "Weight": w["inspection_gap"], "0 → 1": f"{ig['start_days']} → {ig['full_days']} days since a city inspection", "From": "Part A permit_inspections"},
    ]), hide_index=True, use_container_width=True)
    cor = {int(k): v for k, v in cfg["corroboration"].items()}   # JSON turns the keys into text
    st.markdown(f"""
- **Anti-fluke C(k):** k = early signals at ≥ {cfg['shows_at']:.0%}. 1 → {cor[1]:.0%}, 2 → {cor[2]:.0%}, 3+ → {cor[3]:.0%}.
- **⭐ STANDOUT:** a signal at ≥ {so['at_strength']:.0%} counts ×{so['boost']} and skips the anti-fluke discount.
- **↩ COUNTER-SIGNAL:** strong evidence the other way (rent beating its city by {cs['rent_beats_city_by']:.0%}+, or a city inspection in the last {cs['inspected_within_days']} days) multiplies early points by {cs['factor']} each.
- **Late (max {late['max_points']}):** court/legal filing {late['court_points']}, contractor liens {late['lien_points']} each up to {late['max_lien_points']}. Older than {late['recent_days']} days count half.
- **Not scored:** Ten-X auctions and Trepp/CMBS data are shown as context only.
- **Tiers:** Hot ≥ {cfg['tiers']['hot']}, Watch ≥ {cfg['tiers']['watch']}.

Edit every number in `pipeline/config/equation.yaml`, then rerun `python -m scorer.weekly`.
""")


PAGES = {"Ranked flags": page_ranked, "Flag details": page_detail, "What changed this week": page_changes,
         "Research log": page_log, "How the score works": page_how}


def main():
    con = get_con()
    all_runs = runs(con)
    with st.sidebar:
        st.markdown("## 🏚️ Bloomfield Flags")
        st.session_state.setdefault("page", "Ranked flags")
        st.radio("Page", list(PAGES), key="page")
        run_id = st.selectbox("Week", all_runs, format_func=lambda x: x.replace("week-", "Week of ")) if all_runs else None
        st.caption(f"Database: {Path(db_path() or 'data/bloomfield.duckdb').name}")
        st.caption("Sample data only. The tool reads and flags; Bloomfield's team does all outreach.")
    if not run_id:
        st.title("No weekly run yet")
        st.write("Run `python -m scorer.weekly` (real data) or `python -m scorer.demo_week` (made-up demo).")
        return
    PAGES[st.session_state.page](con, run_id)


main()
