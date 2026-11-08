// build_deck.js - builds the mid-point check-in slide deck for Bloomfield Capital.
//
// Run from this folder:   NODE_PATH=$(npm root -g) node build_deck.cjs
// It reads ../content.json, the same facts the demo page uses, so the numbers can never disagree.
// Output: Bloomfield_Midpoint_Deck.pptx (opens in PowerPoint, Keynote and Google Slides).
const fs = require("fs");
const path = require("path");
const pptxgen = require("pptxgenjs");
const { applyTheme } = require("/root/.claude/skills/synced/a13abb0b-f0e3-49b3-ac99-44329ceb197c_0d484aa3-6aef-429d-8b0e-ab730fdd2f13/pptx/scripts/apply_theme.js");

const content = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "content.json"), "utf8"));
const OUT = path.join(__dirname, "Bloomfield_Midpoint_Deck.pptx");

// ---- theme: Michigan navy and maize, light neutral background (matches the example deck) ----
const THEME = {
  name: "Bloomfield Check-in",
  headFontFace: "Calibri", bodyFontFace: "Calibri",
  colors: { dk1: "1B2430", lt1: "FFFFFF", dk2: "00274C", lt2: "F4F3EF",
            accent1: "FFCB05", accent2: "1F4F8F", accent3: "1A7F46", accent4: "8A5A00", accent5: "B42318", accent6: "5B6573",
            hlink: "1F4F8F", folHlink: "5B6573" },
};

const pres = new pptxgen();
pres.layout = "LAYOUT_16x9";                 // 10 x 5.625 inches
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.title = "Bloomfield sourcing tool: mid-point check-in";
pres.author = "Business+Tech AI Sprint team";
pres.company = "University of Michigan";
const C = pres.SchemeColor;

const FOOTER = "Bloomfield sourcing tool  ·  Mid-point check-in";
const M = 0.6;                                // side margin
const W = 10 - 2 * M;

// ---- layouts: one per kind of slide ----
pres.defineSlideMaster({
  title: "CONTENT", background: { color: "F4F3EF" },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: M, y: 0.4, w: W, h: 0.75, fontSize: 34, bold: true, color: C.text2, align: "left", valign: "top", margin: 0 }, text: "" } },
    { text: { text: FOOTER, options: { x: M, y: 5.12, w: 6, h: 0.3, fontSize: 10, color: C.accent6, margin: 0 } } },
  ],
  slideNumber: { x: 9.0, y: 5.12, w: 0.4, h: 0.3, fontSize: 10, color: C.accent6, align: "right" },
});
pres.defineSlideMaster({
  title: "DARK", background: { color: "00274C" },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: M, y: 0.75, w: W, h: 1.2, fontSize: 34, bold: true, color: C.background1, align: "left", valign: "top", margin: 0 }, text: "" } },
    { text: { text: FOOTER, options: { x: M, y: 5.12, w: 6, h: 0.3, fontSize: 10, color: "9FB0C5", margin: 0 } } },
  ],
  slideNumber: { x: 9.0, y: 5.12, w: 0.4, h: 0.3, fontSize: 10, color: "9FB0C5", align: "right" },
});
pres.defineSlideMaster({
  title: "CLOSE", background: { color: "00274C" },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: M, y: 1.2, w: W, h: 1.9, fontSize: 80, bold: true, color: C.background1, align: "left", valign: "top", margin: 0 }, text: "" } },
  ],
});
pres.defineSlideMaster({
  title: "COVER", background: { color: "00274C" },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: M, y: 1.5, w: 6.2, h: 1.7, fontSize: 48, bold: true, color: C.background1, align: "left", valign: "top", margin: 0 }, text: "" } },
  ],
});

// ---- small helpers (a fresh options object every call: pptxgenjs changes them in place) ----
const T = (slide, text, o) => slide.addText(text, Object.assign({ isTextBox: true, margin: 0, valign: "top", fontFace: "Calibri" }, o));
const card = (slide, x, y, w, h, name, fill = C.background1) => slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
  x, y, w, h, rectRadius: 0.08, fill: { color: fill }, line: { color: "E3E0D8", width: 0.75 },
  shadow: { type: "outer", color: "000000", opacity: 0.08, blur: 6, offset: 1, angle: 90 }, objectName: name });
