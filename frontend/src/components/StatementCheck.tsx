import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import {
  get, post, type BatchDetail, type CheckIssue, type CheckStatus, type Job, type StatementCheck, type StatementCheckList,
  type StatementTimelineItem,
} from '../api'
import { fullDate, money, shortDate } from '../format'
import { useJob } from '../hooks'
import { accountPath, txnPath } from '../links'
import { oneOf, sortRows, useUrl, useUrlSort } from '../urlState'
import { Button, Card, ErrorNote, ProgressBar, Seg, SortTh, TableCard } from './ui'

const STATUS_LABEL: Record<CheckStatus, string> = {
  ok: 'Matches', explained: 'Explained', mismatch: 'Differences', unverified: 'Not checked',
}
const STATUS_CLASS: Record<CheckStatus, string> = {
  ok: 'tag-accent', explained: 'tag-accent-2', mismatch: 'tag-outline', unverified: 'tag-neutral',
}
const KIND_LABEL: Record<CheckIssue['kind'], string> = {
  missing: 'Missing from ledger', extra: 'Not on statement', amount: 'Amount differs', link: 'Already in ledger',
  edge: 'Near period edge', listed: 'On another statement', elsewhere: 'In another account',
}
const FIX_LABEL: Record<string, string> = {
  add: 'Add to ledger', remove: 'Remove from ledger', amount: 'Use statement amount', link: 'Link, don’t add',
  date: 'Use that statement’s date',
}

export function CheckStatusTag({ status }: { status: CheckStatus }) {
  return <span className={`tag ${STATUS_CLASS[status]}`}>{STATUS_LABEL[status]}</span>
}

