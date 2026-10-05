import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, post, type Job, type MerchantReviewState, type MerchantSuggestion, type ReviewAcceptResult, type ReviewMember } from '../api'
import { shortDate } from '../format'
import { useJob } from '../hooks'
import { merchantPath } from '../links'
import { oneOf, useKeyedState, useUrl, useUrlText } from '../urlState'
import { Button, Card, ErrorNote, ProgressBar, Seg, TableCard } from './ui'

type View = 'merge' | 'rename' | 'minor'
type Sort = 'txns' | 'merchants'
const VIEWS: View[] = ['merge', 'rename', 'minor']
type Edit = { name?: string; target?: string; skip?: string[] }
type Work = { verb: 'Applying' | 'Dismissing'; done: number; total: number; ids: Set<number> }
const PAGE = 100
const FOLD = 6
const BATCH = 20
const ACTIVE = ['queued', 'running']
const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`

const inView = (s: MerchantSuggestion, v: View) => (v === 'merge' ? s.kind === 'merge' : s.kind === 'rename' && s.minor === (v === 'minor'))
const txnCount = (s: MerchantSuggestion) => s.target.count + s.sources.reduce((n, m) => n + m.count, 0)

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
  const [params, set] = useUrl()
  const view = oneOf(params, 'view', VIEWS, 'merge')
  const sort = oneOf<Sort>(params, 'order', ['txns', 'merchants'], 'txns')
  const [q, setQ, committedQ] = useUrlText('q')
  const [limit, setLimit] = useKeyedState(`${view}|${sort}|${committedQ}`, () => PAGE)
  const [edits, setEdits] = useState<Record<number, Edit>>({})
  const [checked, setChecked] = useKeyedState(view, () => new Set<number>())
  const [message, setMessage] = useState<string | null>(null)
  const [work, setWork] = useState<Work | null>(null)
  const [workError, setWorkError] = useState<unknown>(null)

  useEffect(() => {
    if (job.data && !ACTIVE.includes(job.data.status)) qc.invalidateQueries({ queryKey: ['merchant-review'] })
  }, [job.data?.status, qc, job.data])
  useEffect(() => {
    if (!work) return
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', warn)
    return () => { window.removeEventListener('beforeunload', warn) }
  }, [work])

  const suggestions = useMemo(() => review.data?.suggestions ?? [], [review.data])
  const counts = useMemo(() => {
    const c = { merge: 0, rename: 0, minor: 0 }
    for (const s of suggestions) for (const v of ['merge', 'rename', 'minor'] as View[]) if (inView(s, v)) c[v]++
    return c
  }, [suggestions])
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const rows = suggestions.filter((s) => inView(s, view) && (!needle ||
      [s.display_name ?? '', ...[s.target, ...s.sources].flatMap((m) => [m.key, m.name, m.sample ?? ''])]
        .some((x) => x.toLowerCase().includes(needle))))
    const byTxns = (a: MerchantSuggestion, b: MerchantSuggestion) => txnCount(b) - txnCount(a)
    return rows.sort(sort === 'merchants' ? (a, b) => b.sources.length - a.sources.length || byTxns(a, b) : byTxns)
  }, [suggestions, view, q, sort])
  const shown = filtered.slice(0, limit)
  const allChecked = shown.length > 0 && shown.every((s) => checked.has(s.id))

  const run = useMutation({
    mutationFn: () => post<Job>('/merchants/review/run', { all }),
    onSuccess: (j) => { setMessage(null); setJobId(j.id); qc.invalidateQueries({ queryKey: ['merchant-review'] }) },
  })
  const busy = work !== null

  /** Send in small batches so progress shows and finished batches stick even if a later one fails. */
  const runBatches = async (items: MerchantSuggestion[], verb: Work['verb']) => {
    const total = items.length
    setMessage(null)
    setWorkError(null)
    setWork({ verb, done: 0, total, ids: new Set(items.map((s) => s.id)) })
    let done = 0, accepted = 0, dismissed = 0, moved = 0
    const errors: string[] = []
    try {
      for (let i = 0; i < total; i += BATCH) {
        const chunk = items.slice(i, i + BATCH)
        if (verb === 'Applying') {
          const r = await post<ReviewAcceptResult>('/merchants/review/accept', { items: chunk.map((s) => payload(s, edits[s.id])) })
          accepted += r.accepted
          moved += r.moved
          errors.push(...r.errors)
        } else {
          dismissed += (await post<{ dismissed: number }>('/merchants/review/dismiss', { ids: chunk.map((s) => s.id) })).dismissed
        }
        const ids = new Set(chunk.map((s) => s.id))
        qc.setQueryData<MerchantReviewState>(['merchant-review'], (d) => d && { ...d, suggestions: d.suggestions.filter((s) => !ids.has(s.id)) })
        setChecked((c) => new Set([...c].filter((id) => !ids.has(id))))
        done += chunk.length
        setWork((w) => w && { ...w, done })
      }
    } catch (e) {
      setWorkError(e)
    } finally {
      const parts = verb === 'Applying'
        ? [`Applied ${plural(accepted, 'suggestion')}`, moved ? `${plural(moved, 'transaction')} moved to their merged merchant` : '']
        : [`Dismissed ${plural(dismissed, 'suggestion')}; those merchants won't be suggested again`]
      if (done < total) parts.push(`stopped with ${total - done} not processed (still waiting below)`)
      setMessage(parts.filter(Boolean).join('; ') + (errors.length ? `. Skipped: ${errors.join('; ')}` : '.'))
      setWork(null)
      for (const k of ['merchant-review', 'transactions', 'details', 'merchants', 'analytics', 'rules']) qc.invalidateQueries({ queryKey: [k] })
    }
  }
  const accept = (items: MerchantSuggestion[]) => { void runBatches(items, 'Applying') }
  const dismiss = (items: MerchantSuggestion[]) => { void runBatches(items, 'Dismissing') }

  const edit = (id: number, patch: Partial<Edit>) => setEdits((m) => ({ ...m, [id]: { ...m[id], ...patch } }))
  const toggle = (id: number) => setChecked((s) => {
    const n = new Set(s)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    return n
  })
  const start = () => {
    const waiting = suggestions.length
    if (!all || !waiting || confirm(`Re-review everything? This replaces the ${plural(waiting, 'suggestion')} still waiting.`)) run.mutate()
  }
  const selected = suggestions.filter((s) => checked.has(s.id))

  return (
    <>
      <Card style={{ gap: 'var(--space-2)' }}>
        <div className="card-kicker">AI merchant cleanup</div>
        <div className="small">
          Gemini reads every merchant you haven't cleaned up yet (no custom name, nothing merged into it) and proposes
          <strong> merges</strong> of merchants that are the same business — store numbers, cities, card-processor prefixes —
          and <strong>cleaner names</strong>. Nothing changes until you accept. Suggestions are saved, so you can work through
          them over several visits; a new review only looks at merchants that aren't already reviewed or waiting, and adds any
          new store variants to the matching waiting suggestion.
        </div>
        <div className="row">
          <span className="text-muted small">
            {review.data ? `${plural(review.data.unreviewed, 'merchant')} not reviewed yet · ${plural(suggestions.length, 'suggestion')} waiting` : 'Loading…'}
          </span>
          <span className="spacer" />
          <label className="small row" style={{ gap: 6 }} title="Start over: replaces the waiting suggestions">
            <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> Re-review everything
          </label>
          <Button variant="primary" onClick={start} disabled={running || run.isPending || busy}>Review merchants with AI</Button>
        </div>
      </Card>

      {shownJob && (running || jobId !== null) && (
        <div className="stack">
          <div className={`callout${shownJob.status === 'failed' ? ' error' : ''}`}>
            <strong>Merchant review · {shownJob.status}</strong> {shownJob.message ?? ''}
            {shownJob.status === 'succeeded' && shownJob.result && (
              ` · ${plural(Number(shownJob.result.added ?? 0), 'new suggestion')}` +
              (shownJob.result.extended ? `, ${Number(shownJob.result.extended)} added to waiting ones` : '') +
              ` from ${plural(Number(shownJob.result.considered), 'merchant')}` +
              (shownJob.result.failed_chunks ? ` (${shownJob.result.failed_chunks} batches failed; run again to retry them)` : '')
            )}
            {shownJob.status === 'failed' && <div className="small">{shownJob.error?.split('\n')[0]}</div>}
            {!running && <Button variant="ghost" className="small" onClick={() => setJobId(null)}>Dismiss</Button>}
          </div>
          {running && <ProgressBar fraction={Number(shownJob.progress)} />}
        </div>
      )}
      {work && (
        <div className="stack">
          <div className="callout">
            <strong>{work.verb} {work.done.toLocaleString()} of {plural(work.total, 'suggestion')}…</strong>{' '}
            Finished ones drop off the list as each batch is saved; keep this page open until it's done.
          </div>
          <ProgressBar fraction={work.total ? work.done / work.total : 0} />
        </div>
      )}
      {message && (
        <div className="callout row" style={{ flexWrap: 'nowrap' }}>
          <span style={{ flex: 1 }}>{message}</span>
          <Button variant="ghost" className="small" onClick={() => setMessage(null)}>Dismiss</Button>
        </div>
      )}
      <ErrorNote error={review.error || run.error || workError} />

      <div className="row-3">
        <Seg name="review-view" value={view} onChange={(v) => set({ view: v === 'merge' ? null : v })} options={[
          { value: 'merge', label: `Merges (${counts.merge})` },
          { value: 'rename', label: `Renames (${counts.rename})` },
          { value: 'minor', label: `Capitalization only (${counts.minor})` },
        ]} />
        <input className="input compact" placeholder="Search suggestions…" style={{ width: 220 }} value={q}
          onChange={(e) => setQ(e.target.value)} />
        {view === 'merge' && (
          <Seg name="review-sort" value={sort} onChange={(v) => set({ order: v === 'txns' ? null : v })} options={[
            { value: 'txns', label: 'Most transactions' },
            { value: 'merchants', label: 'Most merchants' },
          ]} />
        )}
      </div>

      {selected.length > 0 && (
        <div className="bulk-bar">
          <strong>{selected.length} selected</strong>
          <Button variant="primary" disabled={busy} onClick={() => accept(selected)}>Accept</Button>
          <Button variant="ghost" disabled={busy} onClick={() => dismiss(selected)}>Dismiss</Button>
          <span className="spacer" />
          <Button variant="ghost" onClick={() => setChecked(new Set())}>Clear selection</Button>
        </div>
      )}

      <TableCard foot={filtered.length > shown.length && (
        <div className="table-foot">
          <span className="text-muted">Showing {shown.length} of {filtered.length.toLocaleString()}</span>
          <Button variant="ghost" onClick={() => setLimit((n) => n + PAGE * 4)}>Show more</Button>
        </div>
      )}>
        <table className="table">
          <thead>
            <tr>
              <th className="check">
                <input type="checkbox" checked={allChecked} disabled={!shown.length || busy}
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
                working={!!work?.ids.has(s.id)} onCheck={() => toggle(s.id)} onEdit={(p) => edit(s.id, p)}
                onAccept={() => accept([s])} onDismiss={() => dismiss([s])} />
            ))}
            {review.isSuccess && filtered.length === 0 && (
              <tr><td colSpan={4} className="empty text-muted">
                {suggestions.length ? 'No suggestions match.' : running ? 'Reviewing…' : 'No suggestions waiting. Run a review to get some.'}
              </td></tr>
            )}
          </tbody>
        </table>
      </TableCard>
    </>
  )
}