const pill = (slide, text, x, y, w, kind, name) => {
  const k = { good: ["1A7F46", "FFFFFF"], warn: ["FFCB05", "00274C"], low: ["5B6573", "FFFFFF"], bad: ["B42318", "FFFFFF"], navy: ["00274C", "FFFFFF"] }[kind];
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h: 0.26, rectRadius: 0.13, fill: { color: k[0] }, line: { color: k[0], width: 0.5 }, objectName: name });
  T(slide, text, { x, y, w, h: 0.26, fontSize: 10, bold: true, color: k[1], align: "center", valign: "middle", charSpacing: 0.5, objectName: name + " text" });
};
const title = (slide, text) => slide.addText(text.toUpperCase(), { placeholder: "title" });
const notes = (slide, who, text) => slide.addNotes(`Suggested presenter: ${who} (the team decides who presents what).\n\n${text}`);
const STATUS = { built: ["BUILT", "good"], partial: ["PARTLY BUILT", "warn"], later: ["NOT YET", "low"] };

// ================= 1. cover =================
pres.addSection({ title: "Welcome" });
let s = pres.addSlide({ masterName: "COVER", sectionTitle: "Welcome" });
T(s, "BUSINESS+TECH  ·  " + content.event.title.toUpperCase(), { x: M, y: 1.1, w: 7, h: 0.3, fontSize: 14, bold: true, color: C.accent1, charSpacing: 1, objectName: "Kicker" });
s.addText("BLOOMFIELD SOURCING TOOL", { placeholder: "title" });
T(s, content.event.client, { x: M, y: 3.15, w: 6, h: 0.5, fontSize: 24, color: C.background1, objectName: "Client" });
T(s, content.event.when + "  ·  " + content.event.length + "  ·  Video call", { x: M, y: 3.75, w: 7, h: 0.3, fontSize: 14, color: "9FB0C5", objectName: "When" });
T(s, content.team.map(t => t.name + ", " + t.role).join("   ·   "), { x: M, y: 4.3, w: 8.4, h: 0.5, fontSize: 12, color: C.background1, objectName: "Team" });
T(s, "Powered by the Weiser Center for Real Estate", { x: M, y: 5.12, w: 6, h: 0.3, fontSize: 10, color: "9FB0C5", objectName: "Program" });
card(s, 7.3, 1.55, 2.1, 1.5, "Sample data note", "0A3560");
T(s, "ALL SAMPLE DATA", { x: 7.45, y: 1.7, w: 1.8, h: 0.3, fontSize: 12, bold: true, color: C.accent1, objectName: "Sample note title" });
T(s, "Everything on screen is invented. No real Bloomfield data is used.", { x: 7.45, y: 2.02, w: 1.8, h: 0.95, fontSize: 12, color: C.background1, objectName: "Sample note text" });
notes(s, "Vishwa", "Welcome and thank you. 30 minutes: about 8 on the demo and 4 on what works, then most of the call is your feedback. Everything we show is sample data.");

// ================= 2. agenda =================
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Welcome" });
title(s, "+Agenda");
content.agenda.forEach((a, i) => {
  const col = i < 3 ? 0 : 1, row = i % 3, x = M + col * 4.5, y = 1.45 + row * 0.95;
  T(s, "0" + (i + 1), { x, y, w: 0.8, h: 0.7, fontSize: 36, bold: true, color: C.text2, objectName: "Agenda number " + (i + 1) });
  T(s, a.what, { x: x + 0.85, y: y + 0.08, w: 2.6, h: 0.6, fontSize: 18, color: C.text2, valign: "middle", objectName: "Agenda item " + (i + 1) });
  T(s, a.t, { x: x + 3.4, y: y + 0.08, w: 0.6, h: 0.6, fontSize: 12, color: C.accent6, valign: "middle", align: "right", objectName: "Agenda time " + (i + 1) });
});
T(s, "Almost half the call goes to the demo and your feedback.", { x: M, y: 4.45, w: W, h: 0.4, fontSize: 14, italic: true, color: C.accent6, objectName: "Agenda note" });
notes(s, "Vishwa", "Walk the agenda in 30 seconds. We will ask for your feedback throughout, not just at the end.");

