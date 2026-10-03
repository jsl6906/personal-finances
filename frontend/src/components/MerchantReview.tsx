import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, post, type Job, type MerchantReviewState, type MerchantSuggestion, type ReviewAcceptResult, type ReviewMember } from '../api'
import { shortDate } from '../format'
import { useJob } from '../hooks'
import { merchantPath } from '../links'
import { Button, Card, ErrorNote, ProgressBar, Seg } from './ui'

type View = 'merge' | 'rename' | 'minor'
type Edit = { name?: string; target?: string; skip?: string[] }
const PAGE = 100
const FOLD = 6
const ACTIVE = ['queued', 'running']

const inView = (s: MerchantSuggestion, v: View) => (v === 'merge' ? s.kind === 'merge' : s.kind === 'rename' && s.minor === (v === 'minor'))

function payload(s: MerchantSuggestion, e: Edit = {}) {
  const members = [s.target, ...s.sources].map((m) => m.key)
  return {
    id: s.id,
    display_name: e.name,
    target_key: e.target,
    keys: e.skip?.length ? members.filter((k) => !e.skip!.includes(k)) : undefined,
  }
}

export function MerchantReview() {
  const qc = useQueryClient()
  const review = useQuery({ queryKey: ['merchant-review'], queryFn: () => get<MerchantReviewState>('/merchants/review') })
  const [jobId, setJobId] = useState<number | null>(null)
  const latest = review.data?.job ?? null
  const job = useJob(jobId ?? (latest && ACTIVE.includes(latest.status) ? latest.id : null))
  const shownJob: Job | null = job.data ?? latest
  const running = !!shownJob && ACTIVE.includes(shownJob.status)
  const [all, setAll] = useState(false)
  const [view, setView] = useState<View>('merge')
  const [q, setQ] = useState('')
  const [limit, setLimit] = useState(PAGE)
  const [edits, setEdits] = useState<Record<number, Edit>>({})
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => {
    if (job.data && !ACTIVE.includes(job.data.status)) qc.invalidateQueries({ queryKey: ['merchant-review'] })
  }, [job.data?.status, qc, job.data])

  const suggestions = useMemo(() => review.data?.suggestions ?? [], [review.data])
  const counts = useMemo(() => {
    const c = { merge: 0, rename: 0, minor: 0 }
    for (const s of suggestions) for (const v of ['merge', 'rename', 'minor'] as View[]) if (inView(s, v)) c[v]++
    return c
  }, [suggestions])
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return suggestions.filter((s) => inView(s, view) && (!needle ||
      [s.display_name ?? '', ...[s.target, ...s.sources].flatMap((m) => [m.key, m.name, m.sample ?? ''])]
        .some((x) => x.toLowerCase().includes(needle))))
  }, [suggestions, view, q])
  const shown = filtered.slice(0, limit)
  const allChecked = shown.length > 0 && shown.every((s) => checked.has(s.id))

  const refresh = () => {
    setChecked(new Set())
    for (const k of ['merchant-review', 'transactions', 'details', 'merchants', 'analytics', 'rules']) qc.invalidateQueries({ queryKey: [k] })
  }
  const run = useMutation({
    mutationFn: () => post<Job>('/merchants/review/run', { all }),
    onSuccess: (j) => { setMessage(null); setEdits({}); setJobId(j.id); qc.invalidateQueries({ queryKey: ['merchant-review'] }) },
  })
  const accept = useMutation({
    mutationFn: (items: MerchantSuggestion[]) =>
      post<ReviewAcceptResult>('/merchants/review/accept', { items: items.map((s) => payload(s, edits[s.id])) }),
    onSuccess: (r) => {
      setMessage(`Applied ${r.accepted} suggestion${r.accepted === 1 ? '' : 's'}` +
        (r.moved ? `; ${r.moved.toLocaleString()} transactions moved to their merged merchant` : '') +
        (r.errors.length ? `. Skipped: ${r.errors.join('; ')}` : '.'))
      refresh()
    },
  })
  const dismiss = useMutation({
    mutationFn: (ids: number[]) => post<{ dismissed: number }>('/merchants/review/dismiss', { ids }),
    onSuccess: (r) => { setMessage(`Dismissed ${r.dismissed}. Those merchants won't be suggested again.`); refresh() },
  })
  const busy = accept.isPending || dismiss.isPending

  const edit = (id: number, patch: Partial<Edit>) => setEdits((m) => ({ ...m, [id]: { ...m[id], ...patch } }))
  const toggle = (id: number) => setChecked((s) => {
    const n = new Set(s)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    return n
  })
  const start = () => {
    const waiting = suggestions.length
    if (!waiting || confirm(`Start a new review? It replaces the ${waiting} suggestion${waiting === 1 ? '' : 's'} still waiting.`)) run.mutate()
  }
  const selected = suggestions.filter((s) => checked.has(s.id))

  return (
    <>
      <Card style={{ gap: 'var(--space-2)' }}>
        <div className="card-kicker">AI merchant cleanup</div>
        <div className="small">
          Gemini reads every merchant you haven't cleaned up yet (no custom name, nothing merged into it) and proposes
          <strong> merges</strong> of merchants that are the same business — store numbers, cities, card-processor prefixes —
          and <strong>cleaner names</strong>. Nothing changes until you accept. Accepted and dismissed merchants, and ones that
          already look right, are skipped next time.
        </div>
        <div className="row">
          <span className="text-muted small">
            {review.data ? `${review.data.unreviewed.toLocaleString()} merchants not reviewed yet · ${suggestions.length.toLocaleString()} suggestions waiting` : 'Loading…'}
          </span>
          <span className="spacer" />
          <label className="small row" style={{ gap: 6 }}>
            <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> Include merchants reviewed before
          </label>
          <Button variant="primary" onClick={start} disabled={running || run.isPending}>Review merchants with AI</Button>
        </div>
      </Card>

      {shownJob && (running || jobId !== null) && (
        <div className="stack">
          <div className={`callout${shownJob.status === 'failed' ? ' error' : ''}`}>
            <strong>Merchant review · {shownJob.status}</strong> {shownJob.message ?? ''}
            {shownJob.status === 'succeeded' && shownJob.result && (
              ` · ${shownJob.result.merges} merges and ${shownJob.result.renames} renames suggested from ${Number(shownJob.result.considered).toLocaleString()} merchants` +
              (shownJob.result.failed_chunks ? ` (${shownJob.result.failed_chunks} batches failed; run again to retry them)` : '')
            )}
            {shownJob.status === 'failed' && <div className="small">{shownJob.error?.split('\n')[0]}</div>}
            {!running && <Button variant="ghost" className="small" onClick={() => setJobId(null)}>Dismiss</Button>}
          </div>
          {running && <ProgressBar fraction={Number(shownJob.progress)} />}
        </div>
      )}
      {message && (
        <div className="callout row" style={{ flexWrap: 'nowrap' }}>
          <span style={{ flex: 1 }}>{message}</span>
          <Button variant="ghost" className="small" onClick={() => setMessage(null)}>Dismiss</Button>
        </div>
      )}
      <ErrorNote error={review.error || run.error || accept.error || dismiss.error} />

      <div className="row-3">
        <Seg name="review-view" value={view} onChange={(v) => { setView(v); setLimit(PAGE); setChecked(new Set()) }} options={[
          { value: 'merge', label: `Merges (${counts.merge})` },
          { value: 'rename', label: `Renames (${counts.rename})` },
          { value: 'minor', label: `Capitalization only (${counts.minor})` },
        ]} />
        <input className="input compact" placeholder="Search suggestions…" style={{ width: 220 }} value={q}
          onChange={(e) => { setQ(e.target.value); setLimit(PAGE) }} />
      </div>

      {selected.length > 0 && (
        <div className="bulk-bar">
          <strong>{selected.length} selected</strong>
          <Button variant="primary" disabled={busy} onClick={() => accept.mutate(selected)}>Accept</Button>
          <Button variant="ghost" disabled={busy} onClick={() => dismiss.mutate(selected.map((s) => s.id))}>Dismiss</Button>
          <span className="spacer" />
          <Button variant="ghost" onClick={() => setChecked(new Set())}>Clear selection</Button>
        </div>
      )}

      <Card className="table-card">
        <table className="table">
          <thead>
            <tr>
              <th className="check">
                <input type="checkbox" checked={allChecked} disabled={!shown.length}
                  onChange={() => setChecked(allChecked ? new Set() : new Set(shown.map((s) => s.id)))} />
              </th>
              <th>{view === 'merge' ? 'Merchants to combine' : 'Merchant'}</th>
              <th style={{ width: 260 }}>{view === 'merge' ? 'Combined name' : 'New name'}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {shown.map((s) => (
              <SuggestionRow key={s.id} s={s} e={edits[s.id] ?? {}} checked={checked.has(s.id)} busy={busy}
                onCheck={() => toggle(s.id)} onEdit={(p) => edit(s.id, p)}
                onAccept={() => accept.mutate([s])} onDismiss={() => dismiss.mutate([s.id])} />
            ))}
            {review.isSuccess && filtered.length === 0 && (
              <tr><td colSpan={4} className="empty text-muted">
                {suggestions.length ? 'No suggestions match.' : running ? 'Reviewing…' : 'No suggestions waiting. Run a review to get some.'}
              </td></tr>
            )}
          </tbody>
        </table>
        {filtered.length > shown.length && (
          <div className="table-foot">
            <span className="text-muted">Showing {shown.length} of {filtered.length.toLocaleString()}</span>
            <Button variant="ghost" onClick={() => setLimit((n) => n + PAGE * 4)}>Show more</Button>
          </div>
        )}
      </Card>
    </>
  )
}

