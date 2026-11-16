# The research layer

The careful-analyst method for public-record distress research, as code. It sits between
collecting public records and trusting what they say, and it never touches the Opportunity
Score equation - it records **how much verified evidence** sits under a flag.

## The method

1. **Every fact carries its provenance.** Source URL, dataset and vintage, the identity key
   used to attach it, the exact period it covers, its units (and whether the units are
   confirmed), the source's own row id, the limit of what the source can tell, and the
   caveat that must travel with it (`research_evidence`, `evidence.py`).
2. **Identity is graded, and keys resolve to real properties.** CMS records attach by CCN;
   county records by parcel in the county's own spelling. A name/city-only match is stored
   as `name_only_unverified` and can never verify identity on its own (`identity.py`).
   `properties.py` then links those identity keys to the matcher's universe `property_id`
   (parcel, address, or CCN via the licensing file's facility address - a CCN alone never
   maps straight onto a property, and an ambiguous match resolves to nothing, never a
   guess). Linked rows group under the same `property_id` the rest of the app uses, so a
   facility's CMS rows and its county tax rows form ONE property's evidence.
3. **The guardrails are text, not vibes.** Fines are assessed dollars, never "unpaid debt".
   Denial days are suspended payment for new admissions, never lost revenue. Utilization is
   never staffed occupancy. An ownership "since" date is never a transfer. A shared name is
   never common ownership. A partial-year period is never annualized. A negative
   balance-sheet component is preserved as reported, with no inference of insolvency.
   Missing data is **unknown**, and unknown is never a healthy sign.
4. **Counter-evidence is part of the job, in every needed lane.** The county tax check
   AND the lien search must both be run and *recorded* (finding nothing still counts; not
   running a lane does not). One paid-tax row alone is not the counter-evidence method.
   Taxes paid current are counter-evidence. Historical tax-sale/redemption events are
   never assigned to the current owner without temporal grounding (`county_tax.py`).
5. **Sources get reconciled.** Provider Information totals are checked against the summed
   Penalties rows, per facility. A disagreement opens a conflict (`cms_pool.py`).
6. **Unresolved conflicts hold the property.** The gates (`gates.py`) mark a property
   `held` until a person resolves the disagreement. Nothing is averaged over or scored past.
7. **Depth is honest, and the financial record must hang on the verified identity.**
   `lead` → `partial` → `ready_for_review` is derived from what is actually in the
   tables, and the report names the unmet lanes - including exactly which counter-evidence
   lanes are missing. A dated financial row attached by a name-only or unmatched key does
   not count as the property's primary financial source. A source match is not completed
   research. `python -m research report` prints the counts. The terminal state is
   deliberately called ready-for-review - minimum structural readiness, not "full depth":
   the live research lane's full-depth standard is not codified in this repo, and which
   county sources are reachable is not recorded here, so this layer never claims either.
   `gates.readiness_for(con, property_id)` is the live read-only view of one property's
   depth for the rest of the app to consult as an assessment input; it writes nothing and
   never produces a score or tier.

## What each lane does

| Lane | Module | Facts it stores |
|---|---|---|
| CMS pool | `cms_pool.py` | Provider Info (rating, fines, denials, utilization), Penalties (summed per facility), Ownership (record entries only), plus the reconciliation |
| Cost reports | `cost_reports.py` | SNF Cost Report figures as reported, worksheet lines, verbatim periods, `rpt_rec_num`, odd components flagged |
| County tax | `county_tax.py` | Billed/paid by tax year (paid = counter-evidence), tax-sale/redemption events, bounded lien searches, unread dockets, unattributed leads |
| Source discovery | `discovery.py` | Candidate public-record sources from the project's existing search provider (see paid APIs below) |

## Running it

```bash
python -m research ingest     # rebuild evidence from stored CMS files, re-derive the gates
python -m research report     # the honest counts + held properties + unmet lanes
python -m research discover --name "Fake Pines" --county Washtenaw --state MI
```

`ingest` is an explicit command, not part of the default weekly collector run: it parses
whole national files, so running it is a decision, not a side effect. It makes no network
calls and no AI calls; it reads what `collectors/state.py` and the manual inbox stored.

## Wiring into the weekly AI research pass (opt-in, off by default)