// ================= 3. the problem =================
pres.addSection({ title: "Problem and build" });
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Problem and build" });
title(s, "+The problem");
T(s, content.problem.headline.toUpperCase(), { x: M, y: 1.4, w: 5.3, h: 1.4, fontSize: 24, bold: true, color: C.text2, objectName: "Problem headline" });
T(s, content.problem.body, { x: M, y: 2.95, w: 5.2, h: 1.9, fontSize: 14, color: C.accent6, objectName: "Problem body" });
content.problem.stats.forEach((st, i) => {
  const y = 1.4 + i * 1.12;
  card(s, 6.2, y, 3.2, 0.97, "Stat card " + (i + 1));
  T(s, st.n, { x: 6.38, y: y + 0.1, w: 1.75, h: 0.77, fontSize: 32, bold: true, color: C.text2, valign: "middle", objectName: "Stat number " + (i + 1) });
  T(s, st.label, { x: 8.15, y: y + 0.1, w: 1.15, h: 0.77, fontSize: 11, color: C.accent6, valign: "middle", objectName: "Stat label " + (i + 1) });
});
notes(s, "Vishwa", "Bloomfield deployed $200M last year and is aiming for $300M, partly by using AI in sourcing. A bank has strict limits on how it can help a struggling borrower; Bloomfield has few, so reaching the borrower first lets it offer a loan that actually solves the problem. We raise flags; a researcher decides.");

// ================= 4. Bloomfield's example =================
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Problem and build" });
title(s, "+Bloomfield's example");
const story = [["AUGUST", "A student housing building in Ann Arbor promises to open"], ["NOVEMBER", "The opening slips by three months"], ["FALL LEASES", "Students have already signed elsewhere"], ["NEXT FALL", "Empty for a year, no rent, the developer defaults"]];
story.forEach((st, i) => {
  const x = M + i * 2.25;
  card(s, x, 1.4, 1.95, 1.75, "Story card " + (i + 1));
  T(s, st[0], { x: x + 0.15, y: 1.55, w: 1.65, h: 0.3, fontSize: 14, bold: true, color: C.accent4, objectName: "Story step " + (i + 1) });
  T(s, st[1], { x: x + 0.15, y: 1.92, w: 1.65, h: 1.1, fontSize: 14, color: C.text2, objectName: "Story text " + (i + 1) });
  if (i < 3) s.addShape(pres.shapes.RIGHT_ARROW, { x: x + 1.98, y: 2.12, w: 0.24, h: 0.3, fill: { color: C.accent6 }, line: { color: C.accent6, width: 0.25 }, objectName: "Story arrow " + (i + 1) });
});
s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: M, y: 3.5, w: W, h: 1.05, rectRadius: 0.08, fill: { color: C.text2 }, line: { color: C.text2, width: 0.5 }, objectName: "Clue band" });
T(s, "THE CLUE", { x: M + 0.25, y: 3.5, w: 1.3, h: 1.05, fontSize: 18, bold: true, color: C.accent1, valign: "middle", objectName: "Clue label" });
T(s, "The project's website still said “coming Winter 2026” after that date had passed. We want to catch signals like this automatically, across the whole country.", { x: M + 1.6, y: 3.5, w: W - 1.85, h: 1.05, fontSize: 14, color: C.background1, valign: "middle", objectName: "Clue text" });
notes(s, "Vishwa", "This is Bloomfield's own example, from Shana. Our test case reproduces it with invented data, and our tool finds it automatically.");

// ================= 5. what we're building =================
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Problem and build" });
title(s, "+What we're building");
s.addText([{ text: "Every week our tool reads public records, links them to a property, and ranks the owners most likely to be in trouble. ", options: {} }, { text: "A researcher reviews every flag.", options: { bold: true, color: C.text2 } }],
  { isTextBox: true, margin: 0, x: M, y: 1.3, w: W, h: 0.75, fontSize: 16, color: C.accent6, valign: "top", objectName: "Build intro" });
