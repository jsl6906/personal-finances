import { useState, type ReactNode } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  del, get, post, put, type Anomaly, type BreakdownRow, type BudgetInfo, type EntityYear, type PeriodType, type TransactionPage,
} from '../api'
import { TXN_SORT, type DetailRange } from '../detail'
import { iso, money, monthLabel, parseIso, shortDate } from '../format'
import { accountPath, categoryPath, txnPath } from '../links'
import { numParam, sortRows, useUrl, useUrlSort } from '../urlState'
import { AnomalyList } from './AnomalyList'
import { Button, Card, ErrorNote, Field, Seg, SortTh, TableCard } from './ui'

export function RangeSeg({ value, onChange }: { value: DetailRange; onChange: (r: DetailRange) => void }) {
  return (
    <Seg name="detail-range" value={value} onChange={onChange} options={[
      { value: '12m', label: '12 months' }, { value: '24m', label: '24 months' }, { value: '5y', label: '5 years' },
      { value: 'all', label: 'All time' },
    ]} />
  )
}

export function Kpis({ items }: { items: { k: string; v: ReactNode; m?: ReactNode }[] }) {
  return (
    <div className="grid-kpi">
      {items.map((it) => (
        <Card key={it.k} style={{ gap: 4 }}>
          <div className="card-kicker">{it.k}</div>
          <div className="kpi-value">{it.v}</div>
          {it.m && <div className="card-meta">{it.m}</div>}
        </Card>
      ))}
    </div>
  )
}

export function Breakdown({ kicker, rows, to, value, empty = 'Nothing in this range.' }: {
  kicker: string; rows: BreakdownRow[]; to: (r: BreakdownRow) => string | null; value: (r: BreakdownRow) => number; empty?: string
}) {
  const max = Math.max(1, ...rows.map((r) => Math.abs(value(r))))
  return (
    <Card style={{ gap: 'var(--space-2)' }}>
      <div className="card-kicker">{kicker}</div>
      {rows.map((r) => {
        const path = to(r)
        return (
          <div key={String(r.id)} className="stack" style={{ gap: 2 }}>
            <div className="row small" style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
              {path ? <Link to={path}>{r.name}</Link> : <span>{r.name}</span>}
              <span className="nowrap" style={{ fontVariantNumeric: 'tabular-nums' }}>
                {money(value(r))} <span className="text-muted">· {r.count}</span>
              </span>
            </div>
            <div className="progress"><div style={{ width: `${(Math.abs(value(r)) / max) * 100}%` }} /></div>
          </div>
        )
      })}
      {rows.length === 0 && <div className="small text-muted">{empty}</div>}
    </Card>
  )
}

export function RenameCard({ initial, pending, onSave, onCancel, meta }: {
  initial: string; pending: boolean; onSave: (name: string) => void; onCancel: () => void; meta?: ReactNode
}) {
  const [draft, setDraft] = useState(initial)
  const name = draft.trim()
  return (
    <Card style={{ gap: 'var(--space-2)' }}>
      <div className="card-kicker">Rename</div>
      <form className="row" onSubmit={(e) => { e.preventDefault(); if (name && name !== initial) onSave(name) }}>
        <input className="input" style={{ flex: 1, minWidth: 220 }} autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} />
        <Button variant="primary" type="submit" disabled={pending || !name || name === initial}>Save</Button>
        <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
      </form>
      {meta && <div className="card-meta">{meta}</div>}
    </Card>
  )
}

