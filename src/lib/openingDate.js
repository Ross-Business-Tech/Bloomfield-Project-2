// Stale-opening check (Part B, scout/openings.py in the build plan, done in the browser).
//
// 1. Find the sentence on a project's website that states the opening date.
// 2. Turn the phrase ("Opening Summer 2026") into a deadline, always using the
//    LATEST reasonable date so we never flag a project that isn't late yet.
// 3. Read whether the page still says "coming soon" / "pre-leasing".
// 4. Compare old snapshots (Wayback Machine) to count how often the date slipped.
// 5. Combine with the certificate-of-occupancy status into a verdict and a 0-100 strength.

const MONTH_INDEX = {
  jan: 0, january: 0, feb: 1, february: 1, mar: 2, march: 2, apr: 3, april: 3,
  may: 4, jun: 5, june: 5, jul: 6, july: 6, aug: 7, august: 7,
  sep: 8, sept: 8, september: 8, oct: 9, october: 9, nov: 10, november: 10,
  dec: 11, december: 11,
}
const MONTH = '(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
const YEAR = "((?:19|20)\\d{2}|['’]\\d{2})"

const toYear = (s) => {
  const digits = s.replace(/\D/g, '')
  return digits.length === 2 ? 2000 + Number(digits) : Number(digits)
}
const endOfMonth = (y, m) => new Date(y, m + 1, 0)
const date = (y, m, d) => new Date(y, m, d)

const ORDINAL = { first: 1, '1st': 1, second: 2, '2nd': 2, third: 3, '3rd': 3, fourth: 4, '4th': 4 }

// Rules are tried in order; the most specific phrase wins.
// KEEP IN STEP WITH pipeline/scout/seasons.py (the Python version; its tests are in pipeline/tests/test_seasons.py).
const RULES = [
  {
    re: new RegExp(`\\b${MONTH}\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?,?\\s+${YEAR}`, 'i'),
    toDate: (m) => date(toYear(m[3]), MONTH_INDEX[m[1].toLowerCase()], Number(m[2])),
    rule: 'An exact date is given, so that is the deadline.',
  },
  {
    re: /\b(\d{1,2})\/(\d{1,2})\/((?:19|20)\d{2})\b/,
    toDate: (m) => date(Number(m[3]), Number(m[1]) - 1, Number(m[2])),
    rule: 'An exact date is given, so that is the deadline.',
  },
  {
    re: new RegExp(`\\b${MONTH}\\.?,?\\s+(?:of\\s+)?${YEAR}`, 'i'),
    toDate: (m) => endOfMonth(toYear(m[2]), MONTH_INDEX[m[1].toLowerCase()]),
    rule: 'A month is given, so we allow until the last day of that month.',
  },
  {
    re: /\b(\d{1,2})\/((?:19|20)\d{2})\b/,
    toDate: (m) => endOfMonth(Number(m[2]), Number(m[1]) - 1),
    rule: 'A month is given, so we allow until the last day of that month.',
  },
  {
    re: new RegExp(`${YEAR}\\s*[-–/]\\s*(\\d{2,4})\\s+(?:school|academic)\\s+year`, 'i'),
    toDate: (m) => date(toYear(m[1]), 11, 31),
    rule: 'A school year is given. Students move in by late August (Aug 31 lease-up), so we allow until Dec 31 before calling it late.',
  },
  {
    re: new RegExp(`\\b(?:early|mid|late)?[-\\s]*(spring|summer|fall|autumn|winter)\\s+(?:of\\s+)?${YEAR}`, 'i'),
    toDate: (m) => {
      const y = toYear(m[2])
      const season = m[1].toLowerCase()
      if (season === 'spring') return date(y, 5, 20)
      if (season === 'summer') return date(y, 8, 21)
      if (season === 'winter') return date(y + 1, 2, 20) // Winter 2026 can run into March 2027
      return date(y, 11, 31) // fall / autumn: student move-in is usually August, so a Fall date can already be late
    },
    rule: 'A season is given, so we allow until the last day of that season. "Winter" runs into March of the next year.',
  },
  {
    re: new RegExp(`\\b(?:q([1-4])|([1-4](?:st|nd|rd|th)|first|second|third|fourth)\\s+quarter)\\s*(?:of\\s+)?,?\\s*${YEAR}`, 'i'),
    toDate: (m) => {
      const q = m[1] ? Number(m[1]) : ORDINAL[m[2].toLowerCase()]
      return endOfMonth(toYear(m[3]), q * 3 - 1)
    },
    rule: 'A quarter is given, so we allow until the last day of that quarter.',
  },
  {
    re: new RegExp(`\\b(?:h([12])|(first|second|1st|2nd)\\s+half)\\s*(?:of\\s+)?${YEAR}`, 'i'),
    toDate: (m) => {
      const half = m[1] ? Number(m[1]) : ORDINAL[m[2].toLowerCase()]
      return half === 1 ? date(toYear(m[3]), 5, 30) : date(toYear(m[3]), 11, 31)
    },
    rule: 'Half a year is given, so we allow until the end of that half.',
  },
  {
    re: new RegExp(`\\b(early|beginning\\s+of|mid|late|end\\s+of)[-\\s]+(?:of\\s+)?${YEAR}`, 'i'),
    toDate: (m) => {
      const y = toYear(m[2])
      const word = m[1].toLowerCase()
      if (word.startsWith('early') || word.startsWith('beginning')) return date(y, 3, 30)
      if (word === 'mid') return date(y, 7, 31)
      return date(y, 11, 31)
    },
    rule: '"Early" means by April 30, "mid" by Aug 31, "late" or "end of" by Dec 31.',
  },
  {
    re: new RegExp(`\\b(?:open(?:ing|s)?|coming|deliver(?:ing|s|y)?|complet(?:e|es|ion)|available|ready|move[-\\s]?ins?)\\b[^.!?\\d]{0,20}?${YEAR}`, 'i'),
    toDate: (m) => date(toYear(m[1]), 11, 31),
    rule: 'Only a year is given, so we allow until Dec 31 of that year.',
  },
]