[["INPUT", "Public records: county filings, liens, recorded loans, permits, SEC and bank reports."], ["AI STEP", "Reads each document into the same fields, with the exact source sentence."],
 ["OUTPUT", "A ranked list of properties, with the evidence for each."], ["HUMAN CHECK", "A Bloomfield researcher approves, dismisses or edits every flag."]].forEach((c, i) => {
  const x = M + i * 2.2;
  card(s, x, 2.15, 2.0, 1.8, "Build card " + (i + 1));
  T(s, c[0], { x: x + 0.15, y: 2.28, w: 1.7, h: 0.3, fontSize: 12, bold: true, color: C.text2, charSpacing: 0.5, objectName: "Build label " + (i + 1) });
  T(s, c[1], { x: x + 0.15, y: 2.62, w: 1.7, h: 1.25, fontSize: 13, color: C.accent6, objectName: "Build text " + (i + 1) });
});
s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: M, y: 4.15, w: W, h: 0.7, rectRadius: 0.08, fill: { color: C.text2 }, line: { color: C.text2, width: 0.5 }, objectName: "Never band" });
T(s, "MUST NEVER", { x: M + 0.25, y: 4.15, w: 1.6, h: 0.7, fontSize: 16, bold: true, color: C.accent1, valign: "middle", objectName: "Never label" });
T(s, "Show a fact it cannot point to in a document, or contact anyone.", { x: M + 1.85, y: 4.15, w: W - 2.1, h: 0.7, fontSize: 15, color: C.background1, valign: "middle", objectName: "Never text" });
notes(s, "Neel", "Input, AI step, output, human check. The rule we never break: no fact without the sentence it came from, and the tool never contacts anyone. Bloomfield handles all outreach.");

// ================= 6. the signals (progress) =================
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Problem and build" });
title(s, "+Progress on your signals");
T(s, "The warning signs you asked for, and where each one stands.", { x: M, y: 1.18, w: W, h: 0.3, fontSize: 14, color: C.accent6, objectName: "Progress intro" });
content.signals.forEach((sg, i) => {
  const y = 1.58 + i * 0.49;
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: M, y, w: W, h: 0.44, rectRadius: 0.06, fill: { color: C.background1 }, line: { color: "E3E0D8", width: 0.75 }, objectName: "Signal row " + (i + 1) });
  T(s, sg.name, { x: M + 0.18, y, w: 5.2, h: 0.44, fontSize: 14, bold: true, color: C.text2, valign: "middle", objectName: "Signal name " + (i + 1) });
  pill(s, STATUS[sg.status][0], 8.1, y + 0.09, 1.2, STATUS[sg.status][1], "Signal status " + (i + 1));
});
notes(s, "Maahik", "Built: construction loans that never converted, contractor liens. Partly built: stale opening dates (the rules and scoring work, the weekly website checker is not built yet), foreclosure notices (county sites are manual drop-in), weak loans (scoring works; some SEC and bank field names still need checking). Not yet: WSJ print notices and Ten-X.");

// ================= 7. five steps =================
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Problem and build" });
title(s, "+Five steps, a person at each");
[["COLLECT", "Public records, SEC and bank data, and the Trepp screen if you share one.", "A researcher uploads Trepp; nothing pulls on its own."],
 ["READ", "AI turns each document into fields, with the exact quote.", "Doubtful results go to a person."],
 ["LINK", "Join records about the same property, loan, lender and company.", "Unsure matches wait for a person."],
 ["SCORE", "Points for each independent warning sign.", "Bloomfield owns the weights and filters."],
 ["RANK", "Flags with evidence, strongest first.", "The researcher approves or dismisses."]].forEach((st, i) => {
  const x = M + i * 1.78;
  card(s, x, 1.35, 1.62, 3.4, "Step card " + (i + 1));
  T(s, String(i + 1), { x: x + 0.15, y: 1.45, w: 0.6, h: 0.6, fontSize: 32, bold: true, color: C.accent4, objectName: "Step number " + (i + 1) });
  T(s, st[0], { x: x + 0.15, y: 2.05, w: 1.35, h: 0.3, fontSize: 14, bold: true, color: C.text2, charSpacing: 0.5, objectName: "Step name " + (i + 1) });
  T(s, st[1], { x: x + 0.15, y: 2.4, w: 1.35, h: 1.15, fontSize: 12, color: C.accent6, objectName: "Step text " + (i + 1) });
  T(s, "A PERSON", { x: x + 0.15, y: 3.62, w: 1.35, h: 0.25, fontSize: 10, bold: true, color: C.text2, charSpacing: 0.5, objectName: "Step control label " + (i + 1) });
  T(s, st[2], { x: x + 0.15, y: 3.88, w: 1.35, h: 0.8, fontSize: 11, color: C.accent6, objectName: "Step control text " + (i + 1) });
});
notes(s, "Neel", "Each week the same five steps run, with a Bloomfield control point at each one. We never automate a login, a CAPTCHA, or a paywall: sites that do not allow automated access become a manual drop-in folder.");

