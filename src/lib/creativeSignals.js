// "Finance brain" signals: extra ways to spot a borrower in trouble, and the deal angle for Bloomfield.
//
// Beyond the stale opening date, the scout reads the same leasing page (and its old copies) for:
//   - concession creep     (free months / gift cards growing = lease-up is failing)
//   - asking-rent cuts     (lower rents = lower NOI = smaller permanent loan)
//   - stalled pre-leasing  ("48% leased" barely moving)
//   - already for sale / receiver / new management
//   - website going dark   (domain parked, account suspended)
// Then it runs the test a permanent lender would run:
//   - refinance gap        (how far the construction loan exceeds what a perm loan would size to)
//   - equity cushion       (loan vs. estimated value)
// And it looks across the portfolio:
//   - sponsor contagion    (the same sponsor's other projects are already flagged)
//   - note-sale angle      (a CRE-heavy bank holding a troubled loan may sell the note at a discount)

const WORD_NUM = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6 }

/** Value of leasing concessions on the page, in months of rent. */
export function readConcessions(text, rent) {
  if (!text) return { months: 0, dollars: 0, cues: [] }
  const cues = []
  let months = 0
  let dollars = 0
  for (const m of text.matchAll(/\b(\d+(?:\.\d+)?|one|two|three|four|five|six)\s+(weeks?|months?)\s+(?:of\s+)?(?:free|rent[- ]free|free rent)/gi)) {
    const n = WORD_NUM[m[1].toLowerCase()] ?? Number(m[1])
    months += m[2].toLowerCase().startsWith('week') ? n / 4.33 : n
    cues.push(m[0])
  }
  for (const m of text.matchAll(/\$\s?([\d,]+)\s+(?:off|gift card|visa|amazon|move[- ]in (?:special|credit|bonus)|credit|look[- ]and[- ]lease)/gi)) {
    dollars += Number(m[1].replace(/,/g, ''))
    cues.push(m[0])
  }
  for (const m of text.matchAll(/\b(?:no|zero|\$0)\s+(?:security\s+)?deposit\b|\b(?:application|admin|amenity) fees? waived\b/gi)) {
    cues.push(m[0])
  }
  const total = months + (rent ? dollars / rent : 0)
  return { months: total, dollars, cues }
}

/** Lowest asking rent quoted on the page ($/month). */
export function readRent(text) {
  if (!text) return null
  const rents = [...text.matchAll(/\$\s?([\d,]{3,6})(?:\.\d{2})?\s*(?:\/|per\s+)(?:mo\b|month|bed|bedroom|person|unit)/gi)]
    .map((m) => Number(m[1].replace(/,/g, '')))
    .filter((n) => n >= 300 && n <= 20000)
  return rents.length ? Math.min(...rents) : null
}

/** "48% pre-leased" → 48 */
export function readLeasedPct(text) {
  const m = text?.match(/\b(\d{1,3})\s?%\s*(?:pre-?leased|leased|occupied|reserved|sold)/i)
  return m ? Math.min(100, Number(m[1])) : null
}