export function BudgetCard({ budget, target, meta, onDone }: {
  budget: BudgetInfo | null; target: { category_id: number } | { group_id: number }; meta?: ReactNode; onDone: () => void
}) {
  const qc = useQueryClient()
  const [amount, setAmount] = useState(budget ? String(budget.amount) : '')
  const [period, setPeriod] = useState<PeriodType>(budget?.period_type ?? 'month')
  const [notes, setNotes] = useState(budget?.notes ?? '')
  const finish = () => {
    for (const k of ['details', 'budgets']) qc.invalidateQueries({ queryKey: [k] })
    onDone()
  }
  const save = useMutation({
    mutationFn: () => {
      const body = { category_id: null, group_id: null, ...target, period_type: period, amount: Number(amount).toFixed(2), notes: notes.trim() || null }
      return budget ? put(`/budgets/${budget.id}`, body) : post('/budgets', body)
    },
    onSuccess: finish,
  })
  const remove = useMutation({ mutationFn: () => del(`/budgets/${budget!.id}`), onSuccess: finish })
  const valid = Number(amount) > 0
  return (
    <Card style={{ gap: 'var(--space-2)' }}>
      <div className="card-kicker">{budget ? 'Edit budget' : 'Set a budget'}</div>
      <form className="stack" onSubmit={(e) => { e.preventDefault(); if (valid) save.mutate() }}>
        <div className="grid-form" style={{ alignItems: 'end' }}>
          <Field label="Amount">
            <input className="input" type="number" min="0" step="0.01" autoFocus value={amount} onChange={(e) => setAmount(e.target.value)} />
          </Field>
          <Field label="Per">
            <select className="input" value={period} onChange={(e) => setPeriod(e.target.value as PeriodType)}>
              <option value="month">Month</option><option value="quarter">Quarter</option><option value="year">Year</option>
            </select>
          </Field>
          <Field label="Notes"><input className="input" value={notes} onChange={(e) => setNotes(e.target.value)} /></Field>
        </div>
        <ErrorNote error={save.error || remove.error} />
        <div className="row">
          <Button variant="primary" type="submit" disabled={!valid || save.isPending}>Save</Button>
          <Button type="button" variant="ghost" onClick={onDone}>Cancel</Button>
          {budget && (
            <Button type="button" variant="ghost" style={{ marginLeft: 'auto' }} disabled={remove.isPending}
              onClick={() => confirm('Remove this budget?') && remove.mutate()}>Remove budget</Button>
          )}
        </div>
      </form>
      <div className="card-meta">{meta}{meta && ' '}<Link to="/budgets">All budgets</Link></div>
    </Card>
  )
}

