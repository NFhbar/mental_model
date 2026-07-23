import { useEffect, useRef, useState } from 'react'
import './App.css'

const CITATION_RE = /\[commit:([0-9a-fA-F]{4,40})\]/g
const STORAGE_KEY = 'mm_state'

function loadStoredState() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}
  } catch {
    return {}
  }
}

async function streamAsk(sessionId, question, onEvent) {
  const res = await fetch('/api/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, question }),
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

function CitationChip({ sha, sessionId }) {
  const [detail, setDetail] = useState(null)
  const [open, setOpen] = useState(false)

  const toggle = async () => {
    if (!open && detail === null) {
      try {
        const res = await fetch(`/api/commit/${sessionId}/${sha}`)
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

function AnswerText({ text, sessionId }) {
  const parts = []
  let last = 0
  let match
  CITATION_RE.lastIndex = 0
  while ((match = CITATION_RE.exec(text)) !== null) {
    parts.push(text.slice(last, match.index))
    parts.push(<CitationChip key={match.index} sha={match[1]} sessionId={sessionId} />)
    last = match.index + match[0].length
  }
  parts.push(text.slice(last))
  return <div className="answer-text">{parts}</div>
}

function TraceItem({ event }) {
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
        <button className="trace-toggle" onClick={() => setOpen(!open)}>
          {open ? '▾' : '▸'} result ({event.result.length} chars)
        </button>
        {open && <pre>{event.result}</pre>}
      </div>
    )
  }
  if (event.kind === 'verification_failed') {
    return (
      <div className="trace-verification">
        citation verification failed, retrying:
        <ul>
          {event.problems.map((p, i) => (
            <li key={i}>{p}</li>
          ))}
        </ul>
      </div>
    )
  }
  return null
}

function DiagnosePanel({ onClose, token }) {
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetch('/api/diagnose', { headers: { 'X-Auth-Token': token } })
      .then((res) => res.json())
      .then(setReport)
      .catch((err) => setError(String(err)))
  }, [token])

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

function RepoPicker({ onOpen, busy }) {
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
    <form className="repo-picker" onSubmit={submit}>
      <h1>Mental Model</h1>
      <p>Point me at a repository and ask me anything about its history.</p>
      <input
        value={source}
        onChange={(e) => setSource(e.target.value)}
        placeholder="Local path or GitHub URL (e.g. https://github.com/pallets/click)"
        autoFocus
      />
      <button disabled={busy || !source.trim()}>{busy ? 'Opening…' : 'Open repository'}</button>
      {error && <div className="error">{error}</div>}
    </form>
  )
}

export default function App() {
  const [session, setSession] = useState(() => loadStoredState().session || null)
  const [showDiagnose, setShowDiagnose] = useState(false)
  const [config, setConfig] = useState(null)
  const [token, setToken] = useState(() => sessionStorage.getItem('mm_token') || '')
  const [busy, setBusy] = useState(false)
  const [messages, setMessages] = useState(() => loadStoredState().messages || [])
  const [trace, setTrace] = useState(() => loadStoredState().trace || [])
  const [question, setQuestion] = useState('')
  const traceEndRef = useRef(null)
  const chatEndRef = useRef(null)

  useEffect(() => {
    fetch('/api/config')
      .then((res) => res.json())
      .then(setConfig)
  }, [])
  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ session, messages, trace }))
  }, [session, messages, trace])
  useEffect(() => {
    traceEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [trace])
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const openRepo = async (source) => {
    setBusy(true)
    try {
      const res = await fetch('/api/repo', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': token },
        body: JSON.stringify({ source }),
      })
      if (res.status === 401) {
        sessionStorage.removeItem('mm_token')
        setToken('')
        throw new Error('session expired, please log in again')
      }
      const body = await res.json()
      if (!res.ok) throw new Error(body.detail)
      setSession(body)
    } finally {
      setBusy(false)
    }
  }

  const ask = async (e) => {
    e.preventDefault()
    const q = question.trim()
    if (!q || busy) return
    setQuestion('')
    setBusy(true)
    setMessages((m) => [...m, { role: 'user', text: q }])
    setTrace((t) => [...t, { kind: 'question', text: q }])
    try {
      await streamAsk(session.session_id, q, (event) => {
        if (event.kind === 'answer') {
          setMessages((m) => [
            ...m,
            { role: 'agent', text: event.text, verified: event.verified },
          ])
        } else if (event.kind === 'error') {
          setMessages((m) => [...m, { role: 'agent', text: `Error: ${event.text}`, error: true }])
        } else {
          setTrace((t) => [...t, event])
        }
      })
    } catch (err) {
      if (String(err.message).includes('unknown session')) {
        setSession(null)
        setMessages([])
        setTrace([])
      } else {
        setMessages((m) => [...m, { role: 'agent', text: `Error: ${err.message}`, error: true }])
      }
    } finally {
      setBusy(false)
    }
  }

  const clearMemory = () => {
    setMessages([])
    setTrace([])
    localStorage.removeItem(STORAGE_KEY)
  }

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

  if (!session) return <RepoPicker onOpen={openRepo} busy={busy} />

  return (
    <div className="layout">
      <header>
        <span className="brand">Mental Model</span>
        <button
          className="repo-chip"
          onClick={() => session.web_url && window.open(session.web_url, '_blank')}
          disabled={!session.web_url}
          title={session.web_url || session.root}
        >
          {session.name}
        </button>
        <span className="repo-info">
          {session.summary.branch} · {session.summary.commit_count} commits · {session.summary.head}
        </span>
        <button className="switch-repo" onClick={() => setShowDiagnose(true)}>
          diagnose API key
        </button>
        <button className="switch-repo" onClick={clearMemory}>
          clear memory
        </button>
        <button className="switch-repo" onClick={() => { setSession(null); clearMemory() }}>
          switch repo
        </button>
      </header>
      {showDiagnose && <DiagnosePanel onClose={() => setShowDiagnose(false)} token={token} />}
      <main>
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
                    <AnswerText text={m.text} sessionId={session.session_id} />
                    {m.verified === true && <span className="verified-badge">citations verified</span>}
                  </>
                ) : (
                  m.text
                )}
              </div>
            ))}
            {busy && <div className="msg msg-agent msg-busy">investigating…</div>}
            <div ref={chatEndRef} />
          </div>
          <form className="ask-bar" onSubmit={ask}>
            <input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="Ask about this repository's history…"
              disabled={busy}
              autoFocus
            />
            <button disabled={busy || !question.trim()}>Ask</button>
          </form>
        </section>
        <aside className="trace">
          <div className="trace-header">Investigation trace</div>
          <div className="trace-scroll">
            {trace.length === 0 && <div className="hint">Tool calls appear here live.</div>}
            {trace.map((event, i) => (
              <TraceItem key={i} event={event} />
            ))}
            <div ref={traceEndRef} />
          </div>
        </aside>
      </main>
    </div>
  )
}
