> U-M Toolkit setup: [pipeline/UMICH-WORKFLOW.md](pipeline/UMICH-WORKFLOW.md).

# Bloomfield Sourcing Tool

A tool that finds commercial-property owners in financial trouble **before the broker network does**, for Bloomfield Capital.
Built by the University of Michigan Business+Tech AI Sprint team. A researcher at Bloomfield reviews every flag;
**the tool never contacts anyone.**

> **All data in this repository is invented.** Nothing real from Bloomfield is used, and nothing real may be committed
> (this repository is public).

## How it works

Every week the tool runs the same chain, and a person checks the output at each stage:

| Part | What it does | Where |
|---|---|---|
| **A. Collect** | Public records: county recorder, court and notice sites, SEC CMBS filings, bank call reports, building permits. The NETR Online directory is used to find each county's record sites, and every site is screened before it is used. | `pipeline/collectors/` |
| **B. Read** | An AI reader turns each document into the same fields. Every value must come with an exact quote, and the quote is checked against the document. | `pipeline/reader/`, `pipeline/llm/` |
| **Bot** | Searchers for each signal: project websites ("coming soon" past the date), news and notices, plus the county, bank and SEC collectors. | `pipeline/bot/` |
| **C. Link** | Joins records about the same property, loan, lender and company (parcel, then address, then fuzzy names). Unsure matches go to a person. | `pipeline/matcher/` |
| **D. Score** | Ranks properties with the Bloomfield Opportunity Score and shows them, with evidence and an executive summary, in a researcher dashboard. | `pipeline/scorer/`, `pipeline/dashboard/` |
| **Research layer** | The careful-analyst method: provenance-rich evidence, graded identity matching, primary financial records, counter-evidence, source reconciliation, and holds when sources conflict. Keeps partial research honest; never changes the score. | `pipeline/research/` |

## How we weight the signals

From Bloomfield's own feedback (details in `pipeline/scorer/EQUATION.md` and `pipeline/config/`):

1. **Construction-loan clock: strongest.** A project that opens with its construction loan still outstanding, or a loan from
   five years ago that was never paid off, is distress.
2. **Opening-date watch: good but weaker.** A 3 to 6 month delay is normal, so short slips score little or nothing.
3. **Court and lien filings: they come late.** They confirm a situation is actionable, so they carry a **Ready to act** label,
   not an early-warning score.
4. **Forced and public sales, and CMBS data: lowest weight.** Everyone has them, so they give little edge.
5. **Creative signals** (rent concessions, stalled leasing, and similar) are promising and have measurable formulas, but
   need a reliable way to detect them.

## Try it

You need Git and Python 3.11 or newer. Everything below runs on made-up data, with no internet and no AI key.

```bash
git clone --branch Current-Solution https://github.com/Ross-Business-Tech/Bloomfield-Project-2.git
cd Bloomfield-Project-2/pipeline
python -m venv .venv
source .venv/bin/activate          # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q                # all tests should pass
python -m demo --open              # the screenshare demo (sample data, works offline)
```

The researcher dashboard on sample data:

```bash
python -m scorer.demo_week
BLOOMFIELD_DB=data/demo.duckdb streamlit run dashboard/app.py
```

## The weekly run

```bash
python -m bot --list               # what each signal's searcher does
python -m bot --full               # the whole week: crawl, AI reads, link, score
python -m bot --full --no-ai       # the same without the AI (keyword reading only)
python -m bot                      # crawl only
streamlit run dashboard/app.py     # open the researcher dashboard
```

`python -m bot --full` is the one command for a real week. The bot's crawlers collect pages and records (project sites, news,
county, bank and SEC data). The **AI reader (OpenAI)** then reads each page or document into fields with an exact, checked
quote and spots the signals in it. The matcher links records about the same property, and the Opportunity Score equation
ranks them. The score is plain arithmetic on the evidence, so a researcher can always see why a property ranked where it did:
the AI reads and identifies, it never invents a score. Without a key it says so and still runs with keyword reading.
Sample (FAKE) properties are left out of every real list; the demo database opts in explicitly.

## Using the AI reader (needs a key)

Every AI step defaults to U-M GPT Toolkit through one wrapper (`pipeline/llm/client.py`), using its OpenAI-compatible chat API.

1. Copy `pipeline/.env.example` to `pipeline/.env` (the file is git-ignored and must never be committed).
2. Put your U-M Toolkit key on the line `OPENAI_API_KEY=...`. The defaults select `gpt-4o-mini` and `gpt-4o` at the U-M endpoint.
3. Check it: `python -m llm.check`.

Spending is capped in `pipeline/config/reader.yaml`: **$10 per week and $5 per run**. Anthropic and Gemini also work
(`LLM_PROVIDER` in `.env`). Never paste a key into chat, code or a commit.

## Finding county record websites

```bash
python -m collectors.discover --check      # can it reach and read NETR Online?
python -m collectors.discover --state MI --county "Washtenaw County" --screen --proposals
```

NETR Online has agreed to this use. Each county site it points to is still screened (robots.txt, login or CAPTCHA wall, the
site's terms) before it is added, and a person confirms the terms before a source goes automatic. The five sample counties
are Washtenaw MI, Maricopa AZ, Travis TX, Miami-Dade FL and Pinellas FL; every source stays on a manual inbox until checked.
No contact email setup is needed: requests identify the team with a default address (`pipeline/config/sources.yaml`).

## Rules the tool follows

- Reads `robots.txt`, waits between requests, and identifies itself on every request.
- **Never** solves a CAPTCHA, logs in, or gets around a block or paywall. A site that does not allow automated access
  becomes a manual drop-in folder.
- Never shows a fact it cannot point to in a document, and never contacts anyone.
- Bloomfield's own material is never sent to an outside AI service.
- No secrets or real data in the repository: `pipeline/scripts/check_no_data_staged.py` blocks them from commits.

## What is built, and what is not

| | Status |
|---|---|
| Construction-loan clock, contractor liens, UCC sales | **Coded** (tested on sample data) |
| Opening-date watch, foreclosure and receivership notices, bank and CMBS stress, news, permits | **In progress**: logic built and tested; needs real data, real sites and (for open-web search) a search-service key |
| Ten-X auctions, tracking each flag to a closed deal | **Not built yet** |

Not yet proven: the NETR tool and the web bot have only been run against fake pages, because the build environment's network
blocks real sites. Run `python -m collectors.discover --check` on your own computer first.
Final scope (every signal, or one done exceptionally well) is decided before the Nov 17 presentation.

## Where things are

- `pipeline/README.md`: detailed setup and per-part documentation.
- `pipeline/research/README.md`: the deep research method, gate by gate.
- `pipeline/scorer/EQUATION.md`: the Opportunity Score, term by term.
- `pipeline/config/`: every number and switch (`scoring.yaml`, `equation.yaml`, `reader.yaml`, `bot.yaml`, `discovery.yaml`, `counties/`).
- `pipeline/demo/`: the demo page, the mid-point slide deck, the call plan and the content they share (`content.json`).
- `src/`: the web app (React) with the browser-side opening-date and creative-signal logic.
- `CLAUDE.md`: how to work on this repository (publishing, branches, safety rules).

## Branches

Work comes together on `Current-Solution` and reaches `main` through pull requests. Never push to `main` directly.