// ================= 8. live demo =================
pres.addSection({ title: "Demo" });
s = pres.addSlide({ masterName: "DARK", sectionTitle: "Demo" });
T(s, "+LIVE DEMO", { x: M, y: 0.4, w: 4, h: 0.3, fontSize: 14, bold: true, color: C.accent1, objectName: "Demo kicker" });
s.addText("WATCH IT FIND THE ANN ARBOR PROJECT", { placeholder: "title" });
[["01", "RANK", "This week's flags, strongest first. One HOT project stands out."], ["02", "PROVE", "Open it: every claim has the exact sentence it came from."], ["03", "DECIDE", "The researcher adds it to the weekly list, or dismisses it."]].forEach((d, i) => {
  const x = M + i * 2.95;
  s.addShape(pres.shapes.LINE, { x, y: 2.5, w: 2.7, h: 0, line: { color: "FFCB05", width: 1.5 }, objectName: "Demo rule " + (i + 1) });
  T(s, d[0], { x, y: 2.65, w: 1.2, h: 0.7, fontSize: 40, bold: true, color: C.accent1, objectName: "Demo number " + (i + 1) });
  T(s, d[1], { x, y: 3.35, w: 2.6, h: 0.4, fontSize: 20, bold: true, color: C.background1, objectName: "Demo step " + (i + 1) });
  T(s, d[2], { x, y: 3.8, w: 2.6, h: 0.9, fontSize: 14, color: "C5D2E3", objectName: "Demo text " + (i + 1) });
});
T(s, "Sample data only. A recorded walkthrough is ready if the live demo does not load.", { x: M, y: 4.78, w: 8, h: 0.3, fontSize: 11, italic: true, color: "9FB0C5", objectName: "Demo note" });
notes(s, "Maahik (flags and evidence), Rex (stale opening date), Vishwa (AI Reader)", "Open the demo page and press Walk through. 14 steps follow this agenda; the arrow keys move between them. Tell the client the data is invented. Steps 3 to 9 are the flag walk-through (about 5 minutes); step 10 is the AI Reader. If the page fails, play the screen recording.");

// ================= 9. results =================
pres.addSection({ title: "What is working" });
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "What is working" });
title(s, "+Early results");
const L = content.results.live_reader;
s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: M, y: 1.3, w: 4.3, h: 2.0, rectRadius: 0.08, fill: { color: C.background1 }, line: { color: "E3E0D8", width: 0.75 }, objectName: "Result card" });
T(s, L.ok + " / " + L.documents, { x: M + 0.25, y: 1.38, w: 3.9, h: 1.0, fontSize: 66, bold: true, color: C.text2, valign: "middle", objectName: "Result number" });
T(s, "documents read cleanly by a real AI model", { x: M + 0.25, y: 2.38, w: 3.9, h: 0.3, fontSize: 14, color: C.accent6, objectName: "Result label" });
T(s, L.review + " were sent to a person, and all " + L.review + " were right to be.", { x: M + 0.25, y: 2.78, w: 3.9, h: 0.4, fontSize: 12, italic: true, color: C.accent6, objectName: "Result detail" });
s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: M, y: 3.45, w: 4.3, h: 1.4, rectRadius: 0.08, fill: { color: C.text2 }, line: { color: C.text2, width: 0.5 }, objectName: "Accuracy band" });
T(s, "NOT MEASURED YET", { x: M + 0.25, y: 3.57, w: 3.9, h: 0.3, fontSize: 12, bold: true, color: C.accent1, charSpacing: 0.5, objectName: "Accuracy label" });
T(s, "Accuracy on a labeled test set of about 30 documents (target: about 90% of fields correct). It is the next test we build.", { x: M + 0.25, y: 3.9, w: 3.9, h: 0.9, fontSize: 13, color: C.background1, objectName: "Accuracy text" });
T(s, "WHAT WORKED", { x: 5.2, y: 1.3, w: 4.2, h: 0.3, fontSize: 12, bold: true, color: C.text2, charSpacing: 0.5, objectName: "Worked label" });
content.worked.forEach((w, i) => {
  const y = 1.65 + i * 0.77;
  card(s, 5.2, y, 4.2, 0.7, "Worked card " + (i + 1));
  T(s, w, { x: 5.35, y, w: 3.95, h: 0.7, fontSize: 11.5, color: C.text2, valign: "middle", objectName: "Worked text " + (i + 1) });
});
T(s, content.results.tests.passing + " automated tests pass. " + L.provider + ", " + L.date + ". The run stopped at the free key's daily limit.", { x: 5.2, y: 4.85, w: 4.2, h: 0.2, fontSize: 10, color: C.accent6, objectName: "Result footnote" });
notes(s, "Vishwa", "Be precise: these are invented documents and one real AI model, not real Bloomfield data, and this is not an accuracy figure. We have not measured accuracy yet. 15 of 18 read cleanly; the 3 that were flagged were flagged correctly. The run stopped because the free API key allows only 20 requests a day.");