function SuggestionRow({ s, e, checked, busy, working, onCheck, onEdit, onAccept, onDismiss }: {
  s: MerchantSuggestion; e: Edit; checked: boolean; busy: boolean; working: boolean
  onCheck: () => void; onEdit: (p: Partial<Edit>) => void; onAccept: () => void; onDismiss: () => void
}) {
  const target = e.target ?? s.target.key
  const skip = e.skip ?? []
  const members = [s.target, ...s.sources]
  const included = members.filter((m) => m.key === target || !skip.includes(m.key))
  const [open, setOpen] = useState(false)
  const visible = open ? members : members.filter((m, i) => i < FOLD || m.key === target)
  return (
    <tr className={checked ? 'selected' : undefined} style={working ? { opacity: 0.55 } : undefined}>
      <td className="check"><input type="checkbox" checked={checked} disabled={busy} onChange={onCheck} /></td>
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
        {working ? <span className="small muted-2">Working…</span> : <>
          <Button variant="ghost" className="small" disabled={busy || (s.kind === 'merge' && included.length < 2 && !(e.name ?? s.display_name))}
            onClick={onAccept}>
            {s.kind === 'merge' && included.length > 1 ? `Merge ${included.length}` : 'Rename'}
          </Button>
          <Button variant="ghost" className="small" disabled={busy} onClick={onDismiss}>Dismiss</Button>
        </>}
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