const keyOf = (i: CheckIssue) => `${i.fix}:${i.row_id ?? ''}:${i.transaction_id ?? ''}`
// Mirrors coverage.AUTO_CONFIDENCE: suggested fixes this confident are applied when a statement is committed.
const AUTO_CONFIDENCE = 90
const isAuto = (i: CheckIssue) => !!i.fix && i.suggested && (i.confidence ?? 0) >= AUTO_CONFIDENCE

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
  const listed = c.detail.listed_elsewhere?.count ?? 0
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
          + ' statement, or recorded in another account'
          + (fixable.some((i) => i.fix === 'date') ? '; where another statement dates one differently, its date is offered as a fix.'
            : ' — nothing needs fixing.')}
        {c.status === 'mismatch' && 'The ledger doesn’t match the statement. The statement is treated as the record:'
          + ' suggested fixes are pre-selected; review them and apply.'}
        {(shifted > 0 || listed > 0) && ` The ledger total ${[
          shifted > 0 && `counts ${shifted} matched transaction${shifted === 1 ? '' : 's'} dated just outside the period`,
          listed > 0 && `leaves out ${listed} listed on another statement (${money(c.detail.listed_elsewhere?.total ?? 0)})`,
        ].filter(Boolean).join(' and ')}.`}
      </div>
      {c.detail.message && (
        <div className="callout">
          {c.detail.message}
          {c.detail.same_file?.map((id) => <span key={id}> · <Link to={`/import/${id}`}>Open import #{id}</Link></span>)}
        </div>
      )}
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
                            Row {i.row.row_index + 1}{i.row.statement ? ` of ${i.row.statement}` : ''}
                            {' · '}{shortDate(i.row.date)} · {money(i.row.amount)}
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
                    <td className="nowrap small">
                      {i.fix ? FIX_LABEL[i.fix] : '—'}
                      {i.fix && i.confidence != null && (
                        <div className="text-muted" title={isAuto(i) ? 'Confident enough to apply without review' : undefined}>
                          {i.confidence}% confident{isAuto(i) ? ' · auto' : ''}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {fixable.length > 0 && (
            <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap' }}>
              <span className="small text-muted">
                Selected fixes change the ledger by {money(change, true)}; remaining difference {money(remaining)}.
                {before ? ' Adds and links adjust this import’s rows;' : ''} removals, amount and date corrections change
                existing transactions now and leave a note on them.
                {before && fixable.some(isAuto)
                  ? ` Fixes marked “auto” (${AUTO_CONFIDENCE}%+) are applied anyway when the statement is committed.` : ''}
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
const FILTERS: Filter[] = ['mismatch', 'explained', 'ok', 'unverified', 'all']
const amt = (v: string | null) => (v === null ? null : Number(v))
const absAmt = (v: string | null) => (v === null ? null : Math.abs(Number(v)))

const TIMELINE_LABEL: Record<Exclude<StatementTimelineItem['kind'], 'statement'>, string> = {
  review: 'Being imported', queued: 'In the backfill', missing: 'Missing',
}

const periodText = (s: string | null, e: string | null) =>
  s && e ? `${shortDate(s)} – ${fullDate(e)}` : s ? `From ${fullDate(s)}` : e ? `To ${fullDate(e)}` : 'Period unknown'

/** One account's statements: period, the rows' total and the ledger's total for that period, plus highlighted
 * placeholders for statements still being imported or apparently missing. */
export function AccountStatements({ accountId }: { accountId: number }) {
  const navigate = useNavigate()
  const s = useUrlSort({ period: 'desc', statement: 'asc', stmt: 'desc', ledger: 'desc', diff: 'desc', check: 'asc' }, null, 'ssort')
  const list = useQuery({
    queryKey: ['statement-checks', 'timeline', accountId],
    queryFn: () => get<StatementTimelineItem[]>('/statement-checks/timeline', { account_id: accountId }),
  })
  const items = list.data ?? []
  if (list.error) return <ErrorNote error={list.error} />
  if (!items.length) return null
  const sorted = sortRows(items, s, {
    period: (i) => i.period_end ?? i.period_start, statement: (i) => i.filename ?? (i.import_batch_id ? `Import #${i.import_batch_id}` : null),
    stmt: (i) => amt(i.statement_total), ledger: (i) => amt(i.ledger_total), diff: (i) => absAmt(i.difference),
    check: (i) => (i.kind === 'statement' ? i.status : i.kind),
  })
  const count = (k: StatementTimelineItem['kind']) => items.filter((i) => i.kind === k).length
  const missing = items.filter((i) => i.kind === 'missing').reduce((n, i) => n + (i.estimated ?? 1), 0)
  const inProgress = count('review') + count('queued')
  const open = (i: StatementTimelineItem) => {
    if (i.import_batch_id) navigate(`/import/${i.import_batch_id}`)
    else if (i.backfill_file_id) navigate('/sources')
  }
  return (
    <TableCard head={
      <div style={{ padding: 'var(--space-3) var(--space-3) 0' }}>
        <div className="card-kicker">Statements</div>
        <div className="card-meta">
          {count('statement')} imported
          {inProgress ? ` · ${inProgress} in progress` : ''}
          {missing ? ` · about ${missing} missing` : ''}. Totals are the net of the statement’s rows and of the
          ledger’s transactions for this account over the same period. Gaps are judged from how often statements close.
        </div>
      </div>
    }>
      <div style={{ maxHeight: 480, overflow: 'auto' }}>
        <table className="table">
          <thead>
            <tr>
              <SortTh s={s} k="period">Period</SortTh><SortTh s={s} k="statement">Statement</SortTh>
              <SortTh s={s} k="stmt" right>On statement</SortTh><SortTh s={s} k="ledger" right>In ledger</SortTh>
              <SortTh s={s} k="diff" right>Difference</SortTh><SortTh s={s} k="check">Check</SortTh>
            </tr>
          </thead>
          <tbody>
            {sorted.map((i, n) => (
              <tr key={`${i.kind}:${i.import_batch_id ?? i.backfill_file_id ?? n}`}
                className={[i.kind === 'missing' ? 'row-missing' : i.kind !== 'statement' ? 'row-pending' : '',
                  i.import_batch_id || i.backfill_file_id ? 'clickable' : ''].join(' ')}
                onClick={() => open(i)}>
                <td className="nowrap">{periodText(i.period_start, i.period_end)}</td>
                <td>
                  {i.kind === 'missing' ? <span className="text-muted">{i.note}</span>
                    : i.filename ?? (i.import_batch_id ? `Import #${i.import_batch_id}` : '—')}
                  {i.kind !== 'missing' && i.note && <div className="small text-muted">{i.note}</div>}
                </td>
                <td className="num nowrap">
                  {i.statement_total !== null ? (
                    <>{money(i.statement_total)}<div className="small text-muted">{i.statement_rows} rows</div></>
                  ) : '—'}
                </td>
                <td className="num nowrap">
                  {i.ledger_total !== null ? (
                    <>{money(i.ledger_total)}<div className="small text-muted">{i.ledger_rows} transactions</div></>
                  ) : '—'}
                </td>
                <td className="num nowrap">{Number(i.difference ?? 0) ? money(i.difference, true) : '—'}</td>
                <td className="nowrap">
                  {i.kind === 'statement' ? <CheckStatusTag status={i.status as CheckStatus} />
                    : <span className={`tag ${i.kind === 'missing' ? 'tag-over' : 'tag-pace'}`}>{TIMELINE_LABEL[i.kind]}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </TableCard>
  )
}

/** Every committed statement's check, problems first, with a button to re-check them all. */
export function StatementChecksPanel() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [params, set] = useUrl()
  const filter = oneOf(params, 'checks', FILTERS, 'mismatch')
  const setFilter = (v: Filter) => set({ checks: v === 'mismatch' ? null : v })
  const cs = useUrlSort({ statement: 'asc', account: 'asc', period: 'desc', stmt: 'desc', ledger: 'desc', diff: 'desc' }, null, 'csort')
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
    mutationFn: (apply: boolean) => post<Job>(`/statement-checks/run${apply ? '?apply=true' : ''}`),
    onSuccess: (j) => setJobId(j.id),
  })
  const s = list.data?.summary ?? {}
  const total = (s.ok ?? 0) + (s.explained ?? 0) + (s.mismatch ?? 0) + (s.unverified ?? 0)
  const items = list.data?.items ?? []
  const confident = list.data?.confident ?? 0

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
        <div className="row" style={{ gap: 'var(--space-2)' }}>
          <Button variant="primary" disabled={running || run.isPending || !confident}
            title={`Re-check every statement and apply the suggested fixes that are ${list.data?.auto_confidence ?? 90}%+ confident`}
            onClick={() => confirm(`Re-check every statement and apply about ${confident} high-confidence fix`
              + `${confident === 1 ? '' : 'es'}? Each change leaves a note on the transaction.`) && run.mutate(true)}>
            Apply confident fixes ({confident})
          </Button>
          <Button disabled={running || run.isPending} onClick={() => run.mutate(false)}>
            {running ? 'Checking…' : 'Check all statements'}
          </Button>
        </div>
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
              <SortTh s={cs} k="statement">Statement</SortTh><SortTh s={cs} k="account">Account</SortTh>
              <SortTh s={cs} k="period">Period</SortTh><SortTh s={cs} k="stmt" right>Statement</SortTh>
              <SortTh s={cs} k="ledger" right>Ledger</SortTh><SortTh s={cs} k="diff" right>Difference</SortTh><th>Findings</th>
            </tr>
          </thead>
          <tbody>
            {sortRows(items, cs, {
              statement: (c) => c.filename ?? `Import #${c.import_batch_id}`, account: (c) => c.account_name ?? c.account_ref,
              period: (c) => c.period_start, stmt: (c) => amt(c.statement_total), ledger: (c) => amt(c.ledger_total),
              diff: (c) => absAmt(c.difference),
            }).map((c) => (
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
                  {c.confident > 0 && <span className="text-muted"> · {c.confident} confident</span>}
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