// ================= 10. what doesn't work yet =================
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "What is working" });
title(s, "+What doesn't work yet");
const WSTAT = { "Caught": "good", "Fixed": "good", "Next": "warn", "In progress": "warn", "Open": "bad", "Need from you": "bad" };
content.weak_spots.forEach((w, i) => {
  const col = i % 3, row = Math.floor(i / 3), x = M + col * 2.98, y = 1.3 + row * 1.85;
  card(s, x, y, 2.8, 1.72, "Gap card " + (i + 1));
  T(s, w.what, { x: x + 0.15, y: y + 0.1, w: 2.5, h: 0.5, fontSize: 13, bold: true, color: C.text2, objectName: "Gap title " + (i + 1) });
  T(s, w.detail, { x: x + 0.15, y: y + 0.62, w: 2.5, h: 0.72, fontSize: 11, color: C.accent6, objectName: "Gap detail " + (i + 1) });
  pill(s, w.status.toUpperCase(), x + 0.15, y + 1.4, 1.3, WSTAT[w.status] || "low", "Gap status " + (i + 1));
});
notes(s, "Neel", "Say these plainly. Clients trust teams that know their weak spots. The most important ones for Bloomfield: no real data yet (we need a few anonymized deals), county sites are manual drop-in until each site's terms are checked, and we need a paid AI key for weekly runs.");

// ================= 11. what we need from you =================
pres.addSection({ title: "Feedback" });
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Feedback" });
title(s, "+What we need from you");
content.asks.forEach((a, i) => {
  const y = 1.3 + i * 0.9;
  card(s, M, y, W, 0.8, "Ask card " + (i + 1));
  T(s, String(i + 1), { x: M + 0.2, y, w: 0.5, h: 0.8, fontSize: 30, bold: true, color: C.accent2, valign: "middle", objectName: "Ask number " + (i + 1) });
  T(s, [{ text: a.ask, options: { bold: true, color: C.text2, fontSize: 15, breakLine: true } }, { text: a.detail, options: { color: C.accent6, fontSize: 11 } }], { x: M + 0.8, y, w: 5.9, h: 0.8, valign: "middle", objectName: "Ask text " + (i + 1) });
  T(s, a.by, { x: 7.5, y, w: 1.75, h: 0.8, fontSize: 12, bold: true, color: C.accent6, valign: "middle", align: "right", objectName: "Ask deadline " + (i + 1) });
});
notes(s, "Maahik (questions 1 and 3), Rex (questions 2 and 4). Neel takes notes for the whole call.", "Ask more than you defend. If the client pushes back, write it down and decide as a team afterward. Question 1: which states and counties first. 2: a few anonymized sample deals, plus the Trepp screen if they are happy to share. 3: which signals would make them pick up the phone, and their size range, property types and markets. 4: any rules on sending public records to an AI service, and who should hold the AI account.");

