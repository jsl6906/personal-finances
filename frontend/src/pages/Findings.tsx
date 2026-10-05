import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ANOMALY_LABEL, post, type Anomaly, type Job } from '../api'
import { AnomalyList } from '../components/AnomalyList'
import { Button, Card, ErrorNote, Seg } from '../components/ui'
import { monthLabel, parseIso } from '../format'
import { useAnomalies, useJob } from '../hooks'
import { oneOf, useUrl } from '../urlState'

type Status = 'open' | 'reviewed' | 'dismissed' | 'withdrawn'
const STATUSES: Status[] = ['open', 'reviewed', 'dismissed', 'withdrawn']

export function Findings() {
  const qc = useQueryClient()
  const [params, set] = useUrl()
  const status = oneOf<Status>(params, 'status', STATUSES, 'open')
  const kind = params.get('kind') ?? 'all'
  const setFilter = (k: 'status' | 'kind', v: string, dflt: string) =>
    set({ [k]: v === dflt ? null : v, ...(k === 'status' ? { kind: null } : {}) })
  const anomalies = useAnomalies(status)
  const open = useAnomalies('open')
  const [jobId, setJobId] = useState<number | null>(null)
  const job = useJob(jobId)
  const run = useMutation({ mutationFn: () => post<Job>('/anomalies/run'), onSuccess: (j) => setJobId(j.id) })
  const done = job.data?.status === 'succeeded' || job.data?.status === 'failed'
  const checking = jobId !== null && !done
  useEffect(() => {
    if (done) qc.invalidateQueries({ queryKey: ['anomalies'] })
  }, [done, qc])

  const all = anomalies.data ?? []
  const counts = new Map<string, number>()
  for (const a of all) counts.set(a.kind, (counts.get(a.kind) ?? 0) + 1)
  const items = kind === 'all' ? all : all.filter((a) => a.kind === kind)
  const byPeriod = new Map<string, Anomaly[]>()
  for (const a of items) byPeriod.set(a.period, [...(byPeriod.get(a.period) ?? []), a])

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <h2>Findings</h2>
          <div className="text-muted subtitle">
            Activity that looks out of the ordinary · {open.data ? `${open.data.length} open` : '…'}
            {job.data?.status === 'failed' && ' · last check failed'}
          </div>
        </div>
        <div className="row">
          <Button onClick={() => run.mutate()} disabled={run.isPending || checking}>
            {checking ? 'Checking…' : 'Run check now'}
          </Button>
        </div>
      </header>
      <ErrorNote error={anomalies.error || run.error} />

      <div className="row" style={{ justifyContent: 'space-between' }}>
        <Seg name="fstatus" value={status} onChange={(v) => setFilter('status', v, 'open')} options={[
          { value: 'open', label: 'Open' }, { value: 'reviewed', label: 'Reviewed' }, { value: 'dismissed', label: 'Dismissed' },
          { value: 'withdrawn', label: 'Withdrawn' },
        ]} />
        <Seg name="fkind" value={kind} onChange={(v) => setFilter('kind', v, 'all')} options={[
          { value: 'all', label: `All ${all.length}` },
          ...Object.keys(ANOMALY_LABEL).filter((k) => counts.has(k) || k === kind)
            .map((k) => ({ value: k, label: `${ANOMALY_LABEL[k]} ${counts.get(k) ?? 0}` })),
        ]} />
      </div>

      {[...byPeriod.entries()].map(([period, list]) => (
        <Card key={period}>
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
            <div className="card-title">{monthLabel(parseIso(period))}</div>
            <span className="small text-muted">{list.length} finding{list.length === 1 ? '' : 's'}</span>
          </div>
          <AnomalyList items={list} />
        </Card>
      ))}
      {anomalies.data && items.length === 0 && (
        <Card><AnomalyList items={[]} /></Card>
      )}

      <div className="card-meta">Checks run nightly and after each import: category spikes vs. 12-month norm, unusually large purchases for a
        merchant, large one-off transactions, first purchases at new merchants, bill increases, and transfers with no matching leg.
        Findings that a later check no longer confirms (e.g. after re-categorizing) move to Withdrawn.</div>
    </section>
  )
}
