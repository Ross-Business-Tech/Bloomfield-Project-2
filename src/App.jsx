import { useEffect, useMemo, useRef, useState } from 'react'
import { analyzeProject, fmtDate, isoDate } from './lib/openingDate.js'
import { PROPERTY_TYPES, SAMPLE_PROJECTS } from './data/sampleProjects.js'
import './App.css'

const STORE_KEY = 'stale-opening-scout-v1'

const VERDICTS = {
  flag: { label: 'Flag', tone: 'flag', blurb: 'Late and still not open' },
  watch: { label: 'Watch', tone: 'watch', blurb: 'Not late yet, but close or already slipping' },
  'co-issued': { label: 'Probably fine', tone: 'muted', blurb: 'City says it is finished' },
  open: { label: 'Open', tone: 'muted', blurb: 'Website says it is open' },
  'on-time': { label: 'On time', tone: 'muted', blurb: 'Deadline still far off' },
  unreadable: { label: 'Needs a look', tone: 'watch', blurb: 'No date found' },
}
const VERDICT_ORDER = ['flag', 'watch', 'unreadable', 'on-time', 'co-issued', 'open']

const DECISIONS = [
  { key: 'approved', label: 'Add to weekly list' },
  { key: 'digging', label: 'Needs more digging' },
  { key: 'dismissed', label: 'Dismiss' },
]

function loadSaved() {
  try {
    const raw = window.localStorage.getItem(STORE_KEY)
    if (raw) return JSON.parse(raw)
  } catch {
    /* storage unavailable: start fresh */
  }
  return null
}

