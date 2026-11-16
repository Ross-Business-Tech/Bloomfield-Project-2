"""
style.py - visual theme for the Bloomfield Flags dashboard.

Pure presentation: typography, color, spacing, card and sidebar styling.
No scoring, data, or content logic lives here. app.py calls inject() once,
right after st.set_page_config(...). Palette mirrors .streamlit/config.toml;
the CSS here covers what the config cannot (fonts, sidebar, cards, metrics).

v4 design direction: an institutional research product, not a demo.
Typography carries the hierarchy; color is reserved for meaning (tier,
severity). No emoji anywhere in the interface - tier and status are set as
small uppercase engraved chips with restrained tints.
"""
from __future__ import annotations

import streamlit as st

# Palette (single source of truth for the CSS below and config.toml):
INK = "#152A42"        # deep navy - headings, primary buttons
INK_SOFT = "#3E5268"   # secondary text
PAPER = "#FAF8F4"      # warm paper background
CARD = "#FFFFFF"
LINE = "#E4DFD3"       # hairline borders
GOLD = "#A88435"       # muted brass accent
GOLD_DEEP = "#8C6C28"
SIDEBAR = "#10233B"    # deep navy sidebar
SIDEBAR_EDGE = "#0B1A2D"
SIDEBAR_TEXT = "#D8DEE8"
SIDEBAR_MUTE = "#A9BAD0"
# Tier tints - desaturated, meaning-bearing only:
TIER_HOT = "#9C2F28"
TIER_WATCH = "#8A6A1C"
TIER_LOW = "#54667A"
MUTE = "#6E7681"
RANK = "#B9AE97"       # muted brass-gray for ordinal numerals

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Source+Serif+4:opsz,wght@8..60,500;8..60,600;8..60,700&display=swap');

