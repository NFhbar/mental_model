import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'

const INLINE_CITATION_RE = /\[(e\d+)\]|\[commit:([0-9a-fA-F]{4,40})\]/g
const STORAGE_KEY = 'mm_state'
const UI_STORAGE_KEY = 'mm_ui'
const MAX_INVESTIGATIONS = 50
const MAX_MESSAGES = 100
const MAX_STORED_RESULT_CHARS = 1200
const EMPTY_LIST = []

function loadWorkspaceState() {
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}
    if (Array.isArray(stored.repositories)) {
      const repositories = stored.repositories.slice(0, 3).map((repository) => ({
        ...repository,
        connected: repository.connected ?? true,
      }))
      return {
        repositories,
        activeRepositoryId: repositories.some(
          (repository) => repository.id === stored.activeRepositoryId,
        )
          ? stored.activeRepositoryId
          : repositories[0]?.id || null,
      }
    }
    if (stored.session) {
      const id = stored.session.session_id || 'migrated-repository'
      return {
        repositories: [
          {
            id,
            source: stored.session.source || stored.session.root,
            session: stored.session,
            messages: stored.messages || [],
            trace: stored.trace || [],
            connected: true,
            contextReset: false,
          },
        ],
        activeRepositoryId: id,
      }
    }
    return { repositories: [], activeRepositoryId: null }
  } catch {
    return { repositories: [], activeRepositoryId: null }
  }
}

function loadUiPreferences() {
  try {
    return JSON.parse(localStorage.getItem(UI_STORAGE_KEY)) || {}
  } catch {
    return {}
  }
}

function groupTrace(trace) {
  const groups = []
  let current = null
  for (const event of trace) {
    if (event.kind === 'question') {
      current = {
        id: event.id || `legacy-${groups.length}`,
        question: event.text,
        startedAt: event.startedAt,
        events: [],
      }
      groups.push(current)
    } else if (current) {
      current.events.push(event)
    }
  }
  return groups
}

function flattenGroups(groups) {
  return groups.flatMap((group) => [
    {
      kind: 'question',
      id: group.id,
      text: group.question,
      startedAt: group.startedAt,
    },
    ...group.events,
  ])
}

function limitTrace(trace) {
  return flattenGroups(groupTrace(trace).slice(-MAX_INVESTIGATIONS))
}

function compactTrace(trace, investigationLimit = MAX_INVESTIGATIONS) {
  const groups = groupTrace(trace).slice(-investigationLimit)
  return flattenGroups(
    groups.map((group) => ({
      ...group,
      events: group.events.map((event) => {
        if (
          event.kind !== 'tool_result'
          || typeof event.result !== 'string'
          || event.result.length <= MAX_STORED_RESULT_CHARS
        ) {
          return event
        }
        return {
          ...event,
          result: `${event.result.slice(0, MAX_STORED_RESULT_CHARS)}\n... [stored preview truncated]`,
        }
      }),
    })),
  )
}

function saveWorkspaceState(workspaceState) {
  const state = {
    version: 2,
    activeRepositoryId: workspaceState.activeRepositoryId,
    repositories: workspaceState.repositories.map((repository) => ({
      ...repository,
      messages: repository.messages.slice(-MAX_MESSAGES),
      trace: compactTrace(repository.trace),
    })),
  }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          ...state,
          repositories: state.repositories.map((repository) => ({
            ...repository,
            messages: repository.messages.slice(-20),
            trace: compactTrace(repository.trace, 10),
          })),
        }),
      )
    } catch {
      localStorage.removeItem(STORAGE_KEY)
    }
  }
}

function formatDuration(durationMs) {
  if (!Number.isFinite(durationMs)) return null
  if (durationMs < 1000) return `${durationMs}ms`
  return `${(durationMs / 1000).toFixed(1)}s`
}

function normalizeRepositorySource(source) {
  return source.trim().replace(/\.git\/?$/, '').replace(/\/$/, '').toLowerCase()
}

async function streamAsk(sessionId, question, token, signal, onEvent) {
  const res = await fetch('/api/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': token },
    body: JSON.stringify({ session_id: sessionId, question }),
    signal,
  })
  if (!res.ok) throw new Error((await res.json()).detail || res.statusText)
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const blocks = buffer.split(/\r\n\r\n|\n\n/)
    buffer = blocks.pop()
    for (const block of blocks) {
      const dataLine = block.split(/\r?\n/).find((l) => l.startsWith('data:'))
      if (!dataLine) continue
      const payload = JSON.parse(dataLine.slice(5))
      if (payload.kind) onEvent(payload)
    }
  }
}

function LegacyCitationChip({ sha, sessionId, token }) {
  const [detail, setDetail] = useState(null)
  const [open, setOpen] = useState(false)

  const toggle = async () => {
    if (!open && detail === null) {
      try {
        const res = await fetch(`/api/commit/${sessionId}/${sha}`, {
          headers: { 'X-Auth-Token': token },
        })
        const body = await res.json()
        setDetail(res.ok ? body.detail : body.detail || 'failed to load commit')
      } catch (err) {
        setDetail(String(err))
      }
    }
    setOpen(!open)
  }

  return (
    <span className="citation">
      <button className="citation-chip" onClick={toggle}>
        {sha.slice(0, 7)}
      </button>
      {open && detail !== null && <pre className="citation-detail">{detail}</pre>}
    </span>
  )
}