The autonomous research pass is `agent/weekly.py`. Setting `weekly_loop.research_gates: true`
in `config/agent.yaml` wires this layer into that workflow in both directions:

* **Goal targeting (assessment preparation).** Each property's research goal carries the
  layer's current depth and unmet lanes (`gates.readiness_for`), so the agent spends its
  bounded steps on the checks the method says are missing - a lien search that was never
  run, a primary financial record not yet pulled. Because that steering initiates paid
  search/model calls, it is appended only when the paid-execution opt-in
  (`BLOOMFIELD_RESEARCH_PAID=1`) is also set; the pass's own existing budget is not a
  loophole. The agent's own tools, budgets, the Reader and its spending caps are unchanged.
* **Qualified checks from the agent.** The agent has a `record_research_check` tool for
  the county tax and lien checks: same verbatim-quote proof as `record_evidence`, plus a
  corroborated identity binding - the saved page must show a strong element (the
  property's name, street or parcel) PLUS a second independent detail (city, county,
  ZIP); one bare mention is not identity, so a page about a different or same-named
  facility can never become this property's completed check. A bare web search is
  recorded as an *attempt* (`attempt:` lanes) and never satisfies a counter-evidence
  gate; a failed fetch is recorded as unknown, and unknown never clears a gate.
* **The bridge after the pass.** `agent_bridge.sync_pass` folds the pass's verified agent
  output into the research tables. The goal's `[property_id=...]` marker is routing
  context, never identity: a fact attaches only under the same corroborated binding,
  otherwise it stays unattached (a misattributed claim is never propagated). Claim names,
  units and periods travel unchanged (an unstated period is labeled unknown);
  agent-fetched web figures are context, never a primary financial source; and only
  COMPATIBLE figures can contradict - same property, claim field, units and stated
  period. A compatible disagreement opens a conflict and the property is HELD; differing
  or unstated periods are different contexts, not conflicts. No network or model calls.
* **Depth refresh + assessment drafts.** The pass ends by rebuilding evidence from the
  stored files, re-deriving every property's depth (`research_depth`), and writing
  UNSCORED assessment drafts for ready/held properties into the ISOLATED
  `research_drafts` table - validated by the existing assessment lane's own contract
  (`agent.ingest.validate_assessment_row`) but never written to the live
  `research_assessments` store, so a draft can never demote, overwrite or inflate a
  reviewed assessment. Promoting a draft is a separate, person-reviewed step this layer
  does not perform. A draft is an explicit no-score marker: the Hot/Watch/Low tier rubric
  is not codified in this repo (the lane is labelled "rubric v2" in `agent/ingest.py`,
  "rubric v3.3" in the dashboard copy; neither version's criteria are recorded), so
  drafts say that and assign nothing. Held properties stay no-score with the conflict
  named. A failure in the hook is logged into the summary, never thrown through a
  finished pass.

Default is off. The layer never assigns a score or a tier: `readiness_for` is the
read-only input the rest of the app can consult, and drafts are Unscored markers only.

## Paid APIs (implemented, off unless explicitly enabled)

* **Search (Tavily or Brave)** - the project's existing providers in `bot/search.py`, used
  by `discovery.py` to find candidate sources. One query = one provider credit, capped by
  the shared per-run `Budget`; the key lives in `.env`. Even with a provider and key
  configured, discovery refuses until `BLOOMFIELD_RESEARCH_PAID=1` is set - a key existing
  is not consent to spend it. It never scrapes search engines. Candidate domains are
  proposed in `suggested_sources` (status pending) for a person to screen.
* **AI reading** stays with the existing Reader (`pipeline/llm/` + the `reader.yaml`
  spending caps) and the sourcing agent (`agent/`). This layer makes no model calls of
  its own.

## What this layer is not

* **Not a scorer or tierer.** The weekly AI research pass is `agent/weekly.py`; the
  app-side ingestion of outside research rows and provisional research-lane assessments is
  `agent/ingest.py` (table `research_assessments`). This layer writes only its own tables
  (evidence, searches, conflicts, status, drafts) and never touches the live assessment
  store, never assigns a research_score or a Hot/Watch/Low tier.
* **Not a scoring change.** The equation in `scorer/` is frozen; the research layer
  annotates depth and holds, it does not re-rank anything.
* **Not a bulk run.** Nothing here launches a sweep over the property universe by itself.