const FOR_SALE = /\b(investment opportunity|offered for sale|for sale|call for offers|best and final|court[- ]appointed receiver|receivership|under new management|chapter 11|bankruptcy)\b/i
const GONE_DARK = /\b(domain (?:may be|is) for sale|account (?:has been )?suspended|site (?:is )?(?:no longer|temporarily) (?:available|unavailable)|page not found|this site can[’']?t be reached)\b/i

const DAY = 864e5

/** Monthly payment constant x 12 for a fully amortizing loan. */
export function annualConstant(rate, years = 30) {
  const r = rate / 12
  return (12 * r) / (1 - Math.pow(1 + r, -years * 12))
}

export const PERM_TERMS = { dscr: 1.25, maxLtv: 0.65, amortYears: 30 }

/**
 * What a permanent lender would lend once the building is stabilized, vs. the construction loan.
 * f: { loanAmount, units, rentPerUnit, occupancy (0-1), expenseRatio (0-1), permRate (0-1), capRate (0-1) }
 */
export function refinanceTest(f) {
  if (!f) return null
  const nums = ['loanAmount', 'units', 'rentPerUnit', 'occupancy', 'expenseRatio', 'permRate', 'capRate']
  if (nums.some((k) => !(Number(f[k]) > 0))) return null
  const noi = f.units * f.rentPerUnit * 12 * f.occupancy * (1 - f.expenseRatio)
  const value = noi / f.capRate
  const k = annualConstant(f.permRate, PERM_TERMS.amortYears)
  const byDscr = noi / (PERM_TERMS.dscr * k)
  const byLtv = value * PERM_TERMS.maxLtv
  const permLoan = Math.min(byDscr, byLtv)
  const gap = f.loanAmount - permLoan
  const ltv = f.loanAmount / value
  const dscrAtLoan = noi / (f.loanAmount * k)
  return { noi, value, byDscr, byLtv, permLoan, gap, ltv, dscrAtLoan, binding: byDscr < byLtv ? 'DSCR' : 'LTV' }
}

const BANKS = ['Community bank', 'Regional bank']

/**
 * Extra signals for one project.
 * base: result of analyzeProject; context: { sponsorFlags: [names of the sponsor's other flagged projects] }
 */
export function analyzeCreative(project, base, context = {}, today = new Date()) {
  const signals = []
  const add = (key, label, detail, points) => signals.push({ key, label, detail, points })

  const snaps = (project.snapshots ?? [])
    .filter((s) => s.date && s.text)
    .map((s) => ({ ...s, seen: new Date(`${s.date}T12:00:00`) }))
    .sort((a, b) => a.seen - b.seen)
  const oldest = snaps[0]
  const text = project.pageText ?? ''
  const rentNow = readRent(text)
  const rentRef = rentNow ?? (Number(project.finance?.rentPerUnit) || null)

  // Concession creep
  const concNow = readConcessions(text, rentRef)
  const concThen = oldest ? readConcessions(oldest.text, rentRef) : null
  if (concNow.months >= 1 && (!concThen || concNow.months > concThen.months + 0.25)) {
    const was = concThen ? `, up from ${concThen.months >= 0.05 ? concThen.months.toFixed(1) : 'none'}` : ''
    add('concessions', 'Concession creep', `Giving away about ${concNow.months.toFixed(1)} months of rent per lease${was} (${concNow.cues.join('; ')}).`, concNow.months >= 2 ? 10 : 6)
  } else if (concNow.cues.length) {
    add('concessions', 'Leasing incentives', `Offering ${concNow.cues.join('; ')}.`, 3)
  }

  // Rent cuts
  const oldRents = snaps.map((s) => readRent(s.text)).filter(Boolean)
  if (rentNow && oldRents.length) {
    const peak = Math.max(...oldRents)
    const cut = (peak - rentNow) / peak
    if (cut >= 0.03) add('rent-cut', 'Asking rent cut', `Rent cut from $${peak.toLocaleString()} to $${rentNow.toLocaleString()} (${Math.round(cut * 100)}% lower). Lower rent means a smaller permanent loan.`, cut >= 0.08 ? 10 : 6)
  }

  // Stalled pre-leasing
  const pctNow = readLeasedPct(text)
  const pctSnap = [...snaps].reverse().find((s) => readLeasedPct(s.text) != null)
  if (pctNow != null) {
    if (pctSnap) {
      const pctThen = readLeasedPct(pctSnap.text)
      const days = (today - pctSnap.seen) / DAY
      if (days >= 90 && pctNow - pctThen <= 10 && pctNow < 90) {
        add('lease-stall', 'Leasing has stalled', `${pctThen}% leased on ${pctSnap.date}, only ${pctNow}% now.`, 8)
      }
    } else if (pctNow < 60 && (base.verdict === 'flag' || base.verdict === 'open')) {
      add('lease-low', 'Low leasing', `Only ${pctNow}% leased.`, 6)
    }
  }

  // Already for sale, receiver, new management
  const sale = text.match(FOR_SALE)
  if (sale) add('for-sale', 'Already looking for a way out', `Page mentions “${sale[0]}”. The owner may take a fast, quiet deal.`, 10)

  // Website gone dark
  const dark = text.match(GONE_DARK)
  if (dark) add('dark', 'Website has gone dark', `Page now shows “${dark[0]}”. The sponsor may have stopped paying bills.`, 10)

  // Refinance gap
  const refi = refinanceTest(project.finance)
  if (refi && refi.gap > 0) {
    add('refi-gap', 'Cannot refinance out', `A permanent loan would size to about ${money(refi.permLoan)}, ${money(refi.gap)} short of the ${money(project.finance.loanAmount)} construction loan.`, refi.gap / project.finance.loanAmount >= 0.15 ? 12 : 8)
  }

  // Sponsor contagion
  const others = context.sponsorFlags ?? []
  if (others.length) add('sponsor', 'Sponsor under pressure elsewhere', `Same sponsor's other flagged project${others.length > 1 ? 's' : ''}: ${others.join(', ')}.`, Math.min(16, 8 * others.length))

  const pressure = signals.reduce((s, x) => s + x.points, 0)
  const angles = dealAngles(project, base, refi, signals)
  const quality = dealQuality(project, base, refi, signals)
  return { signals, pressure, refi, angles, quality }
}

function dealQuality(project, base, refi, signals) {
  let score = 50
  const notes = []
  if (project.propertyType === 'Office') {
    score -= 30
    notes.push('Office: outside Bloomfield’s box')
  } else {
    score += 10
    notes.push(`${project.propertyType}: in Bloomfield’s box`)
  }
  if (refi) {
    if (refi.ltv <= 0.75) { score += 25; notes.push(`Healthy cushion (loan is ${pct(refi.ltv)} of value)`) }
    else if (refi.ltv <= 0.9) { score += 10; notes.push(`Thin cushion (loan is ${pct(refi.ltv)} of value)`) }
    else if (refi.ltv <= 1) { notes.push(`Very thin cushion (loan is ${pct(refi.ltv)} of value)`) }
    else { score -= 25; notes.push(`Underwater (loan is ${pct(refi.ltv)} of value)`) }
    const size = Number(project.finance.loanAmount)
    if (size >= 5e6 && size <= 50e6) { score += 5; notes.push('Loan size fits the $5M to $50M range') }
  }
  if (base.verdict === 'flag' || base.verdict === 'watch') {
    score += 10
    notes.push('Problem looks temporary (a delay, not a lack of demand)')
  }
  if (signals.some((s) => s.key === 'rent-cut') && signals.some((s) => s.key === 'concessions')) {
    score -= 10
    notes.push('Rent cuts plus concessions may mean weak demand')
  }
  if (!signals.some((s) => s.key === 'for-sale')) { score += 5; notes.push('Still off-market') }
  return { score: Math.max(0, Math.min(100, Math.round(score))), notes }
}

function dealAngles(project, base, refi, signals) {
  const angles = []
  const has = (k) => signals.some((s) => s.key === k)
  if (refi && refi.gap > 0) {
    angles.push(
      refi.ltv <= 0.9
        ? `Rescue loan or preferred equity of about ${money(refi.gap)} to bridge the refinance gap. There is still a cushion, so this is a fixable problem.`
        : `Gap of ${money(refi.gap)} with the loan at ${pct(refi.ltv)} of value. Consider buying the debt at a discount instead of lending new money.`,
    )
  }
  const lender = project.lender
  if (lender?.name && BANKS.includes(lender.type) && (base.verdict === 'flag' || (refi && refi.gap > 0))) {
    angles.push(`Note purchase: ${lender.name} (${lender.type.toLowerCase()}) may sell this loan at a discount to clean up its books. Check its FFIEC CRE nonaccruals.`)
  }
  if (has('for-sale')) angles.push('Owner is already marketing an exit. Offer a fast close or financing for a buyer.')
  if (base.verdict === 'flag' && project.propertyType === 'Student housing') {
    angles.push('Bridge loan to carry interest until next August’s move-in, when rent starts.')
  }
  if (!angles.length && signals.length) angles.push('Not actionable yet. Keep it on the watch list for a capital call.')
  return angles
}

/** Merges the stale-opening result with the extra signals into one ranked verdict. */
export function combine(base, creative) {
  const out = { ...base, creative, reasons: [...base.reasons] }
  // Extra signs add points with diminishing returns, so strong flags still rank against each other.
  const boost = (start) => Math.round(start + (100 - start) * (1 - Math.exp(-creative.pressure / 30)))
  if (base.verdict === 'flag') {
    out.score = boost(base.score)
  } else if (creative.pressure >= 10 && ['on-time', 'open', 'co-issued', 'unreadable', 'watch'].includes(base.verdict)) {
    if (base.verdict !== 'watch') out.reasons.push('Other warning signs below put this on the watch list.')
    out.verdict = 'watch'
    out.score = Math.min(65, boost(0)) // watch items stay below real flags
  }
  return out
}

export const money = (n) => {
  const a = Math.abs(n)
  const s = a >= 1e6 ? `$${(a / 1e6).toFixed(1)}M` : a >= 1e3 ? `$${Math.round(a / 1e3)}K` : `$${Math.round(a)}`
  return n < 0 ? `-${s}` : s
}
export const pct = (x) => `${Math.round(x * 100)}%`