function evidenceUrl(item, webUrl) {
  if (!webUrl) return null
  if (item.source_type === 'commit') return `${webUrl}/commit/${item.source_id}`
  if (item.source_type === 'pr') return `${webUrl}/pull/${item.source_id}`
  if (item.source_type === 'file') {
    const separator = item.source_id.lastIndexOf('@')
    if (separator < 1) return null
    const path = item.source_id.slice(0, separator)
    const ref = item.source_id.slice(separator + 1)
    return `${webUrl}/blob/${ref}/${path}`
  }
  return null
}

function EvidenceChip({ item, webUrl }) {
  const [open, setOpen] = useState(false)
  const url = evidenceUrl(item, webUrl)

  return (
    <span className="citation">
      <button
        className={`citation-chip citation-${item?.kind || 'unverified'}`}
        onClick={() => setOpen(!open)}
        title={item ? `${item.kind} evidence from ${item.source_type}:${item.source_id}` : 'missing evidence'}
      >
        {item?.id || '?'}
      </button>
      {open && item && (
        <span className="evidence-detail">
          <span className={`evidence-kind evidence-${item.kind}`}>{item.kind}</span>
          <strong>{item.claim}</strong>
          <q>{item.quote}</q>
          {url ? (
            <a href={url} target="_blank" rel="noreferrer">
              {item.source_type}:{item.source_id}
            </a>
          ) : (
            <span className="evidence-source">{item.source_type}:{item.source_id}</span>
          )}
        </span>
      )}
    </span>
  )
}

function AnswerText({ text, session, token, evidence = [] }) {
  const parts = []
  const evidenceById = Object.fromEntries(evidence.map((item) => [item.id, item]))
  let last = 0
  let match
  INLINE_CITATION_RE.lastIndex = 0
  while ((match = INLINE_CITATION_RE.exec(text)) !== null) {
    parts.push(text.slice(last, match.index))
    if (match[1]) {
      parts.push(
        <EvidenceChip
          key={match.index}
          item={evidenceById[match[1]]}
          webUrl={session.web_url}
        />,
      )
    } else {
      parts.push(
        <LegacyCitationChip
          key={match.index}
          sha={match[2]}
          sessionId={session.session_id}
          token={token}
        />,
      )
    }
    last = match.index + match[0].length
  }
  parts.push(text.slice(last))
  return <div className="answer-text">{parts}</div>
}

function VerificationReport({ report = [], verified, onInteract = null }) {
  return (
    <div className={`verification-report ${verified ? 'verification-report-passed' : 'verification-report-failed'}`}>
      <div className="verification-report-header">
        <span>{verified ? '✓' : '!'}</span>
        <strong>{verified ? 'Evidence verification passed' : 'Evidence verification failed'}</strong>
        <span>{report.filter((item) => item.verified).length}/{report.length} claims</span>
      </div>
      <div className="verification-claims">
        {report.map((item) => (
          <details
            key={item.id}
            className="verification-claim"
            onToggle={(event) => event.currentTarget.open && onInteract?.()}
          >
            <summary>
              <code>{item.id}</code>
              <span className={`evidence-kind evidence-${item.kind}`}>{item.kind}</span>
              <span className="verification-source">{item.source_type}:{item.source_id}</span>
              <span className={item.verified ? 'verification-check-ok' : 'verification-check-bad'}>
                {item.verified ? '✓' : '×'}
              </span>
            </summary>
            <p>{item.claim}</p>
            {item.quote && <q className="verification-quote">{item.quote}</q>}
            <div className="verification-checks">
              {item.checks.map((check) => (
                <span key={check.name} className={check.ok ? 'verification-check-ok' : 'verification-check-bad'}>
                  {check.ok ? '✓' : '×'} {check.name}
                </span>
              ))}
            </div>
          </details>
        ))}
      </div>
    </div>
  )
}

function TraceItem({ event, onInteract = null }) {
  const [open, setOpen] = useState(false)
  if (event.kind === 'question') {
    return <div className="trace-question">{event.text}</div>
  }
  if (event.kind === 'thought') {
    return <div className="trace-thought">{event.text}</div>
  }
  if (event.kind === 'tool_call') {
    return (
      <div className="trace-call">
        <span className="trace-tool-name">{event.name}</span>
        <code>{JSON.stringify(event.args)}</code>
      </div>
    )
  }
  if (event.kind === 'tool_result') {
    return (
      <div className="trace-result">
        <button
          className="trace-toggle"
          onClick={() => {
            setOpen(!open)
            onInteract?.()
          }}
        >
          {open ? '▾' : '▸'} result ({event.result.length} chars)
        </button>
        {open && <pre>{event.result}</pre>}
      </div>
    )
  }
  if (event.kind === 'verification_failed') {
    return (
      <div className="trace-verification">
        <VerificationReport report={event.report} verified={false} onInteract={onInteract} />
        <strong>Retrying because:</strong>
        <ul>
          {event.problems.map((p, i) => (
            <li key={i}>{p}</li>
          ))}
        </ul>
      </div>
    )
  }
  if (event.kind === 'verification_report') {
    return (
      <VerificationReport
        report={event.report}
        verified={event.verified}
        onInteract={onInteract}
      />
    )
  }
  return null
}