export default function App() {
  const [saved] = useState(loadSaved)
  const [projects, setProjects] = useState(saved?.projects ?? SAMPLE_PROJECTS)
  const [decisions, setDecisions] = useState(saved?.decisions ?? {})
  const [selectedId, setSelectedId] = useState(saved?.projects?.[0]?.id ?? SAMPLE_PROJECTS[0].id)
  const [showOffice, setShowOffice] = useState(false)
  const detailRef = useRef(null)
  const [today] = useState(() => new Date())

  useEffect(() => {
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify({ projects, decisions }))
    } catch {
      /* ignore */
    }
  }, [projects, decisions])

  const analyzed = useMemo(
    () => projects.map((p) => ({ project: p, result: analyzeProject(p, today) })),
    [projects, today],
  )

  const ranked = useMemo(
    () =>
      analyzed
        .filter(({ project }) => showOffice || project.propertyType !== 'Office')
        .sort((a, b) => {
          const v = VERDICT_ORDER.indexOf(a.result.verdict) - VERDICT_ORDER.indexOf(b.result.verdict)
          if (v) return v
          return b.result.score - a.result.score || (b.result.daysLate ?? 0) - (a.result.daysLate ?? 0)
        }),
    [analyzed, showOffice],
  )

  const hiddenOffice = analyzed.filter((a) => a.project.propertyType === 'Office').length
  const flagCount = ranked.filter((r) => r.result.verdict === 'flag').length
  const watchCount = ranked.filter((r) => r.result.verdict === 'watch').length
  const selected = analyzed.find((a) => a.project.id === selectedId) ?? ranked[0]

  function select(id) {
    setSelectedId(id)
    if (window.matchMedia('(max-width: 860px)').matches) {
      requestAnimationFrame(() => detailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
    }
  }

  function updateProject(id, patch) {
    setProjects((ps) => ps.map((p) => (p.id === id ? { ...p, ...patch } : p)))
  }

  function addProject() {
    const id = `new-${Date.now()}`
    setProjects((ps) => [
      {
        id,
        name: 'New project',
        city: '',
        propertyType: 'Multifamily',
        developer: '',
        sourceUrl: '',
        coStatus: 'unknown',
        pageText: '',
        snapshots: [],
        isNew: true,
      },
      ...ps,
    ])
    select(id)
  }

  function removeProject(id) {
    setProjects((ps) => ps.filter((p) => p.id !== id))
    setSelectedId(null)
  }

  function resetDemo() {
    setProjects(SAMPLE_PROJECTS)
    setDecisions({})
    setSelectedId(SAMPLE_PROJECTS[0].id)
  }

  return (
    <div className="page">
      <header className="top">
        <div className="brand">
          <span className="logo" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="22" height="22">
              <path d="M4 21V8l8-5 8 5v13h-5v-6H9v6z" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
              <circle cx="18" cy="6" r="3.5" fill="var(--flag)" stroke="var(--bg)" strokeWidth="1.5" />
            </svg>
          </span>
          <div>
            <h1>Stale Opening Scout</h1>
            <p className="tag">Finds projects whose opening date has passed while the website still says “coming soon.”</p>
          </div>
        </div>
        <p className="today">Week of {fmtDate(today)}</p>
      </header>

      <section className="summary" aria-label="This week">
        <div className="stat">
          <strong className="flag-text">{flagCount}</strong>
          <span>flags</span>
        </div>
        <div className="stat">
          <strong className="watch-text">{watchCount}</strong>
          <span>to watch</span>
        </div>
        <div className="stat">
          <strong>{ranked.length}</strong>
          <span>projects tracked</span>
        </div>
        <p className="how">
          For each project we read the opening date off its website, allow until the <em>latest</em> reasonable
          day that phrase could mean, then check old copies of the page for slips and city records for a
          certificate of occupancy.
        </p>
      </section>

      <main className="layout">
        <section className="list" aria-label="Ranked projects">
          <div className="list-head">
            <h2>Ranked projects</h2>
            <button className="btn small" onClick={addProject}>+ Check a project</button>
          </div>
          <label className="toggle">
            <input type="checkbox" checked={showOffice} onChange={(e) => setShowOffice(e.target.checked)} />
            Include office {hiddenOffice > 0 && !showOffice && <span className="muted">({hiddenOffice} hidden)</span>}
          </label>
          <ul>
            {ranked.map(({ project, result }) => (
              <li key={project.id}>
                <button
                  className={`card ${selected?.project.id === project.id ? 'active' : ''}`}
                  onClick={() => select(project.id)}
                >
                  <span className={`pill ${VERDICTS[result.verdict].tone}`}>{VERDICTS[result.verdict].label}</span>
                  <span className="card-main">
                    <span className="card-name">{project.name || 'Untitled project'}</span>
                    <span className="card-meta">
                      {project.propertyType}
                      {project.city ? ` · ${project.city}` : ''}
                    </span>
                    <span className="card-phrase">
                      {result.found ? `“${result.found.phrase}”` : 'No date found'}
                      {result.verdict === 'flag' && ` · ${result.daysLate} days late`}
                      {result.history.slips > 0 && ` · slipped ${result.history.slips}×`}
                    </span>
                  </span>
                  <span className="card-side">
                    {result.verdict === 'flag' && <span className="score">{result.score}</span>}
                    {decisions[project.id] && (
                      <span className={`decision ${decisions[project.id]}`}>
                        {DECISIONS.find((d) => d.key === decisions[project.id])?.label}
                      </span>
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <p className="fine">
            All projects are made up. Edits stay in this browser.{' '}
            <button className="link" onClick={resetDemo}>Reset sample data</button>
          </p>
        </section>

        <section className="detail" ref={detailRef} aria-label="Project detail">
          {selected ? (
            <Detail
              key={selected.project.id}
              project={selected.project}
              result={selected.result}
              decision={decisions[selected.project.id]}
              onDecide={(key) =>
                setDecisions((d) => ({ ...d, [selected.project.id]: d[selected.project.id] === key ? undefined : key }))
              }
              onChange={(patch) => updateProject(selected.project.id, patch)}
              onRemove={() => removeProject(selected.project.id)}
            />
          ) : (
            <p className="muted">Pick a project to see the evidence.</p>
          )}
        </section>
      </main>

      <RuleTable />
    </div>
  )
}

function Detail({ project, result, decision, onDecide, onChange, onRemove }) {
  const v = VERDICTS[result.verdict]
  const { found, history } = result
  return (
    <article>
      <div className="detail-head">
        <div>
          <h2>{project.name || 'Untitled project'}</h2>
          <p className="muted">
            {project.propertyType}
            {project.city ? ` · ${project.city}` : ''}
            {project.developer ? ` · ${project.developer}` : ''}
          </p>
        </div>
        <span className={`pill big ${v.tone}`}>{v.label}</span>
      </div>

      {result.verdict === 'flag' && (
        <div className="meter" aria-label={`Stale-opening strength ${result.score} out of 100`}>
          <div className="meter-label">
            <span>Stale-opening strength</span>
            <strong>{result.score}/100</strong>
          </div>
          <div className="meter-track">
            <div className="meter-fill" style={{ width: `${result.score}%` }} />
          </div>
        </div>
      )}

      <ul className="reasons">
        {result.reasons.map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>

      {found && (
        <div className="evidence">
          <h3>Evidence</h3>
          <blockquote>
            {found.sentence.slice(0, found.start)}
            <mark>{found.sentence.slice(found.start, found.start + found.phrase.length)}</mark>
            {found.sentence.slice(found.start + found.phrase.length)}
            {project.sourceUrl && <cite>{project.sourceUrl}</cite>}
          </blockquote>
          <dl className="facts">
            <div>
              <dt>Phrase read</dt>
              <dd>“{found.phrase}”</dd>
            </div>
            <div>
              <dt>Latest reasonable opening</dt>
              <dd>{fmtDate(found.deadline)}</dd>
            </div>
            <div>
              <dt>Site status</dt>
              <dd>{result.site.status === 'pre-opening' ? 'Still pre-opening' : result.site.status === 'open' ? 'Says open' : 'Unclear'}</dd>
            </div>
            <div>
              <dt>Certificate of occupancy</dt>
              <dd>{{ issued: 'Issued', none: 'None on file', unknown: 'Not checked' }[project.coStatus]}</dd>
            </div>
          </dl>
          <p className="rule">Rule used: {found.rule}</p>
        </div>
      )}

      {history.timeline.length > 1 && (
        <div className="timeline">
          <h3>How the promised date moved</h3>
          <ol>
            {history.timeline.map((t, i) => (
              <li key={i} className={t.slipped ? 'slipped' : ''}>
                <span className="when">{t.isCurrent ? 'Today' : fmtDate(t.seen)}</span>
                <span className="what">
                  “{t.phrase}” → by {fmtDate(t.deadline)}
                  {t.slipped && <span className="slip-tag">slipped</span>}
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}

      <div className="decide">
        <h3>Researcher decision</h3>
        <div className="decide-row">
          {DECISIONS.map((d) => (
            <button
              key={d.key}
              className={`btn ${decision === d.key ? `chosen ${d.key}` : 'ghost'}`}
              aria-pressed={decision === d.key}
              onClick={() => onDecide(d.key)}
            >
              {d.label}
            </button>
          ))}
        </div>
      </div>

      <details className="edit" open={project.isNew}>
        <summary>Edit what the scout read</summary>
        <Editor project={project} onChange={onChange} />
        <button className="link danger" onClick={onRemove}>Remove this project</button>
      </details>

      <details className="row">
        <summary>Row saved to the <code>projects</code> table</summary>
        <pre>{JSON.stringify(toProjectsRow(project, result), null, 2)}</pre>
      </details>
    </article>
  )
}

function Editor({ project, onChange }) {
  const snaps = project.snapshots ?? []
  const setSnap = (i, patch) => onChange({ snapshots: snaps.map((s, j) => (j === i ? { ...s, ...patch } : s)) })
  return (
    <div className="editor">
      <label>
        Project name
        <input value={project.name} onChange={(e) => onChange({ name: e.target.value })} />
      </label>
      <div className="two">
        <label>
          City, state
          <input value={project.city} placeholder="Ann Arbor, MI" onChange={(e) => onChange({ city: e.target.value })} />
        </label>
        <label>
          Property type
          <select value={project.propertyType} onChange={(e) => onChange({ propertyType: e.target.value })}>
            {PROPERTY_TYPES.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </label>
      </div>
      <label>
        Text on the project’s website today
        <textarea
          rows={4}
          value={project.pageText}
          placeholder="Paste the page text, e.g. “Coming soon! Opening Winter 2026. Now pre-leasing.”"
          onChange={(e) => onChange({ pageText: e.target.value })}
        />
      </label>
      <label>
        Certificate of occupancy (city records)
        <select value={project.coStatus} onChange={(e) => onChange({ coStatus: e.target.value })}>
          <option value="unknown">Not checked yet</option>
          <option value="none">Checked: none on file</option>
          <option value="issued">Checked: issued</option>
        </select>
      </label>
      <fieldset>
        <legend>Older copies of the page (Wayback Machine)</legend>
        {snaps.map((s, i) => (
          <div className="snap" key={i}>
            <input
              type="date"
              value={s.date}
              aria-label="Snapshot date"
              onChange={(e) => setSnap(i, { date: e.target.value })}
            />
            <input
              value={s.text}
              placeholder="Opening Fall 2025"
              aria-label="What the page said"
              onChange={(e) => setSnap(i, { text: e.target.value })}
            />
            <button
              className="icon"
              aria-label="Remove snapshot"
              onClick={() => onChange({ snapshots: snaps.filter((_, j) => j !== i) })}
            >
              ×
            </button>
          </div>
        ))}
        <button
          className="btn small ghost"
          onClick={() => onChange({ snapshots: [...snaps, { date: isoDate(new Date(Date.now() - 365 * 864e5)), text: '' }] })}
        >
          + Add older copy
        </button>
      </fieldset>
    </div>
  )
}

function toProjectsRow(project, result) {
  return {
    project_id: project.id,
    project_name: project.name,
    city_state: project.city,
    property_type: project.propertyType,
    developer: project.developer || null,
    source_url: project.sourceUrl || null,
    opening_phrase: result.found?.phrase ?? null,
    opening_deadline: result.found ? isoDate(result.found.deadline) : null,
    source_sentence: result.found?.sentence ?? null,
    site_status: result.site.status,
    co_status: project.coStatus,
    date_slips: result.history.slips,
    days_late: result.daysLate > 0 ? result.daysLate : 0,
    stale_flag: result.verdict === 'flag',
    stale_strength: result.score,
  }
}

const RULE_ROWS = [
  ['“August 15, 2026”', 'That exact day'],
  ['“August 2026”', 'Aug 31, 2026'],
  ['“Q3 2026”', 'Sep 30, 2026'],
  ['“Spring / Summer / Fall 2026”', 'Jun 20 / Sep 22 / Dec 20, 2026'],
  ['“Winter 2026”', 'Mar 20, 2027'],
  ['“Early / mid / late 2026”', 'Apr 30 / Aug 31 / Dec 31, 2026'],
  ['“2026-27 school year”', 'Sep 30, 2026'],
  ['“Opening 2026”', 'Dec 31, 2026'],
]

function RuleTable() {
  return (
    <section className="rules">
      <h2>How a phrase becomes a deadline</h2>
      <p className="muted">
        We always use the latest date the phrase could reasonably mean, so a project is only flagged once it is
        truly late. Student housing gets extra weight: missing the August move-in usually means a year without rent.
      </p>
      <table>
        <thead>
          <tr>
            <th>Website says</th>
            <th>Late after</th>
          </tr>
        </thead>
        <tbody>
          {RULE_ROWS.map(([a, b]) => (
            <tr key={a}>
              <td>{a}</td>
              <td>{b}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
