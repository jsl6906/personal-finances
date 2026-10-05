import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, post, type Job, type TxnBrief, type TxnPair } from '../api'
import { Button, Card, ErrorNote, ProgressBar, Seg, SortTh } from '../components/ui'
import { money, shortDate } from '../format'
import { useJob } from '../hooks'
import { txnPath } from '../links'
import { pairConfidence } from '../review'
import { numParam, oneOf, sortRows, useKeyedState, useUrl, useUrlSort } from '../urlState'

type Decision = 'keep_a' | 'keep_b' | 'separate'
type Filter = 'likely' | 'unsure' | 'unlikely' | 'all' | 'kept'
const FILTERS: Filter[] = ['likely', 'unsure', 'unlikely', 'all', 'kept']

const likelihood = (p: TxnPair) => Number(p.ai_probability ?? p.score)
const MATCHES: Record<Exclude<Filter, 'all' | 'kept'>, (p: TxnPair) => boolean> = {
  likely: (p) => likelihood(p) >= 0.8,
  unsure: (p) => likelihood(p) > 0.3 && likelihood(p) < 0.8,
  unlikely: (p) => likelihood(p) <= 0.3,
}

function Side({ t, bold }: { t: TxnBrief; bold: boolean }) {
  return (
    <>
      <Link to={txnPath(t.id)} style={bold ? { fontWeight: 500 } : undefined}>{t.description}</Link>
      <div className="small text-muted">
        {shortDate(t.txn_date)} · {t.account_name ?? 'No account'} · {money(t.amount)} · via {t.source_type}
        {t.category_name ? ` · ${t.category_name}` : ''}
      </div>
    </>
  )
}