function InvestigationGroup({ group, open, onToggle, onInteract }) {
  const toolCalls = group.events.filter((event) => event.kind === 'tool_call').length
  const retries = group.events.filter((event) => event.kind === 'verification_failed').length
  const completion = group.events.findLast((event) => event.kind === 'complete')
  const investigation = completion?.investigation
  const evidenceCount = investigation
    ? investigation.evidence.direct + investigation.evidence.inferred
    : null

  return (
    <section className="trace-group">
      <button className="trace-group-summary" onClick={onToggle} aria-expanded={open}>
        <span className="trace-group-arrow">{open ? '▾' : '▸'}</span>
        <span className="trace-group-question">{group.question}</span>
        <span className="trace-group-stats">
          {investigation?.question_type && <span>{investigation.question_type}</span>}
          <span>{toolCalls} tools</span>
          {retries > 0 && <span>{retries} retries</span>}
          {evidenceCount !== null && <span>{evidenceCount} evidence</span>}
          {investigation?.duration_ms && <span>{formatDuration(investigation.duration_ms)}</span>}
          {completion?.cancelled && <span>stopped</span>}
        </span>
      </button>
      {open && (
        <div className="trace-group-events">
          {group.events
            .filter((event) => event.kind !== 'complete')
            .map((event, index) => (
              <TraceItem key={index} event={event} onInteract={onInteract} />
            ))}
          <div className="trace-group-end">End of investigation</div>
        </div>
      )}
    </section>
  )
}