export function YearTable({ rows, show }: { rows: EntityYear[]; show: ('spent' | 'received' | 'net')[] }) {
  const s = useUrlSort({ year: 'desc', spent: 'desc', received: 'desc', net: 'desc', count: 'desc' }, null, 'ysort')
  if (rows.length === 0) return null
  const label = { spent: 'Out', received: 'In', net: 'Net' }
  const sorted = sortRows(rows, s, { year: (r) => r.year, spent: (r) => r.spent, received: (r) => r.received, net: (r) => r.net, count: (r) => r.count })
  return (
    <TableCard>
      <table className="table">
        <thead>
          <tr><SortTh s={s} k="year">Year</SortTh>{show.map((k) => <SortTh key={k} s={s} k={k} right>{label[k]}</SortTh>)}
            <SortTh s={s} k="count" right>Transactions</SortTh></tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={r.year}>
              <td>{r.year}</td>
              {show.map((k) => <td key={k} className="num">{money(r[k])}</td>)}
              <td className="num text-muted">{r.count.toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableCard>
  )
}

export function Findings({ params }: { params: Record<string, string | number> }) {
  const q = useQuery({ queryKey: ['anomalies', 'for', params], queryFn: () => get<Anomaly[]>('/anomalies', { status: 'all', limit: 20, ...params }) })
  if (!q.data?.length) return null
  return (
    <Card>
      <div className="card-kicker">Out of norm</div>
      <div className="card-title">Findings</div>
      <AnomalyList items={q.data} />
    </Card>
  )
}

const PAGE = 25

type TxnListProps = {
  params: Record<string, string | number | undefined>; month?: string | null; onClearMonth?: () => void
  hide?: ('category' | 'account')[]; highlightId?: number; title?: string
}

/** Paged transactions for a filter; rows link to the transaction page and to their category/account pages. Sort and
 * page live in the URL (`sort`, `page`). */
export function TxnList({ params, month, onClearMonth, hide = [], highlightId, title = 'Transactions' }: TxnListProps) {
  const [url, set] = useUrl()
  const sort = useUrlSort(TXN_SORT, 'date_desc', 'sort', ['page'])
  const page = Math.max(1, numParam(url, 'page') ?? 1)
  const offset = (page - 1) * PAGE
  const range = month ? { start: month, end: iso(new Date(parseIso(month).getFullYear(), parseIso(month).getMonth() + 1, 0)) } : {}
  const full = { ...params, ...range, limit: PAGE, offset, sort: `${sort.key}_${sort.dir}` }
  const list = useQuery({
    queryKey: ['transactions', 'for', full],
    queryFn: () => get<TransactionPage>('/transactions', full),
    placeholderData: keepPreviousData,
  })
  const items = list.data?.items ?? []
  const total = list.data?.total ?? 0
  return (
    <TableCard
      head={
        <div className="row" style={{ justifyContent: 'space-between', padding: 'var(--space-3) var(--space-3) 0' }}>
          <div className="row">
            <div className="card-kicker">{title}</div>
            {month && (
              <button className="tag tag-outline chip" onClick={onClearMonth}>{monthLabel(parseIso(month))} ×</button>
            )}
          </div>
          <span className="small text-muted">
            {total.toLocaleString()} · in {money(list.data?.total_in ?? 0)} · out {money(list.data?.total_out ?? 0)}
          </span>
        </div>
      }
      foot={total > PAGE && (
        <div className="table-foot">
          <span className="text-muted">{offset + 1}–{Math.min(offset + PAGE, total)} of {total.toLocaleString()}</span>
          <div className="row">
            <Button variant="ghost" disabled={page === 1} onClick={() => set({ page: page > 2 ? page - 1 : null })}>Previous</Button>
            <Button variant="ghost" disabled={offset + PAGE >= total} onClick={() => set({ page: page + 1 })}>Next</Button>
          </div>
        </div>
      )}
    >
      <table className="table">
        <thead>
          <tr>
            <SortTh s={sort} k="date">Date</SortTh><SortTh s={sort} k="description">Description</SortTh>
            {!hide.includes('category') && <SortTh s={sort} k="category">Category</SortTh>}
            {!hide.includes('account') && <SortTh s={sort} k="account">Account</SortTh>}<SortTh s={sort} k="amount" right>Amount</SortTh>
          </tr>
        </thead>
        <tbody>
          {items.map((t) => (
            <tr key={t.id} className={t.id === highlightId ? 'selected' : undefined}>
              <td className="nowrap muted-2">{shortDate(t.txn_date)} {t.txn_date.slice(2, 4)}</td>
              <td style={{ fontWeight: 500 }}><Link to={txnPath(t.id)}>{t.description}</Link></td>
              {!hide.includes('category') && (
                <td>
                  {t.category_id
                    ? <Link to={categoryPath(t.category_id)} className="tag tag-neutral">{t.category_name}</Link>
                    : <span className="text-muted small">Uncategorized</span>}
                </td>
              )}
              {!hide.includes('account') && (
                <td className="nowrap">
                  {t.account_id ? <Link to={accountPath(t.account_id)} className="text-muted">{t.account_name}</Link> : '—'}
                </td>
              )}
              <td className={`num${Number(t.amount) > 0 ? ' pos' : ''}`}>{money(t.amount, true)}</td>
            </tr>
          ))}
          {items.length === 0 && !list.isLoading && (
            <tr><td colSpan={5} className="empty text-muted">No transactions.</td></tr>
          )}
        </tbody>
      </table>
    </TableCard>
  )
}