const OPENING_WORDS = /\b(open(s|ing|ed)?|coming|deliver\w*|complet\w*|move[-\s]?ins?|available|ready|pre-?leasing|leasing for|now leasing|occupancy|debut\w*)\b/i

function sentencesOf(text) {
  return text
    .split(/\n+|(?<=[!?])\s+|(?<=[a-z0-9)]\.)\s+(?=[A-Z"“])/)
    .map((s) => s.trim())
    .filter(Boolean)
}

/** Finds the opening-date phrase in a block of website text and converts it to a deadline. */
export function findOpeningDate(text) {
  if (!text || !text.trim()) return null
  const sentences = sentencesOf(text)
  const ordered = [
    ...sentences.filter((s) => OPENING_WORDS.test(s)),
    ...sentences.filter((s) => !OPENING_WORDS.test(s)),
  ]
  for (const sentence of ordered) {
    let best = null
    for (const r of RULES) {
      const m = sentence.match(r.re)
      if (m) {
        best = { r, m }
        break
      }
    }
    if (best) {
      const deadline = best.r.toDate(best.m)
      if (Number.isNaN(deadline.getTime())) continue
      const phrase = best.m[0].replace(/^[-\s]+/, '').trim()
      const start = sentence.indexOf(phrase)
      return { phrase, sentence, start, deadline, rule: best.r.rule }
    }
  }
  return null
}

const OPEN_NOW = /\b(now open(?!\s+for\s+(pre-?)?leas)|open now|we['’]re open|officially opened|grand opening (was|celebrated)|move in today|now welcoming (residents|guests))\b/i
const PRE_OPENING = /\b(coming soon|pre-?leasing|now leasing for|leasing for|under construction|reserve your|join (the|our) (wait ?list|interest list)|opening|coming)\b/i

/** Does the page still look like the building hasn't opened? */
export function readSiteStatus(text) {
  if (!text) return { status: 'unclear', cue: null }
  const open = text.match(OPEN_NOW)
  if (open) return { status: 'open', cue: open[0] }
  const pre = text.match(PRE_OPENING)
  if (pre) return { status: 'pre-opening', cue: pre[0] }
  return { status: 'unclear', cue: null }
}

const DAY = 24 * 60 * 60 * 1000
const daysBetween = (a, b) => Math.round((startOfDay(b) - startOfDay(a)) / DAY)
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate())

/** Wayback history: how many times has the promised date moved later? */
export function readHistory(snapshots, current) {
  const parsed = (snapshots || [])
    .filter((s) => s.date && s.text)
    .map((s) => ({ ...s, seen: new Date(`${s.date}T12:00:00`), found: findOpeningDate(s.text) }))
    .filter((s) => s.found && !Number.isNaN(s.seen.getTime()))
    .sort((a, b) => a.seen - b.seen)

  const timeline = parsed.map((s) => ({ seen: s.seen, phrase: s.found.phrase, deadline: s.found.deadline }))
  if (current) timeline.push({ seen: null, phrase: current.phrase, deadline: current.deadline, isCurrent: true })

  let slips = 0
  for (let i = 1; i < timeline.length; i++) {
    if (timeline[i].deadline > timeline[i - 1].deadline) {
      slips++
      timeline[i].slipped = true
    }
  }
  const firstPromise = timeline[0]?.deadline ?? null
  const totalSlipDays = firstPromise && current ? Math.max(0, daysBetween(firstPromise, current.deadline)) : 0
  return { timeline, slips, totalSlipDays }
}

const MOVE_IN = { month: 7, day: 15 } // Aug 15

/** For student housing: which August move-in was missed and when rent realistically starts. */
export function studentHousingImpact(deadline, today) {
  const earliestOpen = deadline > today ? deadline : today
  const y = earliestOpen.getFullYear()
  const thisMoveIn = date(y, MOVE_IN.month, MOVE_IN.day)
  const missed = earliestOpen > thisMoveIn ? thisMoveIn : date(y - 1, MOVE_IN.month, MOVE_IN.day)
  const nextMoveIn = earliestOpen > thisMoveIn ? date(y + 1, MOVE_IN.month, MOVE_IN.day) : thisMoveIn
  const monthsUntil = Math.max(0, Math.round(daysBetween(today, nextMoveIn) / 30.4))
  const missedPromised = deadline > missed // the promised date itself was after a move-in that had already been planned around
  return { missed, nextMoveIn, monthsUntil, missedPromised }
}

/**
 * Full analysis for one project.
 * project: { pageText, propertyType, coStatus: 'issued'|'none'|'unknown', snapshots: [{date,text}] }
 */
export function analyzeProject(project, today = new Date()) {
  const found = findOpeningDate(project.pageText)
  const site = readSiteStatus(project.pageText)
  const history = readHistory(project.snapshots, found)
  const student = project.propertyType === 'Student housing'
  const result = { found, site, history, student: null, verdict: 'unreadable', score: 0, reasons: [] }

  if (!found) {
    result.reasons.push('No opening date found on the page. A researcher should read it.')
    return result
  }

  const daysLate = daysBetween(found.deadline, today)
  result.daysLate = daysLate
  if (student) result.student = studentHousingImpact(found.deadline, today)

  if (site.status === 'open') {
    result.verdict = 'open'
    result.reasons.push(`The page says "${site.cue}", so the building appears to be open.`)
    return result
  }
  if (daysLate <= 0) {
    result.verdict = daysLate > -60 ? 'watch' : 'on-time'
    result.reasons.push(`Not late yet: ${-daysLate} days until the latest reasonable opening date.`)
    if (history.slips) result.reasons.push(`But the date has already slipped ${history.slips} time${history.slips > 1 ? 's' : ''}.`)
    if (history.slips && result.verdict === 'on-time') result.verdict = 'watch'
    return result
  }
  if (project.coStatus === 'issued') {
    result.verdict = 'co-issued'
    result.reasons.push('The date has passed, but the city issued a certificate of occupancy. The website is probably just out of date.')
    return result
  }

  // It's a flag.
  result.verdict = 'flag'
  let score = 35
  score += Math.min(35, daysLate / 5)
  score += Math.min(20, history.slips * 10)
  result.reasons.push(`The latest reasonable opening date passed ${daysLate} days ago.`)
  if (site.status === 'pre-opening') result.reasons.push(`The page still says "${site.cue}".`)
  if (history.slips) {
    result.reasons.push(`The opening date has slipped ${history.slips} time${history.slips > 1 ? 's' : ''} (${history.totalSlipDays} days later than first promised).`)
  }
  if (project.coStatus === 'none') {
    result.reasons.push('No certificate of occupancy on file with the city.')
  } else {
    score -= 5
    result.reasons.push('Certificate of occupancy not checked yet. Check city records before calling.')
  }
  if (student && result.student) {
    score += 10
    result.reasons.push(
      `Student housing: missed the ${fmtMonthYear(result.student.missed)} move-in. Likely little rent until ${fmtMonthYear(result.student.nextMoveIn)} (about ${result.student.monthsUntil} months).`,
    )
  }
  result.score = Math.round(Math.max(0, Math.min(100, score)))
  return result
}

export const fmtDate = (d) => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
export const fmtMonthYear = (d) => d.toLocaleDateString('en-US', { month: 'short', year: 'numeric' })
export const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