function MetaPanel({
  session,
  token,
  refreshKey,
  onAuthExpired,
  onSessionExpired,
  repositories,
  activeRepositoryId,
  maxRepositories,
}) {
  const [meta, setMeta] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    setError(null)
    fetch(`/api/meta/${session.session_id}`, {
      headers: { 'X-Auth-Token': token },
    })
      .then(async (res) => {
        const body = await res.json()
        if (res.status === 401) {
          onAuthExpired()
          throw new Error('authentication expired')
        }
        if (res.status === 404) {
          onSessionExpired()
          throw new Error('repository session expired')
        }
        if (!res.ok) throw new Error(body.detail || res.statusText)
        return body
      })
      .then(setMeta)
      .catch((err) => setError(String(err.message || err)))
  }, [session.session_id, token, refreshKey, onAuthExpired, onSessionExpired])

  if (error) return <div className="meta-page"><div className="error">{error}</div></div>
  if (!meta) return <div className="meta-page"><div className="hint">loading agent metadata…</div></div>

  const runtime = meta.runtime
  const last = meta.last_investigation

  return (
    <main className="meta-page">
      <section className="meta-hero">
        <div>
          <span className="meta-eyebrow">Agent introspection</span>
          <h1>Mental Model capabilities</h1>
          <p>The live context, investigation policy, tools, and verification boundary.</p>
        </div>
        <span className="meta-model">{meta.model}</span>
      </section>

      <section className="meta-card">
        <div className="meta-section-heading">
          <h2>Repository workspaces</h2>
          <span>{repositories.length}/{maxRepositories} open</span>
        </div>
        <div className="meta-workspace-grid">
          {repositories.map((repository) => {
            const active = repository.id === activeRepositoryId
            const name = repository.session?.name || repository.source
            return (
              <article
                key={repository.id}
                className={active ? 'meta-workspace-active' : ''}
              >
                <div>
                  <span className={`repository-status repository-status-${
                    repository.connected
                      ? repository.contextReset
                        ? 'reset'
                        : 'connected'
                      : 'disconnected'
                  }`} />
                  <strong>{name}</strong>
                  {active && <span className="repository-active-label">Active</span>}
                </div>
                <span>{repository.session?.summary.branch || 'not connected'}</span>
                <dl>
                  <div><dt>Messages</dt><dd>{repository.messages.length}</dd></div>
                  <div><dt>Investigations</dt><dd>{groupTrace(repository.trace).length}</dd></div>
                  <div><dt>Agent</dt><dd>{repository.connected ? 'connected' : 'reconnect required'}</dd></div>
                </dl>
              </article>
            )
          })}
        </div>
      </section>

      <section className="meta-stats">
        <div><strong>{runtime.questions}</strong><span>questions</span></div>
        <div><strong>{runtime.tool_calls}</strong><span>tool calls</span></div>
        <div><strong>{runtime.verification_retries}</strong><span>verification retries</span></div>
        <div><strong>{runtime.input_tokens + runtime.output_tokens}</strong><span>tokens</span></div>
      </section>

      {last && (
        <section className="meta-card">
          <h2>Latest investigation</h2>
          <div className="meta-last-grid">
            <span>Type<strong>{last.question_type || 'unclassified'}</strong></span>
            <span>Duration<strong>{formatDuration(last.duration_ms)}</strong></span>
            <span>Tools<strong>{last.tool_calls}</strong></span>
            <span>Evidence<strong>{last.evidence.direct} direct · {last.evidence.inferred} inferred</strong></span>
            <span>Retries<strong>{last.verification_retries}</strong></span>
            <span>Status<strong className={last.verified ? 'ok' : 'bad'}>{last.verified ? 'verified' : 'unverified'}</strong></span>
          </div>
        </section>
      )}

      {(meta.last_verification_report || []).length > 0 && (
        <section className="meta-card">
          <h2>Latest per-claim verification</h2>
          <VerificationReport
            report={meta.last_verification_report}
            verified={meta.last_verification_report.every((item) => item.verified)}
          />
        </section>
      )}

      <section className="meta-grid">
        <div className="meta-card">
          <h2>Repository context</h2>
          <pre>{meta.repository_context}</pre>
        </div>
        <div className="meta-card">
          <h2>Question routing</h2>
          <dl className="meta-definition-list">
            {Object.entries(meta.policy.question_types).map(([name, description]) => (
              <div key={name}><dt>{name}</dt><dd>{description}</dd></div>
            ))}
          </dl>
        </div>
      </section>

      <section className="meta-card">
        <h2>Capabilities</h2>
        <div className="capability-grid">
          {meta.capabilities.map((capability) => (
            <article key={capability.name} className={!capability.available ? 'capability-disabled' : ''}>
              <div>
                <code>{capability.name}</code>
                <span className={capability.available ? 'capability-ready' : 'capability-unavailable'}>
                  {capability.available ? 'ready' : 'unavailable'}
                </span>
              </div>
              <p>{capability.description}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="meta-grid">
        <div className="meta-card">
          <h2>Evidence hierarchy</h2>
          <ol>
            {meta.policy.evidence_hierarchy.map((item) => <li key={item}>{item}</li>)}
          </ol>
        </div>
        <div className="meta-card">
          <h2>Deterministic verification</h2>
          <ul>
            {meta.policy.verification.map((item) => <li key={item}>{item}</li>)}
          </ul>
          <p className="meta-boundary">
            Exact provenance is verified. Semantic entailment remains an explicit boundary.
          </p>
        </div>
      </section>
    </main>
  )
}

function DiagnosePanel({ onClose, token, onAuthExpired }) {
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetch('/api/diagnose', { headers: { 'X-Auth-Token': token } })
      .then(async (res) => {
        const body = await res.json()
        if (res.status === 401) {
          onAuthExpired()
          throw new Error('authentication expired')
        }
        if (!res.ok) throw new Error(body.detail || res.statusText)
        return body
      })
      .then(setReport)
      .catch((err) => setError(String(err)))
  }, [token, onAuthExpired])

  return (
    <div className="diagnose-overlay" onClick={onClose}>
      <div className="diagnose-panel" onClick={(e) => e.stopPropagation()}>
        <div className="diagnose-header">
          <span>API key diagnostics</span>
          <button onClick={onClose}>close</button>
        </div>
        {error && <div className="error">{error}</div>}
        {!report && !error && <div className="hint">running checks…</div>}
        {report && (
          <dl>
            <dt>API key</dt>
            <dd>
              {report.api_key_set ? `set (…${report.api_key_suffix})` : 'NOT SET'}
            </dd>
            <dt>Configured model</dt>
            <dd>{report.configured_model}</dd>
            <dt>Model probe</dt>
            <dd className={report.model_probe?.ok ? 'ok' : 'bad'}>
              {report.model_probe?.ok ? 'chat completion succeeded' : report.model_probe?.error}
            </dd>
            <dt>Models available to this key</dt>
            <dd>
              {report.available_models_error ||
                (report.available_models.length ? report.available_models.join(', ') : 'none')}
            </dd>
          </dl>
        )}
      </div>
    </div>
  )
}

function PasswordGate({ onAuth }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)

  const submit = async (e) => {
    e.preventDefault()
    setError(null)
    const res = await fetch('/api/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    })
    const body = await res.json()
    if (!res.ok) {
      setError(body.detail || 'authentication failed')
      return
    }
    onAuth(body.token)
  }

  return (
    <form className="repo-picker" onSubmit={submit}>
      <h1>Mental Model</h1>
      <p>Enter the password to continue.</p>
      <input
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="Password"
        autoFocus
      />
      <button disabled={!password}>Unlock</button>
      {error && <div className="error">{error}</div>}
    </form>
  )
}

function RepoPicker({ onOpen, busy, onCancel = null, compact = false }) {
  const [source, setSource] = useState('')
  const [error, setError] = useState(null)

  const submit = async (e) => {
    e.preventDefault()
    setError(null)
    try {
      await onOpen(source)
    } catch (err) {
      setError(String(err.message || err))
    }
  }

  return (
    <form className={`repo-picker ${compact ? 'repo-picker-compact' : ''}`} onSubmit={submit}>
      <h1>Mental Model</h1>
      <p>Point me at a repository and ask me anything about it.</p>
      <input
        value={source}
        onChange={(e) => setSource(e.target.value)}
        placeholder="Local path or GitHub URL (e.g. https://github.com/pallets/click)"
        autoFocus
      />
      <div className="repo-picker-actions">
        {onCancel && <button type="button" className="secondary-button" onClick={onCancel}>Cancel</button>}
        <button disabled={busy || !source.trim()}>{busy ? 'Opening…' : 'Open repository'}</button>
      </div>
      {error && <div className="error">{error}</div>}
    </form>
  )
}