/* ---------- global type & background ---------- */
html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"] {
  background-color: %(PAPER)s;
  font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif;
}
[data-testid="stMain"] p, [data-testid="stMain"] li, [data-testid="stMain"] span,
[data-testid="stMain"] label, .stMarkdown, [data-testid="stCaptionContainer"] {
  font-family: 'Inter', sans-serif;
}
h1, h2, h3, [data-testid="stHeading"] {
  font-family: 'Source Serif 4', Georgia, serif !important;
  color: %(INK)s; letter-spacing: -0.01em;
}
h1 { font-weight: 650 !important; font-size: 2.15rem !important; }
[data-testid="stMain"] h1::after {
  content: ""; display: block; width: 52px; height: 3px;
  background: %(GOLD)s; border-radius: 2px; margin-top: 0.4rem;
}
h2, h3 { font-weight: 600 !important; }
code, .stCode, kbd { font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace; }
[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' !important; }

/* hairline rules instead of heavy default dividers */
hr { border-color: %(LINE)s !important; }

/* ---------- header chrome: a shipped product, not a Streamlit app ---------- */
header[data-testid="stHeader"] {
  background: rgba(250, 248, 244, 0.85) !important;
  backdrop-filter: blur(6px);
}
[data-testid="stDecoration"] { display: none; }
.stAppDeployButton, [data-testid="stAppDeployButton"] { display: none !important; }
[data-testid="stToolbar"] { opacity: 0.55; }
[data-testid="stToolbar"]:hover { opacity: 1; }

/* ---------- sidebar: deep navy, brand-forward ---------- */
[data-testid="stSidebar"] {
  background: linear-gradient(178deg, %(SIDEBAR)s 0%%, %(SIDEBAR_EDGE)s 100%%) !important;
  border-right: 1px solid #1E3450;
}
[data-testid="stSidebar"] * { color: %(SIDEBAR_TEXT)s; }
[data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3,
[data-testid="stSidebar"] h2 *, [data-testid="stSidebar"] h3 * {
  color: #FFFFFF !important;
  font-family: 'Source Serif 4', Georgia, serif !important;
  letter-spacing: 0.01em;
}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] *,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {
  color: #B4C2D4 !important;
  font-size: 0.78rem !important;
  line-height: 1.45 !important;
}
[data-testid="stSidebar"] hr { border-color: #26405E !important; }

/* widget labels in the sidebar read as micro-captions */
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {
  text-transform: uppercase; letter-spacing: 0.14em;
  font-size: 0.64rem !important; font-weight: 600; color: %(SIDEBAR_MUTE)s !important;
}

/* nav radio -> product navigation list (circles hidden, brass active rail).
   Streamlit >= 1.65 renders the radio as react-aria labels with data-selected. */
[data-testid="stSidebar"] [data-testid="stRadioOption"] > div > div:first-child {
  display: none !important;
}
[data-testid="stSidebar"] [data-testid="stRadioOption"] {
  border-left: 2px solid transparent;
  border-radius: 4px;
  padding: 6px 10px !important;
  margin: 1px 0;
  transition: background-color 140ms ease;
}
[data-testid="stSidebar"] [data-testid="stRadioOption"]:hover { background: rgba(255,255,255,0.06); }
[data-testid="stSidebar"] [data-testid="stRadioOption"] p {
  font-size: 0.92rem; font-weight: 500; color: %(SIDEBAR_TEXT)s;
}
[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] {
  background: rgba(255,255,255,0.07);
  border-left: 2px solid %(GOLD)s;
}
[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] p {
  color: #FFFFFF;
}

/* sidebar selectbox & inputs stay legible on navy (react-aria combobox, streamlit >= 1.65) */
[data-testid="stSidebar"] [data-testid="stSelectbox"] .react-aria-ComboBox,
[data-testid="stSidebar"] [data-testid="stSelectbox"] .react-aria-ComboBox > div,
[data-testid="stSidebar"] [data-baseweb="select"] > div {
  background-color: #1B3454 !important;
  border-color: #2C4A6E !important;
  border-radius: 8px !important;
}
[data-testid="stSidebar"] [data-testid="stSelectbox"] input,
[data-testid="stSidebar"] [data-testid="stSelectbox"] span,
[data-testid="stSidebar"] input {
  background-color: transparent !important;
  color: #FFFFFF !important;
  caret-color: #FFFFFF;
}
[data-testid="stSidebar"] [data-testid="stSelectbox"] svg { fill: %(SIDEBAR_MUTE)s !important; }

/* ---------- metrics: editorial stat treatment ---------- */
[data-testid="stMetric"] {
  background: %(CARD)s;
  border: 1px solid %(LINE)s;
  border-radius: 10px;
  padding: 0.85rem 1rem 0.7rem;
  box-shadow: 0 1px 2px rgba(21, 42, 66, 0.04);
}
[data-testid="stMetricLabel"] p {
  text-transform: uppercase; letter-spacing: 0.07em;
  font-size: 0.68rem !important; font-weight: 600; color: %(INK_SOFT)s !important;
}
[data-testid="stMetricValue"] {
  font-family: 'Source Serif 4', Georgia, serif !important;
  font-weight: 600; color: %(INK)s;
  font-variant-numeric: tabular-nums;
}

/* ---------- flag cards ---------- */
[data-testid="stVerticalBlockBorderWrapper"]:has([data-testid="stVerticalBlock"] [data-testid="stButton"]) {
  background: %(CARD)s;
  border: 1px solid %(LINE)s !important;
  border-radius: 12px !important;
  box-shadow: 0 1px 3px rgba(21, 42, 66, 0.05);
  transition: box-shadow 160ms ease, border-color 160ms ease;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover {
  border-color: #CFC7B4 !important;
  box-shadow: 0 4px 14px rgba(21, 42, 66, 0.09);
}

/* rank ordinal: quiet serif numeral, not a heading */
.bf-rank {
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 1.45rem; font-weight: 600; color: %(RANK)s;
  line-height: 1; padding-top: 3px;
  font-variant-numeric: tabular-nums;
}

/* score readout: large serif figure with a small denominator */
.bf-score { display: flex; align-items: baseline; gap: 5px; margin-bottom: 2px; }
.bf-score-num {
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 1.55rem; font-weight: 650; color: %(INK)s;
  font-variant-numeric: tabular-nums; line-height: 1.05;
}
.bf-score-den { font-size: 0.72rem; color: %(INK_SOFT)s; }
.bf-score-na { color: %(RANK)s; }

/* ---------- engraved signal chips (replace emoji pills) ---------- */
.bf-chip {
  display: inline-block;
  font-family: 'Inter', sans-serif;
  font-size: 0.64rem; font-weight: 600;
  letter-spacing: 0.09em; text-transform: uppercase;
  padding: 2px 7px 1px;
  border-radius: 3px;
  border: 1px solid transparent;
  margin: 0 6px 2px 0;
  white-space: nowrap;
}
.bf-chip-hot      { color: %(TIER_HOT)s;   background: rgba(156, 47, 40, 0.07);  border-color: rgba(156, 47, 40, 0.35); }
.bf-chip-watch    { color: %(TIER_WATCH)s; background: rgba(168, 132, 53, 0.10); border-color: rgba(168, 132, 53, 0.40); }
.bf-chip-low      { color: %(TIER_LOW)s;   background: rgba(84, 102, 122, 0.08); border-color: rgba(84, 102, 122, 0.30); }
.bf-chip-unscored { color: %(MUTE)s;       background: transparent;              border-color: #C9CDD3; border-style: dashed; }
.bf-chip-standout { color: %(GOLD_DEEP)s;  background: rgba(168, 132, 53, 0.12); border-color: rgba(168, 132, 53, 0.45); }
.bf-chip-counter  { color: #2F5D8A;        background: rgba(47, 93, 138, 0.08);  border-color: rgba(47, 93, 138, 0.30); }

/* ---------- buttons ---------- */
.stButton > button, [data-testid="stFormSubmitButton"] > button,
[data-testid="stDownloadButton"] > button {
  background-color: %(INK)s !important;
  color: #FFFFFF !important;
  border: 1px solid %(INK)s !important;
  border-radius: 8px !important;
  font-weight: 500;
}
.stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover,
[data-testid="stDownloadButton"] > button:hover {
  background-color: %(GOLD_DEEP)s !important;
  border-color: %(GOLD_DEEP)s !important;
  color: #FFFFFF !important;
}
.stButton > button:active, .stButton > button:focus:not(:active) {
  border-color: %(GOLD)s !important; color: #FFFFFF !important;
}

/* ---------- progress bars: thin brass fill ---------- */
[data-testid="stProgress"] > div > div { height: 4px !important; }
[data-testid="stProgress"] > div > div > div { background-color: %(GOLD)s !important; }

/* ---------- expanders & info boxes: softer ---------- */
[data-testid="stExpander"] {
  background: %(CARD)s;
  border: 1px solid %(LINE)s !important;
  border-radius: 10px !important;
}
[data-testid="stAlert"] { border-radius: 10px; }

/* ---------- multiselect chips: neutral stone, never alarm colors ---------- */
[data-testid="stMultiSelect"] [data-testid="stMultiSelectTagsContainer"],
[data-testid="stMultiSelect"] .react-aria-ComboBox {
  background-color: %(CARD)s !important;
  border-color: %(LINE)s !important;
  border-radius: 8px !important;
}
[data-testid="stMultiSelect"] span[data-tag] {
  background-color: #F0EDE5 !important;
  border: 1px solid #DED8C9 !important;
  border-radius: 4px !important;
}
[data-testid="stMultiSelect"] span[data-tag] span { color: %(INK)s !important; font-weight: 500; }
[data-testid="stMultiSelect"] span[data-tag] button { color: %(MUTE)s !important; }
[data-testid="stMultiSelect"] input { caret-color: %(INK)s; }

/* ---------- dataframes ---------- */
[data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; }

/* ---------- columns fill their row: equal-height metric/stat cards ---------- */
[data-testid="stColumn"] { display: flex; flex-direction: column; }
[data-testid="stColumn"] > [data-testid="stVerticalBlock"] { flex: 1; }
[data-testid="stColumn"] [data-testid="stElementContainer"]:has([data-testid="stMetric"]) {
  flex: 1; display: flex; flex-direction: column;
}
[data-testid="stMetric"] { flex: 1; }

/* ---------- narrow viewports: metric rows wrap instead of crushing ---------- */
@media (max-width: 900px) {
  [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [data-testid="stMetric"]) {
    flex-wrap: wrap;
  }
  [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [data-testid="stMetric"]) > [data-testid="stColumn"] {
    flex: 1 1 28%% !important;
    min-width: 150px !important;
  }
  [data-testid="stMetricValue"] { font-size: 1.6rem !important; }
}

/* main column breathing room */
[data-testid="stMain"] .block-container {
  padding-top: 3.2rem;
  max-width: 1180px;
}

/* ---------- metric labels wrap instead of ellipsizing ---------- */
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] p,
[data-testid="stMetricLabel"] [class*="stMarkdownEllipsis"],
[data-testid="stMetricLabel"] [data-testid="stMarkdownContainer"] {
  white-space: normal !important;
  overflow: visible !important;
  text-overflow: clip !important;
  overflow-wrap: normal !important;  /* wrap at spaces, never mid-word */
  word-break: normal !important;
  hyphens: none !important;
  line-height: 1.35 !important;
  min-height: 2.45rem;  /* reserve two full label lines (measured 38px) so metric value baselines align */
}
[data-testid="stMetric"] { min-width: 0; }

/* ---------- tables - editorial grid ---------- */
[data-testid="stTable"] th, .stTable th {
  text-transform: uppercase; letter-spacing: 0.06em; font-size: 0.68rem;
  font-weight: 600; color: %(INK_SOFT)s !important;
  border-bottom: 2px solid %(LINE)s !important;
}
[data-testid="stTable"] td, .stTable td { font-variant-numeric: tabular-nums; }
[data-testid="stTable"] tbody tr:hover td { background: #F4F0E8; }

/* ---------- fluid feel - soft entrance, smoother controls ---------- */
[data-testid="stMain"] .block-container { animation: bfFade 240ms ease-out; }
@keyframes bfFade { from { opacity: 0; } to { opacity: 1; } }
.stButton > button, [data-testid="stFormSubmitButton"] > button,
[data-testid="stDownloadButton"] > button { transition: background-color 160ms ease, border-color 160ms ease; }
button:focus-visible, input:focus-visible, [role="combobox"]:focus-visible {
  outline: 2px solid %(GOLD)s !important; outline-offset: 1px;
}

/* ---------- metric values wrap instead of ellipsizing (long tier words) ---------- */
[data-testid="stMetricValue"] [class*="stMarkdownEllipsis"],
[data-testid="stMetricValue"] [data-testid="stMarkdownContainer"] {
  white-space: normal !important;
  overflow: visible !important;
  text-overflow: clip !important;
}
[data-testid="stMetricValue"] [class*="stMarkdownEllipsis"] p,
[data-testid="stMetricValue"] [data-testid="stMarkdownContainer"] p {
  white-space: normal !important;
  overflow-wrap: normal !important;  /* never split a word like "Unscored" mid-word */
  word-break: normal !important;
  hyphens: none !important;
}

/* ---------- tier word card: fit the longest word to the card, other cards untouched ---------- */
[data-testid="stElementContainer"]:has(.bf-tier-marker) { display: none; }
[data-testid="stMetric"] { container-type: inline-size; }
/* numeric values: unchanged at full width (36px), scale down only when the card is too narrow (e.g. "$15.0M" spilling at ~1024px) */
[data-testid="stMetricValue"] { font-size: min(2.25rem, 26cqw) !important; }
[data-testid="stColumn"]:has(.bf-tier-marker) [data-testid="stMetricValue"] {
  font-size: min(2.25rem, 21cqw) !important;  /* "Unscored" is ~4.7em wide; 21cqw keeps it inside the card */
}
@media (max-width: 900px) {
  [data-testid="stMetricValue"] { font-size: min(1.6rem, 26cqw) !important; }
  [data-testid="stColumn"]:has(.bf-tier-marker) [data-testid="stMetricValue"] { font-size: min(1.6rem, 21cqw) !important; }
}
</style>
""" % {"INK": INK, "INK_SOFT": INK_SOFT, "PAPER": PAPER, "CARD": CARD, "LINE": LINE,
       "GOLD": GOLD, "GOLD_DEEP": GOLD_DEEP, "SIDEBAR": SIDEBAR,
       "SIDEBAR_EDGE": SIDEBAR_EDGE, "SIDEBAR_TEXT": SIDEBAR_TEXT,
       "SIDEBAR_MUTE": SIDEBAR_MUTE, "TIER_HOT": TIER_HOT, "TIER_WATCH": TIER_WATCH,
       "TIER_LOW": TIER_LOW, "MUTE": MUTE, "RANK": RANK}

BRAND = """
<div style="padding: 0.35rem 0 1.1rem 0; border-bottom: 1px solid #26405E; margin-bottom: 1.1rem;">
  <div style="font-family: 'Inter', sans-serif; font-size: 0.62rem; font-weight: 600;
              letter-spacing: 0.22em; text-transform: uppercase; color: %(GOLD)s; margin-bottom: 0.3rem;">
    Bloomfield Capital
  </div>
  <div style="font-family: 'Source Serif 4', Georgia, serif; font-size: 1.42rem; font-weight: 650;
              color: #FFFFFF; line-height: 1.15;">
    Opportunity Flags
  </div>
  <div style="font-family: 'Inter', sans-serif; font-size: 0.72rem; color: %(SIDEBAR_MUTE)s; margin-top: 0.3rem;">
    Weekly distressed-debt research desk
  </div>
</div>
""" % {"GOLD": GOLD, "SIDEBAR_MUTE": SIDEBAR_MUTE}


def inject() -> None:
    """Apply the theme. Call once, immediately after st.set_page_config(...)."""
    st.markdown(CSS, unsafe_allow_html=True)
    with st.sidebar:
        st.markdown(BRAND, unsafe_allow_html=True)
