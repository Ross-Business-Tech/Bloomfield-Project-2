# Bloomfield mid-point call: plan (30 minutes)

Everything on screen is sample data. Nothing real from Bloomfield is used.

## What is ready

| Item | Where |
|---|---|
| Slide deck (14 slides, speaker notes on every slide) | `pipeline/demo/deck/Bloomfield_Midpoint_Deck.pptx` (opens in PowerPoint, Keynote and Google Slides) |
| Working demo with a guided walkthrough | `pipeline/demo/demo.html`, or run `python -m demo --open` from `pipeline/` |
| Backup screen recording of the walkthrough | `walkthrough.webm` (plays in Chrome, Edge, Firefox, VLC; QuickTime needs VLC) |
| Test results | Slides 9 and 10, and the "Test results" tab of the demo |

The deck and the demo read their facts (numbers, asks, scope) from the same file, `pipeline/demo/content.json`. Change it once, then run `python -m demo` and `NODE_PATH=$(npm root -g) node build_deck.cjs` in `pipeline/demo/deck/`.

## Before the call (Team Lead)

- [ ] Confirm the time with Shana a few days ahead, and resend the video link. (The deck says "Week of November 9, 2026": put in the exact day and time.)
- [ ] Send the deck the day before.
- [ ] Decide who presents what (suggestion below). Everyone talks.
- [ ] Rehearse once with a timer. Thirty minutes goes faster than you think.
- [ ] Show only sample data on screen (it is all sample).
- [ ] Run the demo on the laptop you will share from. Keep `walkthrough.webm` open in another window as the backup.
- [ ] Write the questions down and decide who asks which (slide 11 has four).
- [ ] Update the Map Your Build worksheet with anything that changed (it is not in this repo, so this one is by hand).

## Run sheet

| Time | Slide(s) | Demo step(s) | Suggested presenter |
|---|---|---|---|
| 0:00 | 1-2 Welcome, agenda | | Vishwa |
| 0:03 | 3-7 Problem and build | 1-2 (Start here, How it works) | Vishwa (problem), Neel (build, five steps), Maahik (signals) |
| 0:08 | 8 Live demo | 3-10 (flags, the Ann Arbor project, source sentence, stale opening, lender, decision, AI Reader) | Maahik (flags and evidence), Rex (stale opening), Vishwa (AI Reader) |
| 0:16 | 9-10 Results, what does not work | 11-12 (Data sources, Test results) | Vishwa (numbers), Neel (gaps) |
| 0:20 | 11 What we need from you | 13 (Four questions) | Maahik (questions 1, 3), Rex (2, 4). **Neel takes notes.** |
| 0:27 | 12-14 Nov 17, next steps, questions | 14 (What ships Nov 17) | Vishwa reads it back |

In the demo, press **Walk through** (top right) and use the arrow keys. **Speaker notes** on the bottom bar are for you; hide them before sharing.

## During the call

- Show what does not work yet (slide 10). Clients trust teams that know the weak spots.
- Ask more than you defend. If Bloomfield pushes back, write it down and decide as a team afterward.
- One person takes notes (the demo has a notes box on the last tab).
- Before you hang up, read slide 12 back: what ships on November 17.

## After the call (Team Lead)

Email Shana before Session 3 (Nov 10):

> Subject: Recap of today's check-in, and what we need from you
>
> Hi Shana, thank you for your time and feedback today.
>
> **What we agreed will ship on Nov 17:** [read back slide 12 and paste the final list]
> **What we're changing because of your feedback:** [from the notes]
> **What we need from you, and by when:**
> 1. [states and counties to start with, if not answered on the call]
> 2. A few anonymized sample deals (and the Trepp screen if you're happy to share one): by Tue, Nov 10
> 3. [anything else]
>
> Thanks again,
> [name], for the team

Then bring the recap and Bloomfield's feedback to Session 3 on November 10.

## Be ready to explain

- **Accuracy is not measured yet.** The 15 / 18 is a real AI model on invented documents, not an accuracy figure.
- **FFIEC item codes and SEC field names are unverified.** The tool prints an audit when it loads real files.
- **The free AI key allows 20 requests a day.** The weekly run needs a paid key.
- The tool never contacts anyone and never logs in, solves a CAPTCHA or gets around a paywall.