function RepositoryDialog({ onOpen, busy, onClose }) {
  return (
    <div className="diagnose-overlay" onClick={onClose}>
      <div className="repository-dialog" onClick={(event) => event.stopPropagation()}>
        <RepoPicker onOpen={onOpen} busy={busy} onCancel={onClose} compact />
      </div>
    </div>
  )
}

function RepositoryTabs({
  repositories,
  activeRepositoryId,
  onSelect,
  onRemove,
  onAdd,
  maxRepositories,
  disabled,
}) {
  return (
    <nav className="repository-tabs" aria-label="Repository workspaces">
      {repositories.map((repository) => {
        const name = repository.session?.name || repository.source.split('/').filter(Boolean).at(-1)
        const active = repository.id === activeRepositoryId
        const status = repository.connected
          ? repository.contextReset
            ? 'reset'
            : 'connected'
          : 'disconnected'
        return (
          <div
            key={repository.id}
            className={`repository-tab ${active ? 'repository-tab-active' : ''}`}
          >
            <button
              className="repository-tab-select"
              onClick={() => onSelect(repository.id)}
              title={repository.source}
              disabled={disabled}
            >
              <span className="repository-tab-title">
                <span className={`repository-status repository-status-${status}`} />
                <span>{name}</span>
                {active && <span className="repository-active-label">Active</span>}
              </span>
              <span className="repository-tab-branch">
                {repository.session?.summary.branch || 'not connected'}
              </span>
            </button>
            {repository.session?.web_url && (
              <a
                href={repository.session.web_url}
                target="_blank"
                rel="noreferrer"
                title="Open repository"
              >
                ↗
              </a>
            )}
            <button
              className="repository-tab-remove"
              onClick={() => onRemove(repository.id)}
              title="Remove repository"
              disabled={disabled}
            >
              ×
            </button>
          </div>
        )
      })}
      <button
        className="repository-add"
        onClick={onAdd}
        disabled={disabled || repositories.length >= maxRepositories}
        title={
          repositories.length >= maxRepositories
            ? `Maximum ${maxRepositories} repositories`
            : 'Add repository'
        }
      >
        + repo
      </button>
    </nav>
  )
}