// ================= 12. what ships Nov 17 =================
pres.addSection({ title: "Nov 17 and next steps" });
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Nov 17 and next steps" });
title(s, "+What you'll see on Nov 17");
s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: M, y: 1.3, w: 4.9, h: 3.2, rectRadius: 0.08, fill: { color: C.text2 }, line: { color: C.text2, width: 0.5 }, objectName: "Ships panel" });
T(s, "IN THE FINAL BUILD", { x: M + 0.25, y: 1.45, w: 4.4, h: 0.4, fontSize: 20, bold: true, color: C.accent1, objectName: "Ships heading" });
s.addText(content.ships.in.map((t, i, a) => ({ text: t, options: { bullet: true, breakLine: i < a.length - 1, paraSpaceAfter: 6 } })),
  { isTextBox: true, margin: 0, x: M + 0.25, y: 1.95, w: 4.45, h: 2.45, fontSize: 13, color: C.background1, valign: "top", objectName: "Ships list" });
s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 5.7, y: 1.3, w: 3.7, h: 3.2, rectRadius: 0.08, fill: { color: C.background1 }, line: { color: "B8B4A8", width: 1, dashType: "dash" }, objectName: "Later panel" });
T(s, "SAVED FOR PHASE TWO", { x: 5.95, y: 1.45, w: 3.3, h: 0.4, fontSize: 16, bold: true, color: C.text2, objectName: "Later heading" });
s.addText(content.ships.later.map((t, i, a) => ({ text: t, options: { bullet: true, breakLine: i < a.length - 1, paraSpaceAfter: 6 } })),
  { isTextBox: true, margin: 0, x: 5.95, y: 1.95, w: 3.3, h: 2.45, fontSize: 12, color: C.accent6, valign: "top", objectName: "Later list" });
notes(s, "Vishwa", "This is our proposal; ask Bloomfield to confirm or change it. Before hanging up, READ THIS SLIDE BACK and make sure everyone agrees on what ships on November 17.");

// ================= 13. next steps =================
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Nov 17 and next steps" });
title(s, "+Next steps");
content.next_steps.forEach((n, i) => {
  const x = M + i * 1.78;
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 1.4, w: 1.62, h: 2.25, rectRadius: 0.08, fill: { color: C.text2 }, line: { color: C.text2, width: 0.5 }, objectName: "Next card " + (i + 1) });
  T(s, n.when, { x: x + 0.15, y: 1.55, w: 1.35, h: 0.45, fontSize: 18, bold: true, color: C.accent1, objectName: "Next date " + (i + 1) });
  T(s, n.title.toUpperCase(), { x: x + 0.15, y: 2.05, w: 1.35, h: 0.35, fontSize: 15, bold: true, color: C.background1, objectName: "Next title " + (i + 1) });
  T(s, n.text, { x: x + 0.15, y: 2.45, w: 1.35, h: 1.35, fontSize: 12, color: "C5D2E3", objectName: "Next text " + (i + 1) });
});
T(s, "After this call we send a short recap: what we agreed, what we are changing, and what we need from you and by when.", { x: M, y: 3.95, w: W, h: 0.6, fontSize: 14, color: C.accent6, objectName: "Recap note" });
notes(s, "Vishwa", "Dates: Nov 10 is Session 3 (we finalize the build and begin the pitch deck). Nov 13 all four parts run on real public data. Nov 15 full end-to-end run and demo flag chosen. Nov 17 final presentation.");

// ================= 14. questions =================
s = pres.addSlide({ masterName: "CLOSE", sectionTitle: "Nov 17 and next steps" });
s.addText("QUESTIONS?", { placeholder: "title" });
T(s, "Thank you. We'll send a recap within 24 hours.", { x: M, y: 3.9, w: 8, h: 0.4, fontSize: 18, bold: true, color: C.accent1, objectName: "Thanks" });
T(s, "Business+Tech AI Sprint  ·  Powered by the Weiser Center for Real Estate", { x: M, y: 4.4, w: 8, h: 0.3, fontSize: 12, color: "9FB0C5", objectName: "Program footer" });
notes(s, "Everyone", "Before you hang up: read back what you agreed will ship on November 17.");

(async () => {
  await pres.writeFile({ fileName: OUT });
  await applyTheme(OUT, THEME);
  console.log("Wrote " + OUT);
})();
