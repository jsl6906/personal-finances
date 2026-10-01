import { useRef, useState, type DragEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { get, post, upload, type BatchDetail, type BatchSummary } from '../api'
import { Button, Card, ErrorNote, Icon } from '../components/ui'
import { fullDate } from '../format'
import { STATUS_TAG, STEP_NAMES } from '../review'

const SHEET_ACCEPT = '.csv,.tsv,.txt,.xlsx,.xlsm,.xls'
const DOC_ACCEPT = '.pdf,.png,.jpg,.jpeg,.webp,.heic,.gif,.tif,.tiff'

export function Steps({ current, reachable, onGo, only }: {
  current: number; reachable: number; onGo?: (n: number) => void; only?: number[]
}) {
  return (
    <ol className="steps">
      {STEP_NAMES.map((label, i) => {
        const n = i + 1
        const cls = n === current ? ' on' : n < current ? ' done' : ''
        const can = !!onGo && n <= reachable && n !== current && (!only || only.includes(n))
        return (
          <li key={label} className={`step${cls}${can ? ' clickable' : ''}`} onClick={() => can && onGo!(n)}>
            <span className="step-n">{String(n).padStart(2, '0')}</span>
            <span className="step-label">{label}</span>
          </li>
        )
      })}
    </ol>
  )
}

function DropCard({ title, body, icon, accept, onFile, busy }: {
  title: string; body: string; icon: string; accept: string; onFile: (f: File) => void; busy: boolean
}) {
  const input = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setOver(false)
    const f = e.dataTransfer.files[0]
    if (f) onFile(f)
  }
  return (
    <div onDragOver={(e) => { e.preventDefault(); setOver(true) }} onDragLeave={() => setOver(false)} onDrop={onDrop}>
      <Card className={`drop${over ? ' over' : ''}`}>
        <span style={{ color: 'var(--color-accent)' }}><Icon name={icon} size={28} /></span>
        <div className="card-title">{title}</div>
        <p className="card-body" style={{ maxWidth: '34ch' }}>{body}</p>
        <input ref={input} type="file" accept={accept} hidden
          onChange={(e) => { const f = e.target.files?.[0]; if (f) onFile(f); e.target.value = '' }} />
        <Button onClick={() => input.current?.click()} disabled={busy}>{busy ? 'Uploading…' : 'Choose file'}</Button>
      </Card>
    </div>
  )
}

export function ImportHome() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const recent = useQuery({ queryKey: ['imports'], queryFn: () => get<BatchSummary[]>('/imports', { limit: 25 }) })
  const send = useMutation({
    mutationFn: (f: File) => upload<BatchDetail>('/imports', f),
    onSuccess: (b) => {
      qc.invalidateQueries({ queryKey: ['imports'] })
      navigate(`/import/${b.id}`)
    },
  })
  const rollback = useMutation({
    mutationFn: (id: number) => post(`/imports/${id}/rollback`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['imports'] })
      qc.invalidateQueries({ queryKey: ['transactions'] })
    },
  })

  return (
    <section className="page" style={{ gap: 'var(--space-6)' }}>
      <header>
        <h2 style={{ margin: 0 }}>Import transactions</h2>
        <div className="text-muted subtitle">Spreadsheet or document → mapped rows → defaults → duplicate review → commit</div>
      </header>
      <Steps current={1} reachable={1} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(300px, 100%), 1fr))', gap: 'var(--space-4)' }}>
        <DropCard title="Spreadsheet" icon="import" accept={SHEET_ACCEPT} busy={send.isPending} onFile={(f) => send.mutate(f)}
          body=".xlsx, .xls, .csv, or a Tiller sheet export. You'll map columns to fields next." />
        <DropCard title="Document" icon="bills" accept={DOC_ACCEPT} busy={send.isPending} onFile={(f) => send.mutate(f)}
          body="Bank or card statement PDF / scan. Gemini extracts the transaction table and proposes defaults." />
      </div>
      <ErrorNote error={send.error || rollback.error} />
      <Card style={{ gap: 'var(--space-2)' }}>
        <div className="card-kicker">Recent imports</div>
        <table className="table">
          <thead>
            <tr><th>File</th><th>Type</th><th>Status</th><th>Rows</th><th>Inserted</th><th>Duplicates removed</th><th>When</th><th /></tr>
          </thead>
          <tbody>
            {(recent.data ?? []).map((b) => (
              <tr key={b.id} className="clickable" onClick={() => navigate(`/import/${b.id}`)}>
                <td>{b.filename}</td>
                <td className="text-muted">{b.source_type}{b.origin !== 'upload' ? ` · ${b.origin}` : ''}</td>
                <td><span className={`tag ${STATUS_TAG[b.status] ?? 'tag-neutral'}`}>{b.status.replace('_', ' ')}</span></td>
                <td>{b.row_count}</td>
                <td>{b.stats.inserted ?? '—'}</td>
                <td>{b.stats.skipped_duplicates ?? '—'}</td>
                <td className="text-muted">{fullDate(b.created_at)}</td>
                <td onClick={(e) => e.stopPropagation()}>
                  {b.status === 'committed' && (
                    <Button variant="ghost" className="small" disabled={rollback.isPending}
                      onClick={() => confirm(`Roll back ${b.stats.inserted} transactions from ${b.filename}?`) && rollback.mutate(b.id)}>
                      Roll back
                    </Button>
                  )}
                </td>
              </tr>
            ))}
            {recent.data?.length === 0 && <tr><td colSpan={8} className="text-muted">No imports yet.</td></tr>}
          </tbody>
        </table>
      </Card>
    </section>
  )
}
