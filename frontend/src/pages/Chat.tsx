import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { del, get, post, upload, type ChatMessage, type ChatQuery, type ChatSession } from '../api'
import { Button, Corners, ErrorNote } from '../components/ui'
import { fullDate } from '../format'

const STARTERS = [
  'What did we spend on groceries each month this year?',
  'Which merchants did we spend the most with in the last 90 days?',
  'How does this month’s dining spend compare with our 12-month average?',
  'How has our electricity usage changed, and are we paying more per kWh?',
]
const DOC_ACCEPT = '.pdf,.png,.jpg,.jpeg,.webp,.heic,.gif,.tif,.tiff,.csv,.tsv,.txt,.xlsx,.xlsm,.xls'

export function Chat() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const chatId = params.get('c') ? Number(params.get('c')) : null
  const [input, setInput] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const bottom = useRef<HTMLDivElement>(null)

  const sessions = useQuery({ queryKey: ['chat', 'sessions'], queryFn: () => get<ChatSession[]>('/chat/sessions') })
  const messages = useQuery({
    queryKey: ['chat', 'messages', chatId],
    queryFn: () => get<ChatMessage[]>(`/chat/sessions/${chatId}/messages`),
    enabled: chatId !== null,
  })

  const send = useMutation({
    mutationFn: async ({ text, doc }: { text: string; doc: File | null }) => {
      let id = chatId
      if (id === null) {
        id = (await post<ChatSession>('/chat/sessions', {})).id
        setParams({ c: String(id) }, { replace: true })
      }
      const out = await upload<ChatMessage[]>(`/chat/sessions/${id}/messages`, doc, { content: text })
      return { id, out }
    },
    onSuccess: ({ id, out }) => {
      qc.setQueryData<ChatMessage[]>(['chat', 'messages', id], (prev) => [
        ...(prev ?? []).filter((m) => !out.some((o) => o.id === m.id)), ...out,
      ])
      qc.invalidateQueries({ queryKey: ['chat', 'sessions'] })
    },
  })
  const remove = useMutation({
    mutationFn: (id: number) => del(`/chat/sessions/${id}`),
    onSuccess: (_, id) => {
      if (id === chatId) setParams({})
      qc.invalidateQueries({ queryKey: ['chat', 'sessions'] })
    },
  })

  const list = chatId === null ? [] : messages.data ?? []
  const pending = send.isPending ? send.variables : undefined

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [list.length, send.isPending])

  const submit = (text = input) => {
    const q = text.trim()
    if ((!q && !file) || send.isPending) return
    send.mutate({ text: q, doc: file })
    setInput('')
    setFile(null)
  }

  return (
    <section className="page" style={{ minHeight: 'calc(100vh - 2 * var(--space-8))' }}>
      <header className="page-header">
        <div>
          <h2>Ask the ledger</h2>
          <div className="text-muted subtitle">
            Questions run against your transactions, budgets and attached statements. Documents you drop here can be
            attached to the ledger.
          </div>
        </div>
      </header>
      <div className="split-nav chat-layout">
        <nav className="side-nav">
          <Button onClick={() => { setParams({}); send.reset() }} style={{ marginBottom: 'var(--space-2)' }}>
            New conversation
          </Button>
          {(sessions.data ?? []).map((s) => (
            <div key={s.id} className={`nav-item${s.id === chatId ? ' active' : ''}`} style={{ cursor: 'pointer' }}
              onClick={() => { setParams({ c: String(s.id) }); send.reset() }} title={fullDate(s.updated_at.slice(0, 10))}>
              <span className="label" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.title}</span>
              <button className="btn btn-ghost small" style={{ padding: '0 4px', minHeight: 0 }} title="Delete conversation"
                onClick={(e) => { e.stopPropagation(); if (confirm('Delete this conversation?')) remove.mutate(s.id) }}>×</button>
            </div>
          ))}
        </nav>

        <div className="chat-pane">
          <div className="stack-3" style={{ overflowY: 'auto', overflowX: 'hidden', padding: '4px var(--space-2)', gap: 'var(--space-4)' }}>
            <ErrorNote error={messages.error || send.error || remove.error} />
            {list.length === 0 && !pending && (
              <div className="stack" style={{ marginTop: 'var(--space-6)' }}>
                <div className="small text-muted">Try one of these</div>
                {STARTERS.map((s) => (
                  <button key={s} className="chip" style={{ textAlign: 'left', padding: '8px 12px' }} onClick={() => submit(s)}>{s}</button>
                ))}
              </div>
            )}
            {list.map((m) => <Message key={m.id} m={m} />)}
            {pending && (
              <>
                <Bubble who="You" me text={pending.text} doc={pending.doc?.name} />
                <div className="stack" style={{ alignItems: 'flex-start' }}>
                  <Who>Ledger</Who>
                  <div className="text-muted" style={{ fontSize: 14 }}><span className="status-dot pending" /> Working through your data…</div>
                </div>
              </>
            )}
            <div ref={bottom} />
          </div>

          <div className="stack">
            {file && (
              <div className="row small">
                <span className="tag tag-outline">{file.name}</span>
                <button className="btn btn-ghost small" onClick={() => setFile(null)}>Remove</button>
              </div>
            )}
            <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
              <input ref={fileInput} type="file" hidden accept={DOC_ACCEPT}
                onChange={(e) => { setFile(e.target.files?.[0] ?? null); e.target.value = '' }} />
              <Button className="btn-icon" title="Attach document" onClick={() => fileInput.current?.click()}>+</Button>
              <input className="input" value={input} onChange={(e) => setInput(e.target.value)}
                placeholder="e.g. What did we spend on the F-150 this year, including insurance?"
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() } }} />
              <Button variant="primary" onClick={() => submit()} disabled={send.isPending || (!input.trim() && !file)}>Send</Button>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