export function Duplicates() {
  const qc = useQueryClient()
  const [params, set] = useUrl()
  const [jobId, setJobId] = useState<number | null>(numParam(params, 'job'))
  const job = useJob(jobId)
  const pairs = useQuery({ queryKey: ['duplicates', 'pending'], queryFn: () => get<TxnPair[]>('/duplicates', { limit: 1000 }) })
  const kept = useQuery({
    queryKey: ['duplicates', 'separate'],
    queryFn: () => get<TxnPair[]>('/duplicates', { status: 'confirmed_separate', limit: 1000 }),
  })
  const filter: Filter | null = params.get('view') ? oneOf(params, 'view', FILTERS, 'all') : null
  const setFilter = (v: Filter) => set({ view: v })
  const sort = useUrlSort({ date: 'desc', match: 'desc' })
  const scan = useMutation({ mutationFn: () => post<Job>('/duplicates/scan', {}), onSuccess: (j) => setJobId(j.id) })
  const decide = useMutation({
    mutationFn: ({ ids, decision }: { ids: number[]; decision: Decision }) =>
      post<{ decided: number; skipped: number }>('/duplicates/decisions', { pair_ids: ids, decision }),
    onSuccess: () => {
      setChecked(new Set())
      qc.invalidateQueries({ queryKey: ['duplicates'] })
      qc.invalidateQueries({ queryKey: ['transactions'] })
    },
  })
  const running = job.data && ['queued', 'running'].includes(job.data.status)
  const finished = job.data?.status === 'succeeded'
  useEffect(() => {
    if (finished) qc.invalidateQueries({ queryKey: ['duplicates'] })
  }, [finished, qc])

  const list = pairs.data ?? []
  const counts = { likely: 0, unsure: 0, unlikely: 0 }
  for (const p of list) for (const k of Object.keys(MATCHES) as (keyof typeof MATCHES)[]) if (MATCHES[k](p)) counts[k]++
  const active: Filter = filter ?? (counts.likely ? 'likely' : 'all')
  const [checked, setChecked] = useKeyedState(active, () => new Set<number>())
  const showKept = active === 'kept'
  const shown = sortRows(showKept ? kept.data ?? [] : active === 'all' ? list : list.filter(MATCHES[active]), sort, {
    date: (p) => p.a.txn_date, match: likelihood,
  })
  const shownIds = showKept ? [] : shown.map((p) => p.id)
  const allChecked = shownIds.length > 0 && shownIds.every((id) => checked.has(id))
  const toggle = (id: number) => setChecked((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n })
  const act = (ids: number[], decision: Decision) => {
    if (!ids.length) return
    const what = decision === 'separate' ? 'Keep both transactions of' : 'Merge'
    if (ids.length > 1 && !confirm(`${what} ${ids.length} pairs?`)) return
    decide.mutate({ ids, decision })
  }
  const busy = decide.isPending

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <h2>Duplicate review</h2>
          <div className="text-muted subtitle">
            {list.length} open candidates · {kept.data?.length ?? 0} confirmed-separate pairs kept and never re-flagged
          </div>
        </div>
        <Button variant="primary" onClick={() => scan.mutate()} disabled={scan.isPending || !!running}>Scan for duplicates</Button>
      </header>
      {job.data && (
        <div className="stack">
          <div className={`callout${job.data.status === 'failed' ? ' error' : ''}`}>
            <strong>Duplicate scan · {job.data.status}</strong>{' '}
            {job.data.result && `${job.data.result.new_candidates} new candidates · ${job.data.result.confirmed_separate_kept} confirmed-separate pairs kept`}
            {job.data.status === 'failed' && <div className="small">{job.data.error?.split('\n')[0]}</div>}
          </div>
          {running && <ProgressBar fraction={Number(job.data.progress)} />}
        </div>
      )}
      <ErrorNote error={scan.error || decide.error || pairs.error} />

      {!pairs.isLoading && !list.length && !showKept ? (
        <Card style={{ maxWidth: 560 }}>
          <div className="card-kicker">All clear</div>
          <div className="card-title">No open duplicate candidates</div>
          <p className="card-body">Run a scan after large imports or source syncs. Pairs you mark as different are remembered.</p>
          {(kept.data?.length ?? 0) > 0 && <Button onClick={() => setFilter('kept')}>Show kept-separate pairs</Button>}
        </Card>
      ) : (
        <div className="stack-3">
          <div className="row" style={{ alignItems: 'baseline', gap: 'var(--space-3)', flexWrap: 'wrap' }}>
            <div className="small text-muted" style={{ maxWidth: '70ch' }}>
              Merging removes one copy and moves its notes, tags, sources and category onto the one kept.
              “Keep both” is remembered; the pair won't be flagged again.
              {decide.data && ` Last action: ${decide.data.decided} decided${decide.data.skipped
                ? `, ${decide.data.skipped} skipped (a transaction in them was already merged away)` : ''}.`}
            </div>
            <span className="spacer" />
            <Seg<Filter> name="dupfilter" value={active} onChange={setFilter}
              options={[
                { value: 'likely', label: `Likely same (${counts.likely})` },
                { value: 'unsure', label: `Unsure (${counts.unsure})` },
                { value: 'unlikely', label: `Likely different (${counts.unlikely})` },
                { value: 'all', label: `All open (${list.length})` },
                { value: 'kept', label: `Kept separate (${kept.data?.length ?? 0})` },
              ]} />
          </div>

          {!showKept && (
            <div className="bulk-bar">
              {checked.size > 0 ? (
                <>
                  <strong>{checked.size} selected</strong>
                  <Button variant="primary" disabled={busy} onClick={() => act([...checked], 'keep_a')}>Same — keep left</Button>
                  <Button disabled={busy} onClick={() => act([...checked], 'keep_b')}>Same — keep right</Button>
                  <Button disabled={busy} onClick={() => act([...checked], 'separate')}>Different — keep both</Button>
                  <span className="spacer" />
                  <Button variant="ghost" onClick={() => setChecked(new Set())}>Clear selection</Button>
                </>
              ) : (
                <>
                  <span>Select pairs to decide them together, or apply to all {shownIds.length} shown:</span>
                  <Button disabled={busy || !shownIds.length} onClick={() => act(shownIds, 'keep_a')}>Merge all shown (keep left)</Button>
                  <Button disabled={busy || !shownIds.length} onClick={() => act(shownIds, 'separate')}>Keep all shown</Button>
                </>
              )}
            </div>
          )}

          <Card className="table-card">
            <table className="table" style={{ fontSize: 13 }}>
              <thead>
                <tr>
                  {!showKept && (
                    <th className="check">
                      <input type="checkbox" checked={allChecked}
                        onChange={() => setChecked(allChecked ? new Set() : new Set(shownIds))} />
                    </th>
                  )}
                  <SortTh s={sort} k="date" title="Sort by date">Left</SortTh><th>Right</th>
                  <SortTh s={sort} k="match">Match</SortTh>{!showKept && <th>Decide</th>}
                </tr>
              </thead>
              <tbody>
                {shown.map((p) => {
                  const reason = p.ai_reason ? `${p.ai_reason} (${p.reasons.join(' · ')})` : p.reasons.join(' · ')
                  const differ = p.a.description.trim().toLowerCase() !== p.b.description.trim().toLowerCase()
                  return (
                    <tr key={p.id} className={checked.has(p.id) ? 'selected' : undefined}>
                      {!showKept && (
                        <td className="check"><input type="checkbox" checked={checked.has(p.id)} onChange={() => toggle(p.id)} /></td>
                      )}
                      <td><Side t={p.a} bold={false} /></td>
                      <td><Side t={p.b} bold={differ} /></td>
                      <td className="small" title={reason}>
                        <div className="nowrap">{pairConfidence(p.score, p.ai_probability)}</div>
                        <div className="text-muted" style={{ maxWidth: 240 }}>{reason}</div>
                      </td>
                      {!showKept && (
                        <td className="nowrap">
                          <select className="input" style={{ minWidth: 150 }} value="" disabled={busy}
                            onChange={(e) => e.target.value && act([p.id], e.target.value as Decision)}>
                            <option value="">Undecided</option>
                            <option value="keep_a">Same — keep left</option>
                            <option value="keep_b">Same — keep right</option>
                            <option value="separate">Different — keep both</option>
                          </select>
                        </td>
                      )}
                    </tr>
                  )
                })}
                {!shown.length && <tr><td colSpan={5} className="text-muted">Nothing in this view.</td></tr>}
              </tbody>
            </table>
          </Card>
        </div>
      )}
    </section>
  )
}
