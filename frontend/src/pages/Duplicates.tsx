import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { get, post, type Job, type TxnPair } from '../api'
import { PairReview } from '../components/PairReview'
import { Button, Card, ErrorNote, ProgressBar } from '../components/ui'
import { shortDate } from '../format'
import { useJob } from '../hooks'
import { briefFacts, pairConfidence } from '../review'

export function Duplicates() {
  const qc = useQueryClient()
  const [searchParams] = useSearchParams()
  const [jobId, setJobId] = useState<number | null>(searchParams.get('job') ? Number(searchParams.get('job')) : null)
  const job = useJob(jobId)
  const pairs = useQuery({ queryKey: ['duplicates', 'pending'], queryFn: () => get<TxnPair[]>('/duplicates') })
  const kept = useQuery({
    queryKey: ['duplicates', 'separate'],
    queryFn: () => get<TxnPair[]>('/duplicates', { status: 'confirmed_separate', limit: 1000 }),
  })
  const scan = useMutation({ mutationFn: () => post<Job>('/duplicates/scan', {}), onSuccess: (j) => setJobId(j.id) })
  const decide = useMutation({
    mutationFn: (v: { id: number; decision: 'duplicate' | 'separate'; keep_id?: number }) =>
      post(`/duplicates/${v.id}/decide`, v),
    onSuccess: () => {
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
  const cur = list[0]

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
      {cur ? (
        <PairReview
          kicker={`Candidate 1 of ${list.length}`}
          title={`${cur.a.description} · ${shortDate(cur.a.txn_date)}${cur.a.txn_date !== cur.b.txn_date ? ` vs ${shortDate(cur.b.txn_date)}` : ''}`}
          confidence={pairConfidence(cur.score, cur.ai_probability)}
          left={{ kicker: `Transaction #${cur.a.id}`, description: cur.a.description, amount: cur.a.amount, facts: briefFacts(cur.a) }}
          right={{ kicker: `Transaction #${cur.b.id}`, description: cur.b.description, amount: cur.b.amount, facts: briefFacts(cur.b) }}
          reason={cur.ai_reason ? `${cur.ai_reason} (${cur.reasons.join(' · ')})` : cur.reasons.join(' · ')}
          note="Removing merges notes, tags and category into the one you keep."
          actions={<>
            <Button variant="primary" disabled={decide.isPending}
              onClick={() => decide.mutate({ id: cur.id, decision: 'duplicate', keep_id: cur.a.id })}>Same — keep left</Button>
            <Button disabled={decide.isPending}
              onClick={() => decide.mutate({ id: cur.id, decision: 'duplicate', keep_id: cur.b.id })}>Same — keep right</Button>
            <Button disabled={decide.isPending} onClick={() => decide.mutate({ id: cur.id, decision: 'separate' })}>
              Different — keep both
            </Button>
          </>}
        />
      ) : (
        !pairs.isLoading && (
          <Card style={{ maxWidth: 560 }}>
            <div className="card-kicker">All clear</div>
            <div className="card-title">No open duplicate candidates</div>
            <p className="card-body">Run a scan after large imports or source syncs. Pairs you mark as different are remembered.</p>
          </Card>
        )
      )}
    </section>
  )
}