function Who({ children }: { children: string }) {
  return (
    <div style={{ fontSize: 11, letterSpacing: '.08em', textTransform: 'uppercase', color: 'var(--color-neutral-600)' }}>
      {children}
    </div>
  )
}

function Bubble({ who, me, text, doc, docHref }: { who: string; me?: boolean; text: string; doc?: string | null; docHref?: string }) {
  return (
    <div className="stack" style={{ alignItems: me ? 'flex-end' : 'flex-start', gap: 6 }}>
      <Who>{who}</Who>
      {text && (
        <div className="blueprint" style={{
          maxWidth: '72ch', padding: 'var(--space-3) var(--space-4)', fontSize: 14, lineHeight: 1.5, whiteSpace: 'pre-wrap',
          background: me ? 'var(--color-accent-900)' : 'transparent', color: me ? 'var(--color-bg)' : 'var(--color-text)',
        }}>
          <Corners />
          {text}
        </div>
      )}
      {doc && (docHref
        ? <a className="tag tag-outline" href={docHref} target="_blank" rel="noreferrer">{doc}</a>
        : <span className="tag tag-outline">{doc}</span>)}
    </div>
  )
}

function Message({ m }: { m: ChatMessage }) {
  if (m.role === 'user') {
    return (
      <div className="stack" style={{ alignItems: 'flex-end', gap: 6 }}>
        <Bubble who="You" me text={m.content} doc={m.attachment_name}
          docHref={m.attachment_id ? `/api/attachments/${m.attachment_id}/content` : undefined} />
        {m.attachment_id && <DocActions id={m.attachment_id} kind={m.attachment_kind} />}
      </div>
    )
  }
  if (m.role === 'error') return <div className="callout error">{m.content}</div>
  return (
    <div className="stack" style={{ alignItems: 'flex-start', gap: 6 }}>
      <Who>Ledger</Who>
      <div className="blueprint chat-answer" style={{ maxWidth: '72ch', padding: 'var(--space-3) var(--space-4)', fontSize: 14, lineHeight: 1.5 }}>
        <Corners />
        <div><Markdown remarkPlugins={[remarkGfm]}>{m.content}</Markdown></div>
      </div>
      {m.queries.length > 0 && <Queries queries={m.queries} />}
    </div>
  )
}

function Queries({ queries }: { queries: ChatQuery[] }) {
  return (
    <details className="small" style={{ alignSelf: 'stretch', minWidth: 0 }}>
      <summary className="text-muted" style={{ cursor: 'pointer' }}>
        {queries.length} {queries.length === 1 ? 'query' : 'queries'} behind this answer
      </summary>
      <div className="stack-3" style={{ marginTop: 'var(--space-2)' }}>
        {queries.map((q, i) => (
          <div key={i} className="stack">
            {q.purpose && <strong>{q.purpose}</strong>}
            <pre style={{ margin: 0, padding: 'var(--space-2)', border: '1px solid var(--color-divider)', whiteSpace: 'pre-wrap',
              fontSize: 12, overflowX: 'auto' }}>{q.sql}</pre>
            {q.error ? <div className="callout error">{q.error}</div> : <ResultTable q={q} />}
          </div>
        ))}
      </div>
    </details>
  )
}

function ResultTable({ q }: { q: ChatQuery }) {
  if (!q.columns?.length) return null
  const rows = q.rows ?? []
  return (
    <div className="table-card" style={{ maxHeight: 260, overflowY: 'auto', border: '1px solid var(--color-divider)' }}>
      <table className="table">
        <thead><tr>{q.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((v, j) => (
                <td key={j} className={typeof v === 'number' ? 'num' : undefined}>
                  {v === null ? '—' : Array.isArray(v) ? v.join(', ')
                    : typeof v === 'number' ? v.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(v)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {(q.row_count ?? 0) > rows.length && (
        <div className="table-foot text-muted">Showing {rows.length} of {q.row_count}{q.truncated ? '+' : ''} rows</div>
      )}
    </div>
  )
}

function DocActions({ id, kind }: { id: number; kind: ChatMessage['attachment_kind'] }) {
  const navigate = useNavigate()
  const bill = useMutation({
    mutationFn: () => post<{ statement_id: number }>(`/chat/attachments/${id}/statement`),
    onSuccess: () => navigate('/bills'),
  })
  const imp = useMutation({
    mutationFn: () => post<{ batch_id: number }>(`/chat/attachments/${id}/import`),
    onSuccess: (r) => navigate(`/import/${r.batch_id}`),
  })
  return (
    <div className="stack" style={{ alignItems: 'flex-end' }}>
      <div className="row">
        {kind === 'document' && (
          <Button onClick={() => bill.mutate()} disabled={bill.isPending}>File as bill / statement</Button>
        )}
        <Button onClick={() => imp.mutate()} disabled={imp.isPending}>Import transactions</Button>
      </div>
      <ErrorNote error={bill.error || imp.error} />
    </div>
  )
}
