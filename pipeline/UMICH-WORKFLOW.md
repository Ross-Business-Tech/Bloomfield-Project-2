# Bloomfield: U-M Toolkit setup and workflow

This branch now defaults to U-M Toolkit in `config/reader.yaml`, even when only the key is supplied. Existing environment or `.env` overrides still win and must be corrected if stale.

## The key and the endpoint must agree

A U-M GPT Toolkit credential is not an OpenAI platform key or a ChatGPT/Codex login.
The Python variable is still named `OPENAI_API_KEY` because U-M offers an OpenAI-compatible API.
For this project, use:

```dotenv
OPENAI_API_KEY=<set securely, never commit>
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.toolkit.umgpt.umich.edu/v1
LLM_MODEL_CHEAP=gpt-4o-mini
LLM_MODEL_STRONG=gpt-4o
```

The wrapper adds `/chat/completions` and sends `Authorization: Bearer <key>` on the server.
Do not append `/responses`, `/chat/completions` or another `/v1` to the base URL.
Do not use `VITE_*` variables or put the key in a browser, React bundle, public page, GitHub file, screenshot or chat.
A GitHub Actions secret only reaches a workflow if that workflow explicitly injects it. It does not configure a laptop or Railway.

## Local setup

From a clean clone of the team repository's `main` branch (or the reviewed working branch):

```bash
cd Bloomfield-Project-2/pipeline
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If `pipeline/.env` does not exist, copy `.env.example` to `.env`; otherwise merge the settings above without overwriting other settings.
Add the actual credential in a local editor or secret manager. `.env` stays git-ignored.
Use a real monitored `CONTACT_EMAIL` for SEC requests.
Existing exported shell variables override `.env`; clear stale provider/model/key settings or open a clean shell before testing.

```bash
python -m llm.check
# Optional diagnostic preset in this branch, using the same saved key:
python -m llm.check --umich
```

The preset only affects the check's process, not future batch jobs. Persist the settings in `.env` too.
Success requires BOTH the cheap JSON call and the strong fake-lien extraction with quote checks to pass.
This is a real, small API test, not an offline unit test. It sends only invented material and prints no key.
The saved project credential passed both tests on November 15, 2026. That proves that credential and configuration at test time, not another teammate's copied key or machine.

## What runs, in order

1. **Collect or import approved source material.** Structured sources such as SEC XML parse directly into tables. County, recorder, court, permit, NETR and state sources must remain off until source-specific permission and privacy checks are complete. Repository assertions are not proof of permission. FFIEC/Trepp remain manual imports under their own terms.
2. **Read eligible unstructured documents.** `reader.batch` extracts fields, validates exact supporting quotes, caches responses and records failures/review items. Confidential Bloomfield material stays blocked. Do not put real personal information in the app, repo or Site Data, including a private database used by the app.
3. **Match records.** `matcher` links property/loan/entity records. A person decides uncertain matches.
4. **Score weekly.** `scorer.weekly` computes the existing equation without inventing missing signals.
5. **Review the dashboard.** Check evidence, uncertain matches and research notes. A person reviews flags; nothing contacts property owners.

A conservative local SEC-only workflow, in a NEW local data directory, with the dashboard stopped:

```bash
export BLOOMFIELD_DATA_DIR="$PWD/data/local-live"
python -m collectors.run_all --only edgar
python -m reader.batch --limit 5
python -m matcher
python -m matcher --review
python -m scorer.weekly
BLOOMFIELD_DB="$BLOOMFIELD_DATA_DIR/bloomfield.duckdb" streamlit run dashboard/app.py
```

Do not run the broad `python -m bot` or all-source collection as a setup test. The SEC-only cycle can legitimately report zero documents read: SEC XML is structured and deliberately skipped by the AI reader. Zero ranked flags is also expected with SEC-only context because CMBS DSCR/watchlists do not contribute to this scoring equation. Neither proves an API-key failure. Use `llm.check` to test AI separately.

Keep demo data (`demo.duckdb`) and live data separate. All stages must use the same local database. `BLOOMFIELD_DB` selects the dashboard's database, while CLI collectors/reader default to `$BLOOMFIELD_DATA_DIR/bloomfield.duckdb`. Do not point the dashboard at `live.duckdb` while a CLI writes to a different default file.

## Hosted workflow

The deployed fork uses `python -m dashboard.serve`, one process, one volume and one DuckDB database. It starts the scheduler at boot, not when somebody first opens a page.
Current approved scope: SEC EDGAR only, a 300-second initial delay after restart, then 168-hour intervals. Restarts reset that initial delay; this is not a fixed weekday cron.
The scheduler passes its open connection to the reader, matcher and scorer so they all use the dashboard's `data/live.duckdb`.
Do not start a second CLI process writing that live DuckDB file. Check Railway logs and the latest weekly run for the real outcome; a green dashboard alone is not proof of an AI call.
Railway service variables and local `.env` are separate. Changing one does not update the other. The team repo and deployed fork are also separate until reviewed changes are merged/synced.

## When it fails

- **401/403:** confirm the key's provider, active entitlement, current credential and the exact endpoint. Do not print the key. Replace through a secure channel if invalidated.
- **404/model not found:** check provider, base URL and model access. U-M's chat adapter is not the OpenAI Responses/Codex adapter.
- **Missing key:** use `pipeline/.env`, not a root `.env` or front-end variable. Check the process actually receives it.
- **429:** quota/rate limit; honor backoff and check allocation. Do not loop retries manually.
- **5xx/timeouts:** possible provider maintenance or networking. Distinguish that from invalid credentials.
- **Check passes, reader reads zero:** inspect pending eligible documents; SEC rows are not unstructured documents.
- **Rows exist, ranking empty:** inspect the input signals and score equation, not the API key.

For debugging share the exact command, branch/commit, runtime (local/Railway), provider/model names and redacted error. Never share the key or a raw environment dump.

Cost limits are estimates using the prices in `config/reader.yaml`, not a provider-enforced billing guarantee. Keep the existing $10/week and $5/run limits; verify provider prices before relying on them for a larger run.