function SuggestionRow({ s, e, checked, busy, onCheck, onEdit, onAccept, onDismiss }: {
  s: MerchantSuggestion; e: Edit; checked: boolean; busy: boolean
  onCheck: () => void; onEdit: (p: Partial<Edit>) => void; onAccept: () => void; onDismiss: () => void
}) {
  const target = e.target ?? s.target.key
  const skip = e.skip ?? []
  const members = [s.target, ...s.sources]
  const included = members.filter((m) => m.key === target || !skip.includes(m.key))
  const [open, setOpen] = useState(false)
  const visible = open ? members : members.filter((m, i) => i < FOLD || m.key === target)
  return (
    <tr className={checked ? 'selected' : undefined}>
      <td className="check"><input type="checkbox" checked={checked} onChange={onCheck} /></td>
      <td>
        {s.kind === 'merge' ? (
          <div style={{ display: 'grid', gap: 4 }}>
            {visible.map((m) => (
              <div key={m.key} className="row small" style={{ gap: 6, flexWrap: 'nowrap', alignItems: 'baseline' }}>
                <input type="checkbox" aria-label={`Include ${m.name}`} checked={m.key === target || !skip.includes(m.key)}
                  disabled={m.key === target}
                  onChange={(ev) => onEdit({ skip: ev.target.checked ? skip.filter((k) => k !== m.key) : [...skip, m.key] })} />
                <Member m={m} />
                {m.key === target
                  ? <span className="tag tag-neutral" title="Transactions of the others move to this merchant">keep</span>
                  : <button type="button" className="link-btn small nowrap"
                    onClick={() => onEdit({ target: m.key, skip: skip.filter((k) => k !== m.key) })}>keep this one</button>}
              </div>
            ))}
            {members.length > visible.length && (
              <button type="button" className="link-btn small" style={{ justifySelf: 'start' }} onClick={() => setOpen(true)}>
                and {members.length - visible.length} more…
              </button>
            )}
            {s.reason && <div className="small muted-2">{s.reason}</div>}
          </div>
        ) : <div className="small"><Member m={s.target} /></div>}
      </td>
      <td>
        <input className="input compact" style={{ width: '100%' }} aria-label="New name" value={e.name ?? s.display_name ?? ''}
          onChange={(ev) => onEdit({ name: ev.target.value })} />
      </td>
      <td className="nowrap">
        <Button variant="ghost" className="small" disabled={busy || (s.kind === 'merge' && included.length < 2 && !(e.name ?? s.display_name))}
          onClick={onAccept}>
          {s.kind === 'merge' && included.length > 1 ? `Merge ${included.length}` : 'Rename'}
        </Button>
        <Button variant="ghost" className="small" disabled={busy} onClick={onDismiss}>Dismiss</Button>
      </td>
    </tr>
  )
}

function Member({ m }: { m: ReviewMember }) {
  return (
    <span style={{ minWidth: 0 }}>
      <Link to={merchantPath(m.key)}>{m.name}</Link>
      <span className="muted-2">
        {' '}· {m.count.toLocaleString()} txn{m.count === 1 ? '' : 's'}
        {m.last_date && ` · last ${shortDate(m.last_date)} ${m.last_date.slice(2, 4)}`}
        {m.sample && <> · <span style={{ fontFamily: 'var(--font-mono, monospace)' }}>{m.sample}</span></>}
      </span>
    </span>
  )
}
