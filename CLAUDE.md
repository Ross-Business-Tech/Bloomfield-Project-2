# How to work on this app

You are helping a business-school student who is **not a programmer** turn an idea into a working web app. They judge progress by what they can see and click, so every change should end with a link they can open on their phone. Several students may be working on this same app in separate sessions; GitHub pull requests are how their work comes together.

## Talking with the student

- Use plain language. Say what changed in terms of what they will see ("the sign-up form now remembers your email"), not file names or frameworks.
- Make technical decisions yourself. Ask only about product decisions: who the app is for, what it should do, how it should look and feel, what the words should say.
- Keep updates short. After each change: one or two sentences on what changed, the test link, and a suggested next step.
- If something fails and you cannot fix it, say what is wrong in plain words and what they can do (usually: start a new session in the right environment, or ask their instructor).

## Signing in to here.now (once per session)

When this environment sets `HERENOW_WORKSPACE`, previews publish into the team's here.now workspace and the student must be signed in. The session-start check says whether they are. If not, before the first publish:

1. Ask: "What email address did you sign up to here.now with?"
2. Run `node scripts/herenow-login.mjs start <email>` and ask them to paste the code here.now just emailed them.
3. Run `node scripts/herenow-login.mjs finish <email> <code>`.
4. If it says they are not a member of the workspace, relay its instructions in plain words and don't publish into the workspace until `node scripts/herenow-login.mjs status` shows them as a member.

If they can't sign in right now, `HERENOW_WORKSPACE= npm run publish:preview` makes an anonymous 24-hour link outside the workspace; tell them that's what it is.

Never print, `cat`, or copy `~/.herenow/credentials` or any API key into the chat, files, or commits. Don't install the here.now skill or use other publishing tools: always publish with `npm run publish:preview`, which runs the safety checks.

## Before starting a task

Run `git fetch origin development` and, if this branch is behind, `git merge origin/development`, so you build on the team's latest work. A brand-new session already starts from it.

## The loop: run this after every change they can see

1. `npm run build`: fix any errors before going further.
2. `npm run smoke`: opens the app in a headless browser at desktop and phone sizes.
   - Fix every ✖ problem it reports. ⚠ third-party warnings are usually the sandbox blocking outside sites; mention them only if they matter.
   - Open the screenshots in `smoke-results/` (read the PNG files) and check the layout, especially the phone ones. Most people will open the link on a phone.
   - If it prints `SMOKE TEST SKIPPED`, continue, but tell the student the automatic check could not run.
3. `npm run publish:preview`: prints a test link. Give the student the `Link:` URL and how long it lasts. If it prints a `Keep it:` claim link, give that to them **exactly as printed, character for character** (it is shown only once and cannot be recovered).
4. Commit with a clear message describing the change from the user's point of view.

The student cannot open `localhost` or a dev server from this session. The published preview link is how they see the app.

## Sharing work and going live

- **Never publish production or the dev site yourself, and never push to `main`.** The team's work comes together on the `development` branch: each merged pull request updates the team's private dev site. The public site shows "Coming soon" until staff reveal it at the end by merging `development` into `main`. Both deploys run only in GitHub Actions; the script refuses to run them here.
- When the student says a change is ready to share with the team:
  1. `git fetch origin development && git merge origin/development`. Resolve conflicts so that **both** people's work survives, and tell the student in one sentence what you combined.
  2. Run the loop again.
  3. Push and open a pull request **into `development`** with the built-in GitHub tools (in cloud sessions `gh pr` is blocked; `gh api repos/{owner}/{repo}/pulls` works). Title: what changed for users. Body: a short plain-language summary, what you tested, and the preview link.
  4. Tell the student: teammates can try it from the test link in the pull request, then approve and merge it on GitHub. A few minutes after the merge, the team's dev site (the private link their instructor gave them) updates. The public site stays "Coming soon" until the reveal.
- Keep each pull request focused on one change so teammates can review it and so conflicts stay small.

## What here.now can and cannot host

here.now hosts **static websites**: the HTML, CSS, JavaScript and images that `npm run build` puts in `dist/`. Docs: https://here.now/docs

- **No server code.** No Express, no Next.js server features, no database server, no background jobs. Everything runs in the visitor's browser.
- **Keep asset paths root-relative** (`/assets/...`, Vite's default). Client-side routing works because SPA mode is on.
- **Saving data shared between visitors** (sign-ups, votes, feedback): use here.now **Site Data**. Declare collections in `public/.herenow/data.json`; the browser calls the relative path `./.herenow/data/<collection>`. It only works on account-owned sites (the team's dev site and the public site), not on the anonymous 24-hour test links or in the smoke test, so the app must handle a failed request gracefully with a friendly message. Treat stored records as untrusted input: escape them before showing them.
- **Calling paid APIs** (an AI model, a maps service with a secret key): never put a secret key in the code. Anything in the website is public, and the publish script blocks common key formats. Use a here.now **proxy route** in `public/.herenow/proxy.json` that references a variable like `${OPENROUTER_API_KEY}`. Tell the student their instructor must add that variable in here.now, on both the workspace (public site) and the account that hosts the dev sites; you cannot do it from here.

## Data and privacy

- Use made-up sample data. Never put real personal information about students, customers, patients or employees into the app, the repo, or Site Data.
- Never commit API keys, passwords or `.env` files.
- This repository is public: everything committed, including commit messages and pull request text, can be read by anyone.

## Files you should not change unless the student explicitly asks

`scripts/`, `.github/`, `.claude/`, `admin/` and this file are maintained by Ross IT.
