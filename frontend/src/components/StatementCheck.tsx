import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import {
  get, post, type BatchDetail, type CheckIssue, type CheckStatus, type Job, type StatementCheck, type StatementCheckList,
} from '../api'
import { fullDate, money, shortDate } from '../format'
import { useJob } from '../hooks'
import { accountPath, txnPath } from '../links'
import { Button, Card, ErrorNote, ProgressBar, Seg } from './ui'

const STATUS_LABEL: Record<CheckStatus, string> = {
  ok: 'Matches', explained: 'Explained', mismatch: 'Differences', unverified: 'Not checked',
}
const STATUS_CLASS: Record<CheckStatus, string> = {
  ok: 'tag-accent', explained: 'tag-accent-2', mismatch: 'tag-outline', unverified: 'tag-neutral',
}
const KIND_LABEL: Record<CheckIssue['kind'], string> = {
  missing: 'Missing from ledger', extra: 'Not on statement', amount: 'Amount differs', link: 'Already in ledger',
  edge: 'Near period edge', elsewhere: 'In another account',
}
const FIX_LABEL: Record<string, string> = {
  add: 'Add to ledger', remove: 'Remove from ledger', amount: 'Use statement amount', link: 'Link, don’t add',
}

export function CheckStatusTag({ status }: { status: CheckStatus }) {
  return <span className={`tag ${STATUS_CLASS[status]}`}>{STATUS_LABEL[status]}</span>
}

const keyOf = (i: CheckIssue) => `${i.fix}:${i.row_id ?? ''}:${i.transaction_id ?? ''}`

/** Statement vs. ledger for each account on a statement, with fixes to apply (import review step). */
export function StatementChecks({ b }: { b: BatchDetail }) {
  const qc = useQueryClient()
  const checks = useQuery({
    queryKey: ['import', b.id, 'checks'],
    queryFn: () => get<StatementCheck[]>(`/imports/${b.id}/checks`),
  })
  if (checks.isLoading) return <Card><div className="card-meta">Comparing the statement with the ledger…</div></Card>
  if (checks.error) return <ErrorNote error={checks.error} />
  const list = checks.data ?? []
  if (!list.length) return null
  const refresh = (next: StatementCheck[]) => {
    qc.setQueryData(['import', b.id, 'checks'], next)
    qc.invalidateQueries({ queryKey: ['import', b.id], predicate: (q) => q.queryKey[2] !== 'checks' })
    qc.invalidateQueries({ queryKey: ['transactions'] })
    qc.invalidateQueries({ queryKey: ['summary'] })
    qc.invalidateQueries({ queryKey: ['statement-checks'] })
  }
  return (
    <div className="stack-3">
      {list.map((c) => <AccountCheck key={c.account_ref} b={b} c={c} multi={list.length > 1} onApplied={refresh} />)}
    </div>
  )
}