export default function App() {
  const [workspaceState, setWorkspaceState] = useState(loadWorkspaceState)
  const [showDiagnose, setShowDiagnose] = useState(false)
  const [showRepositoryDialog, setShowRepositoryDialog] = useState(false)
  const [config, setConfig] = useState(null)
  const [token, setToken] = useState(() => sessionStorage.getItem('mm_token') || '')
  const [busy, setBusy] = useState(false)
  const [elapsedMs, setElapsedMs] = useState(0)
  const [question, setQuestion] = useState('')
  const [view, setView] = useState('chat')
  const [traceVisible, setTraceVisible] = useState(
    () => loadUiPreferences().traceVisible ?? true,
  )
  const [traceWidth, setTraceWidth] = useState(
    () => loadUiPreferences().traceWidth || 440,
  )
  const [traceQuery, setTraceQuery] = useState('')
  const [expandedGroupId, setExpandedGroupId] = useState(null)
  const [tracePinned, setTracePinned] = useState(true)
  const [metaRefresh, setMetaRefresh] = useState(0)
  const traceEndRef = useRef(null)
  const traceScrollRef = useRef(null)
  const chatEndRef = useRef(null)
  const activeRequestRef = useRef(null)
  const repositories = workspaceState.repositories
  const activeRepository = repositories.find(
    (repository) => repository.id === workspaceState.activeRepositoryId,
  )
  const session = activeRepository?.connected ? activeRepository.session : null
  const repositoryInfo = activeRepository?.session || null
  const messages = activeRepository?.messages || EMPTY_LIST
  const trace = activeRepository?.trace || EMPTY_LIST
  const maxRepositories = config?.max_repositories || 3

  const updateRepository = useCallback((repositoryId, updater) => {
    setWorkspaceState((current) => ({
      ...current,
      repositories: current.repositories.map((repository) =>
        repository.id === repositoryId ? updater(repository) : repository,
      ),
    }))
  }, [])

  const updateRepositoryField = useCallback(
    (repositoryId, field, updater) => {
      updateRepository(repositoryId, (repository) => ({
        ...repository,
        [field]: typeof updater === 'function' ? updater(repository[field]) : updater,
      }))
    },
    [updateRepository],
  )

  const expireRepositorySession = useCallback(() => {
    setWorkspaceState((current) => ({
      ...current,
      repositories: current.repositories.map((repository) =>
        repository.id === current.activeRepositoryId
          ? {
              ...repository,
              connected: false,
              contextReset: repository.messages.length > 0,
            }
          : repository,
      ),
    }))
    setExpandedGroupId(null)
  }, [])

  const disconnectRepositories = useCallback(() => {
    setWorkspaceState((current) => ({
      ...current,
      repositories: current.repositories.map((repository) => ({
        ...repository,
        connected: false,
        contextReset: repository.messages.length > 0,
      })),
    }))
    setExpandedGroupId(null)
  }, [])

  const expireAuth = useCallback(() => {
    sessionStorage.removeItem('mm_token')
    setToken('')
    disconnectRepositories()
  }, [disconnectRepositories])

  useEffect(() => {
    fetch('/api/config', { headers: { 'X-Auth-Token': token } })
      .then((res) => res.json())
      .then((body) => {
        setConfig(body)
        if (body.auth_required && !body.authenticated) {
          if (token) {
            expireAuth()
          } else {
            disconnectRepositories()
          }
        }
      })
  }, [token, expireAuth, disconnectRepositories])
  useEffect(() => {
    saveWorkspaceState(workspaceState)
  }, [workspaceState])
  useEffect(() => {
    localStorage.setItem(UI_STORAGE_KEY, JSON.stringify({ traceVisible, traceWidth }))
  }, [traceVisible, traceWidth])
  useEffect(() => {
    if (tracePinned && traceVisible && view === 'chat') {
      traceEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [trace, tracePinned, traceVisible, view])
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])
  useEffect(() => {
    const toggleTrace = (event) => {
      if (event.altKey && event.key.toLowerCase() === 't') {
        event.preventDefault()
        setTraceVisible((visible) => !visible)
      }
    }
    window.addEventListener('keydown', toggleTrace)
    return () => window.removeEventListener('keydown', toggleTrace)
  }, [])
  useEffect(
    () => () => activeRequestRef.current?.abort(),
    [],
  )

  const appendTrace = (event, repositoryId = activeRepository?.id) => {
    if (!repositoryId) return
    updateRepositoryField(
      repositoryId,
      'trace',
      (current) => limitTrace([...(current || []), event]),
    )
  }

  const startTraceResize = (event) => {
    const startX = event.clientX
    const startWidth = traceWidth
    const resize = (moveEvent) => {
      const width = Math.min(720, Math.max(300, startWidth + startX - moveEvent.clientX))
      setTraceWidth(width)
    }
    const stop = () => {
      window.removeEventListener('mousemove', resize)
      window.removeEventListener('mouseup', stop)
    }
    window.addEventListener('mousemove', resize)
    window.addEventListener('mouseup', stop)
  }

  const handleTraceScroll = () => {
    const element = traceScrollRef.current
    if (!element) return
    const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight
    setTracePinned(distanceFromBottom < 60)
  }

  const openRepo = async (source, repositoryId = null) => {
    const normalizedSource = normalizeRepositorySource(source)
    const duplicate = repositories.find(
      (repository) =>
        repository.id !== repositoryId
        && normalizeRepositorySource(repository.source) === normalizedSource,
    )
    if (duplicate) {
      setWorkspaceState((current) => ({
        ...current,
        activeRepositoryId: duplicate.id,
      }))
      setShowRepositoryDialog(false)
      return
    }
    if (!repositoryId && repositories.length >= maxRepositories) {
      throw new Error(`repository limit reached (${maxRepositories})`)
    }
    setBusy(true)
    try {
      const res = await fetch('/api/repo', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': token },
        body: JSON.stringify({ source }),
      })
      if (res.status === 401) {
        expireAuth()
        throw new Error('session expired, please log in again')
      }
      const body = await res.json()
      if (!res.ok) throw new Error(body.detail)
      if (repositoryId) {
        updateRepository(repositoryId, (repository) => ({
          ...repository,
          session: body,
          source: body.source,
          connected: true,
          contextReset: repository.messages.length > 0,
        }))
      } else {
        const id = crypto.randomUUID()
        setWorkspaceState((current) => ({
          repositories: [
            ...current.repositories,
            {
              id,
              source: body.source,
              session: body,
              messages: [],
              trace: [],
              connected: true,
              contextReset: false,
            },
          ],
          activeRepositoryId: id,
        }))
      }
      setShowRepositoryDialog(false)
      setView('chat')
      setExpandedGroupId(null)
    } finally {
      setBusy(false)
    }
  }

  const reconnectRepository = async (repositoryId) => {
    const repository = repositories.find((item) => item.id === repositoryId)
    if (!repository || repository.connected || busy) return
    await openRepo(repository.source, repositoryId)
  }

  const selectRepository = (repositoryId) => {
    if (busy) return
    const repository = repositories.find((item) => item.id === repositoryId)
    if (!repository) return
    setWorkspaceState((current) => ({
      ...current,
      activeRepositoryId: repositoryId,
    }))
    setExpandedGroupId(
      groupTrace(repository.trace || []).slice(-1)[0]?.id || null,
    )
    setTraceQuery('')
    setQuestion('')
  }

  const removeRepository = async (repositoryId) => {
    if (busy) return
    const repository = repositories.find((item) => item.id === repositoryId)
    if (!repository) return
    if (repository.connected && repository.session) {
      const res = await fetch(`/api/repo/${repository.session.session_id}`, {
        method: 'DELETE',
        headers: { 'X-Auth-Token': token },
      })
      if (res.status === 401) {
        expireAuth()
        return
      }
    }
    setWorkspaceState((current) => {
      const remaining = current.repositories.filter((item) => item.id !== repositoryId)
      return {
        repositories: remaining,
        activeRepositoryId:
          current.activeRepositoryId === repositoryId
            ? remaining[0]?.id || null
            : current.activeRepositoryId,
      }
    })
    setExpandedGroupId(null)
  }

  const stopInvestigation = () => {
    activeRequestRef.current?.abort()
  }

  const ask = async (e) => {
    e.preventDefault()
    const q = question.trim()
    if (!q || busy || !activeRepository || !session) return
    const repositoryId = activeRepository.id
    const sessionId = session.session_id
    const groupId = crypto.randomUUID()
    const controller = new AbortController()
    const startedAt = Date.now()
    const timer = window.setInterval(
      () => setElapsedMs(Date.now() - startedAt),
      100,
    )
    activeRequestRef.current = controller
    setElapsedMs(0)
    setQuestion('')
    setBusy(true)
    setExpandedGroupId(groupId)
    setTracePinned(true)
    updateRepositoryField(
      repositoryId,
      'messages',
      (current) => [...(current || []), { role: 'user', text: q }].slice(-MAX_MESSAGES),
    )
    appendTrace(
      { kind: 'question', id: groupId, text: q, startedAt: Date.now() },
      repositoryId,
    )
    try {
      await streamAsk(sessionId, q, token, controller.signal, (event) => {
        if (event.kind === 'answer') {
          updateRepositoryField(
            repositoryId,
            'messages',
            (current) => [
              ...(current || []),
              {
                role: 'agent',
                text: event.text,
                verified: event.verified,
                evidence: event.evidence || [],
              },
            ].slice(-MAX_MESSAGES),
          )
          appendTrace(
            {
              kind: 'complete',
              investigation: event.investigation,
            },
            repositoryId,
          )
          setMetaRefresh((value) => value + 1)
        } else if (event.kind === 'error') {
          updateRepositoryField(
            repositoryId,
            'messages',
            (current) => [
              ...(current || []),
              { role: 'agent', text: `Error: ${event.text}`, error: true },
            ].slice(-MAX_MESSAGES),
          )
          appendTrace({ kind: 'complete', error: true }, repositoryId)
        } else {
          appendTrace(event, repositoryId)
        }
      })
    } catch (err) {
      if (err.name === 'AbortError') {
        appendTrace({ kind: 'complete', cancelled: true }, repositoryId)
      } else if (String(err.message).includes('authentication required')) {
        expireAuth()
      } else if (String(err.message).includes('unknown session')) {
        updateRepository(repositoryId, (repository) => ({
          ...repository,
          connected: false,
          contextReset: true,
        }))
      } else {
        updateRepositoryField(
          repositoryId,
          'messages',
          (current) => [
            ...(current || []),
            { role: 'agent', text: `Error: ${err.message}`, error: true },
          ].slice(-MAX_MESSAGES),
        )
        appendTrace({ kind: 'complete', error: true }, repositoryId)
      }
    } finally {
      window.clearInterval(timer)
      activeRequestRef.current = null
      setElapsedMs(0)
      setBusy(false)
    }
  }

  const clearMemory = () => {
    if (!activeRepository) return
    updateRepository(activeRepository.id, (repository) => ({
      ...repository,
      messages: [],
      trace: [],
      contextReset: false,
    }))
    setExpandedGroupId(null)
  }

  const traceGroups = groupTrace(trace)
  const normalizedQuery = traceQuery.trim().toLowerCase()
  const visibleTraceGroups = normalizedQuery
    ? traceGroups.filter((group) =>
        `${group.question}\n${group.events
          .map((event) => `${event.name || ''} ${event.text || ''} ${event.result || ''}`)
          .join('\n')}`
          .toLowerCase()
          .includes(normalizedQuery),
      )
    : traceGroups

  if (!config) return null

  if (config.auth_required && !token)
    return (
      <PasswordGate
        onAuth={(t) => {
          sessionStorage.setItem('mm_token', t)
          setToken(t)
        }}
      />
    )

  if (repositories.length === 0) return <RepoPicker onOpen={openRepo} busy={busy} />

  return (
    <div className="layout">
      <header>
        <span className="brand">Mental Model</span>
        <nav className="view-tabs" aria-label="Application views">
          <button
            className={view === 'chat' ? 'view-tab-active' : ''}
            onClick={() => setView('chat')}
          >
            Chat
          </button>
          <button
            className={view === 'meta' ? 'view-tab-active' : ''}
            onClick={() => setView('meta')}
          >
            Meta
          </button>
        </nav>
        <span className="header-spacer" />
        <button className="switch-repo" onClick={() => setShowDiagnose(true)}>
          diagnose API key
        </button>
        {view === 'chat' && (
          <button
            className="switch-repo"
            onClick={() => setTraceVisible((visible) => !visible)}
            title="Toggle investigation trace (Option+T)"
          >
            {traceVisible ? 'hide trace' : 'show trace'}
          </button>
        )}
        <button className="switch-repo" onClick={clearMemory}>
          clear memory
        </button>
      </header>
      <div className="repository-bar">
        <div className="repository-bar-label">
          <span>Repositories</span>
          <span>{repositories.length}/{maxRepositories}</span>
        </div>
        <RepositoryTabs
          repositories={repositories}
          activeRepositoryId={workspaceState.activeRepositoryId}
          onSelect={selectRepository}
          onRemove={removeRepository}
          onAdd={() => setShowRepositoryDialog(true)}
          maxRepositories={maxRepositories}
          disabled={busy}
        />
        {repositoryInfo && (
          <span className="repository-bar-context">
            {repositoryInfo.summary.commit_count} commits
            {!session && ' · disconnected'}
          </span>
        )}
      </div>
      {showRepositoryDialog && (
        <RepositoryDialog
          onOpen={openRepo}
          busy={busy}
          onClose={() => !busy && setShowRepositoryDialog(false)}
        />
      )}
      {showDiagnose && (
        <DiagnosePanel
          onClose={() => setShowDiagnose(false)}
          token={token}
          onAuthExpired={expireAuth}
        />
      )}
      {!session && (
        <div className="context-reset-banner">
          <span>
            The transcript is preserved, but this repository's agent session must be reconnected.
          </span>
          <button
            onClick={() => reconnectRepository(activeRepository.id)}
            disabled={busy}
          >
            {busy ? 'Reconnecting…' : 'Reconnect repository'}
          </button>
        </div>
      )}
      {session && activeRepository.contextReset && (
        <div className="context-reset-banner context-reset-warning">
          Earlier messages are preserved for reference, but the agent context restarted.
        </div>
      )}
      {view === 'meta' && session ? (
        <MetaPanel
          session={session}
          token={token}
          refreshKey={metaRefresh}
          onAuthExpired={expireAuth}
          onSessionExpired={expireRepositorySession}
          repositories={repositories}
          activeRepositoryId={workspaceState.activeRepositoryId}
          maxRepositories={maxRepositories}
        />
      ) : view === 'meta' ? (
        <div className="meta-page">
          <div className="hint">Reconnect this repository to inspect live agent metadata.</div>
        </div>
      ) : (
        <main
          className="workspace"
          style={{
            gridTemplateColumns: traceVisible
              ? `minmax(0, 1fr) 6px ${traceWidth}px`
              : 'minmax(0, 1fr)',
          }}
        >
          <section className="chat">
            <div className="chat-scroll">
              {messages.length === 0 && (
                <div className="hint">
                  Try: “When was the shell completion feature introduced, and why?”
                </div>
              )}
              {messages.map((m, i) => (
                <div key={i} className={`msg msg-${m.role} ${m.error ? 'msg-error' : ''}`}>
                  {m.role === 'agent' ? (
                    <>
                      <AnswerText
                        text={m.text}
                        session={repositoryInfo}
                        token={token}
                        evidence={m.evidence}
                      />
                      {m.verified === true && <span className="verified-badge">evidence verified</span>}
                    </>
                  ) : (
                    m.text
                  )}
                </div>
              ))}
              {busy && (
                <div className="msg msg-agent msg-busy">
                  investigating… {formatDuration(elapsedMs)}
                </div>
              )}
              <div ref={chatEndRef} />
            </div>
            <form className="ask-bar" onSubmit={ask}>
              <input
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder="Ask about this repository…"
                disabled={busy || !session}
                autoFocus
              />
              {busy ? (
                <button type="button" className="stop-button" onClick={stopInvestigation}>
                  Stop · {formatDuration(elapsedMs)}
                </button>
              ) : (
                <button disabled={!session || !question.trim()}>Ask</button>
              )}
            </form>
          </section>
          {traceVisible && (
            <>
              <div
                className="trace-resizer"
                onMouseDown={startTraceResize}
                role="separator"
                aria-label="Resize investigation trace"
              />
              <aside className="trace">
                <div className="trace-header">
                  <div>
                    <span>Investigation trace</span>
                    <span>{traceGroups.length} investigations</span>
                  </div>
                  <button onClick={() => setExpandedGroupId(null)}>collapse all</button>
                </div>
                <div className="trace-search">
                  <input
                    value={traceQuery}
                    onChange={(event) => setTraceQuery(event.target.value)}
                    placeholder="Search questions, tools, and results…"
                  />
                </div>
                <div
                  className="trace-scroll"
                  ref={traceScrollRef}
                  onScroll={handleTraceScroll}
                >
                  {visibleTraceGroups.length === 0 && (
                    <div className="hint">
                      {traceGroups.length ? 'No investigations match.' : 'Tool calls appear here live.'}
                    </div>
                  )}
                  {visibleTraceGroups.map((group) => (
                    <InvestigationGroup
                      key={group.id}
                      group={group}
                      open={expandedGroupId === group.id}
                      onToggle={() => {
                        setTracePinned(false)
                        setExpandedGroupId((current) => current === group.id ? null : group.id)
                      }}
                      onInteract={() => setTracePinned(false)}
                    />
                  ))}
                  <div ref={traceEndRef} />
                </div>
                {!tracePinned && (
                  <button
                    className="trace-jump"
                    onClick={() => {
                      setTracePinned(true)
                      traceEndRef.current?.scrollIntoView({ behavior: 'smooth' })
                    }}
                  >
                    jump to latest
                  </button>
                )}
              </aside>
            </>
          )}
        </main>
      )}
    </div>
  )
}