function AccountCheck({ b, c, multi, onApplied }: {
  b: BatchDetail; c: StatementCheck; multi: boolean; onApplied: (next: StatementCheck[]) => void
}) {
  const [picked, setPicked] = useState<Map<string, boolean>>(new Map())
  const apply = useMutation({
    mutationFn: (fixes: CheckIssue[]) => post<{ applied: number; skipped: number; checks: StatementCheck[] }>(
      `/imports/${b.id}/checks/fix`,
      { fixes: fixes.map((i) => ({ fix: i.fix, row_id: i.row_id, transaction_id: i.transaction_id })) },
    ),
    onSuccess: (r) => {
      setPicked(new Map())
      onApplied(r.checks)
    },
  })
  const issues = c.detail.issues
  const fixable = issues.filter((i) => i.fix)
  const isOn = (i: CheckIssue) => !!i.fix && (picked.get(keyOf(i)) ?? i.suggested)
  const chosen = fixable.filter(isOn)
  const change = chosen.reduce((s, i) => s + Number(i.effect), 0)
  const remaining = Number(c.difference ?? 0) - change
  const setAll = (on: boolean, which = fixable) => setPicked(new Map(which.map((i) => [keyOf(i), on])))
  const label = c.account_name ?? (c.account_ref ? `···${c.account_ref}` : 'Statement account')
  const shifted = c.detail.shifted.length
  const before = b.status === 'review'

  return (
    <Card style={{ gap: 'var(--space-3)' }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline', flexWrap: 'wrap' }}>
        <div>
          <div className="card-kicker">Statement check{multi && c.account_ref ? ` · ···${c.account_ref}` : ''}</div>
          <div className="card-title">
            {c.account_id ? <Link to={accountPath(c.account_id)}>{label}</Link> : label}
          </div>
        </div>
        <CheckStatusTag status={c.status} />
      </div>
      {c.status !== 'unverified' && (
        <div className="grid-3">
          <div>
            <div className="stat-n">{money(c.statement_total)}</div>
            <div className="text-muted small">statement rows · {c.statement_rows}</div>
          </div>
          <div>
            <div className="stat-n">{money(c.ledger_total)}</div>
            <div className="text-muted small">{before ? 'ledger after commit' : 'ledger'} · {c.ledger_rows} transactions</div>
          </div>
          <div>
            <div className="stat-n">{money(c.difference)}</div>
            <div className="text-muted small">
              difference · {c.period_start ? `${shortDate(c.period_start)} – ${shortDate(c.period_end)}` : 'no period'}
              {c.detail.period_source === 'rows' ? ' (from row dates)' : ''}
            </div>
          </div>
        </div>
      )}
      <div className="small">
        {c.status === 'ok' && 'The ledger holds exactly the statement’s transactions for this period.'}
        {c.status === 'explained' && 'Totals differ only by transactions near the period edges, listed on another'
          + ' statement, or recorded in another account — nothing needs fixing.'}
        {c.status === 'mismatch' && 'The ledger doesn’t match the statement. The statement is treated as the record:'
          + ' suggested fixes are pre-selected; review them and apply.'}
        {shifted > 0 && ` ${shifted} matched transaction${shifted === 1 ? ' is' : 's are'} dated just outside the period.`}
      </div>
      {c.detail.message && <div className="callout">{c.detail.message}</div>}
      {c.trusted === false && (
        <div className="callout">The statement’s rows don’t add up to its own balance change, so its rows may be
          misread; nothing is pre-selected.</div>
      )}
      {issues.length > 0 && (
        <>
          {fixable.length > 0 && (
            <div className="bulk-bar">
              <strong>{chosen.length} of {fixable.length} fixes selected</strong>
              <Button variant="ghost" onClick={() => setPicked(new Map())}>Suggested</Button>
              <Button variant="ghost" onClick={() => setAll(true)}>All</Button>
              <Button variant="ghost" onClick={() => setAll(false)}>None</Button>
            </div>
          )}
          <div style={{ maxHeight: 520, overflow: 'auto' }}>
            <table className="table" style={{ fontSize: 13 }}>
              <thead>
                <tr>
                  <th className="check" /><th>Difference</th><th>On the statement</th><th>In the ledger</th>
                  <th style={{ textAlign: 'right' }}>Effect</th><th>Fix</th>
                </tr>
              </thead>
              <tbody>
                {issues.map((i) => (
                  <tr key={`${i.kind}:${keyOf(i)}`} className={isOn(i) ? 'selected' : undefined}>
                    <td className="check">
                      {i.fix && (
                        <input type="checkbox" checked={isOn(i)}
                          onChange={() => setPicked((m) => new Map(m).set(keyOf(i), !isOn(i)))} />
                      )}
                    </td>
                    <td className="nowrap">
                      <div style={{ fontWeight: 500 }}>{KIND_LABEL[i.kind]}</div>
                      {i.hint && <div className="small text-muted" style={{ maxWidth: 320, whiteSpace: 'normal' }}>{i.hint}</div>}
                    </td>
                    <td>
                      {i.row ? (
                        <>
                          <div>{i.row.description}</div>
                          <div className="small text-muted nowrap">
                            Row {i.row.row_index + 1} · {shortDate(i.row.date)} · {money(i.row.amount)}
                          </div>
                        </>
                      ) : <span className="text-muted">—</span>}
                    </td>
                    <td>
                      {i.txn ? (
                        <>
                          <Link to={txnPath(i.txn.id)}>{i.txn.description}</Link>
                          <div className="small text-muted nowrap">
                            {shortDate(i.txn.date)} · {money(i.txn.amount)} · via {i.txn.source_type}
                          </div>
                        </>
                      ) : <span className="text-muted">—</span>}
                    </td>
                    <td className="num nowrap">{Number(i.effect) ? money(i.effect, true) : '—'}</td>
                    <td className="nowrap small">{i.fix ? FIX_LABEL[i.fix] : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {fixable.length > 0 && (
            <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap' }}>
              <span className="small text-muted">
                Selected fixes change the ledger by {money(change, true)}; remaining difference {money(remaining)}.
                {before ? ' Adds and links adjust this import’s rows;' : ''} removals and amount corrections change
                existing transactions now and leave a note on them.
              </span>
              <Button variant="primary" disabled={!chosen.length || apply.isPending}
                onClick={() => confirm(`Apply ${chosen.length} fix${chosen.length === 1 ? '' : 'es'} to the ledger?`)
                  && apply.mutate(chosen)}>
                Apply {chosen.length} fix{chosen.length === 1 ? '' : 'es'}
              </Button>
            </div>
          )}
          {apply.data && apply.data.skipped > 0 && (
            <div className="small text-muted">{apply.data.skipped} fixes no longer applied and were skipped.</div>
          )}
        </>
      )}
      <ErrorNote error={apply.error} />
    </Card>
  )
}

type Filter = CheckStatus | 'all'

/** Every committed statement's check, problems first, with a button to re-check them all. */
export function StatementChecksPanel() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [filter, setFilter] = useState<Filter>('mismatch')
  const list = useQuery({
    queryKey: ['statement-checks', filter],
    queryFn: () => get<StatementCheckList>('/statement-checks', { status: filter === 'all' ? undefined : filter }),
  })
  const [jobId, setJobId] = useState<number | null>(null)
  const last = list.data?.job
  const activeId = jobId ?? (last && ['queued', 'running'].includes(last.status) ? last.id : null)
  const job = useJob(activeId)
  const running = !!job.data && ['queued', 'running'].includes(job.data.status)
  const finished = job.data && !running ? job.data.id : null
  useEffect(() => {
    if (finished !== null) {
      qc.invalidateQueries({ queryKey: ['statement-checks'] })
    }
  }, [finished, qc])
  const run = useMutation({
    mutationFn: () => post<Job>('/statement-checks/run'),
    onSuccess: (j) => setJobId(j.id),
  })
  const s = list.data?.summary ?? {}
  const total = (s.ok ?? 0) + (s.explained ?? 0) + (s.mismatch ?? 0) + (s.unverified ?? 0)
  const items = list.data?.items ?? []

  return (
    <Card style={{ gap: 'var(--space-3)' }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline', flexWrap: 'wrap', gap: 'var(--space-3)' }}>
        <div>
          <div className="card-kicker">Statement checks</div>
          <div className="card-title">Official statements vs. the ledger</div>
          <div className="small text-muted" style={{ maxWidth: '70ch' }}>
            For each statement, the ledger’s transactions for that account over the statement period are totalled and
            compared with the statement’s rows. When they differ, the statement is trusted and fixes are proposed.
            {list.data?.statements ? ` ${list.data.statements} statements · ${total} account checks (combined statements`
              + ' get one per account).' : ''}
            {last?.finished_at ? ` Last full check ${fullDate(last.finished_at)}.` : ''}
            {list.data?.unchecked ? ` ${list.data.unchecked} statements haven’t been checked yet.` : ''}
          </div>
        </div>
        <Button disabled={running || run.isPending} onClick={() => run.mutate()}>
          {running ? 'Checking…' : 'Check all statements'}
        </Button>
      </div>
      {running && (
        <div className="stack" style={{ gap: 4 }}>
          <ProgressBar fraction={Number(job.data?.progress ?? 0.02)} />
          <div className="card-meta">{job.data?.message ?? 'Queued'}</div>
        </div>
      )}
      <Seg<Filter> name="checkfilter" value={filter} onChange={setFilter}
        options={[
          { value: 'mismatch', label: `Differences (${s.mismatch ?? 0})` },
          { value: 'explained', label: `Explained (${s.explained ?? 0})` },
          { value: 'ok', label: `Matches (${s.ok ?? 0})` },
          { value: 'unverified', label: `Not checked (${s.unverified ?? 0})` },
          { value: 'all', label: `All accounts (${total})` },
        ]} />
      <div style={{ maxHeight: 560, overflow: 'auto' }}>
        <table className="table" style={{ fontSize: 13 }}>
          <thead>
            <tr>
              <th>Statement</th><th>Account</th><th>Period</th><th style={{ textAlign: 'right' }}>Statement</th>
              <th style={{ textAlign: 'right' }}>Ledger</th><th style={{ textAlign: 'right' }}>Difference</th><th>Findings</th>
            </tr>
          </thead>
          <tbody>
            {items.map((c) => (
              <tr key={`${c.import_batch_id}:${c.account_ref}`} className="clickable"
                onClick={() => navigate(`/import/${c.import_batch_id}`)}>
                <td>{c.filename ?? `Import #${c.import_batch_id}`}</td>
                <td className="nowrap">{c.account_name ?? (c.account_ref ? `···${c.account_ref}` : '—')}</td>
                <td className="nowrap text-muted">{c.period_start ? `${shortDate(c.period_start)} – ${shortDate(c.period_end)}` : '—'}</td>
                <td className="num nowrap">{money(c.statement_total)}</td>
                <td className="num nowrap">{money(c.ledger_total)}</td>
                <td className="num nowrap">{money(c.difference)}</td>
                <td className="small">
                  <CheckStatusTag status={c.status} />{' '}
                  {Object.entries(c.issue_counts).map(([k, n]) => `${n} ${KIND_LABEL[k as CheckIssue['kind']]?.toLowerCase() ?? k}`).join(' · ')}
                  {c.message && <div className="text-muted">{c.message}</div>}
                </td>
              </tr>
            ))}
            {!list.isLoading && !items.length && (
              <tr><td colSpan={7} className="text-muted">
                {total ? 'Nothing in this view.' : 'No statements checked yet — run “Check all statements”.'}
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
      <ErrorNote error={list.error || run.error} />
    </Card>
  )
}
