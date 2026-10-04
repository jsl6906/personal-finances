import { useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { get, post, type Job, type Transaction, type TransactionPage } from '../api'
import { CategorySelect } from '../components/CategorySelect'
import { Button, Card, ErrorNote, ProgressBar, Seg } from '../components/ui'
import { money, PERIODS, periodRange, shortDate, type PeriodKey } from '../format'
import { useAccounts, useDebounced, useJob } from '../hooks'
import { accountPath, categoryPath } from '../links'
import { TransactionDetail, RulePrompt } from './TransactionDetail'

type Status = 'all' | 'uncategorized' | 'suggested' | 'with_statement'
type SortKey = 'date' | 'description' | 'category' | 'account' | 'amount'
type Sort = `${SortKey}_${'asc' | 'desc'}`
const PAGE = 100

function SortTh({ k, sort, onSort, children, className, right }: {
  k: SortKey; sort: Sort; onSort: (s: Sort) => void; children: string; className?: string; right?: boolean
}) {
  const [key, dir] = sort.split('_') as [SortKey, 'asc' | 'desc']
  const active = key === k
  const first = k === 'date' || k === 'amount' ? 'desc' : 'asc'
  return (
    <th className={className} style={right ? { textAlign: 'right' } : undefined}
      aria-sort={active ? (dir === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button type="button" className={`th-sort${active ? ' active' : ''}`}
        onClick={() => onSort(`${k}_${active ? (dir === 'asc' ? 'desc' : 'asc') : first}`)}>
        {children}<span className="th-sort-arrow">{active ? (dir === 'asc' ? '▲' : '▼') : '↕'}</span>
      </button>
    </th>
  )
}

export function Transactions() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const batchId = searchParams.get('batch') ? Number(searchParams.get('batch')) : null
  const ruleId = searchParams.get('rule') ? Number(searchParams.get('rule')) : null
  const urlCats = useMemo(() => (searchParams.get('categories') ?? '').split(',').filter(Boolean).map(Number), [searchParams])
  const urlLabel = searchParams.get('label')
  const dropParams = (...keys: string[]) => setSearchParams((prev) => {
    const next = new URLSearchParams(prev)
    for (const k of keys) next.delete(k)
    return next
  })
  const accounts = useAccounts()
  const [search, setSearch] = useState(() => searchParams.get('q') ?? '')
  const q = useDebounced(search)
  const [status, setStatus] = useState<Status>('all')
  const [period, setPeriod] = useState<PeriodKey>(searchParams.get('start') || searchParams.get('end') ? 'custom'
    : batchId || ruleId || searchParams.get('q') ? 'all' : 'last_90')
  const [dateStart, setDateStart] = useState(() => searchParams.get('start') ?? '')
  const [dateEnd, setDateEnd] = useState(() => searchParams.get('end') ?? '')
  const [accountId, setAccountId] = useState<number | null>(null)
  const [categoryId, setCategoryId] = useState<number | null>(() =>
    searchParams.get('category') ? Number(searchParams.get('category')) : null)
  const [offset, setOffset] = useState(0)
  const [sort, setSort] = useState<Sort>('date_desc')
  const [selected, setSelected] = useState<number | 'new' | null>(null)
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [bulkCategory, setBulkCategory] = useState<number | null>(null)
  const [jobId, setJobId] = useState<number | null>(null)
  const [bulkPrompt, setBulkPrompt] = useState<{ txn: Transaction; categoryId: number } | { done: string } | null>(null)

  const range = period === 'custom'
    ? { start: dateStart || undefined, end: dateEnd || undefined }
    : periodRange(period)
  const params = {
    q, status, ...range, account_id: accountId ?? undefined,
    category_id: categoryId ?? (urlCats.length ? urlCats : undefined), limit: PAGE, offset, sort,
    import_batch_id: batchId ?? undefined, rule_id: ruleId ?? undefined,
  }
  const resetPage = () => {
    setOffset(0)
    setChecked(new Set())
  }
  const onFilter = <T,>(setter: (v: T) => void) => (v: T) => {
    setter(v)
    resetPage()
  }

  const list = useQuery({
    queryKey: ['transactions', params],
    queryFn: () => get<TransactionPage>('/transactions', params),
    placeholderData: keepPreviousData,
  })
  const items = useMemo(() => list.data?.items ?? [], [list.data])
  const selectedTxn = typeof selected === 'number' ? items.find((t) => t.id === selected) ?? null : null
  const detailQuery = useQuery({
    queryKey: ['transactions', 'one', selected],
    queryFn: () => get<Transaction>(`/transactions/${selected}`),
    enabled: typeof selected === 'number' && !selectedTxn,
  })
  const detailTxn = selectedTxn ?? detailQuery.data ?? null

  const job = useJob(jobId)
  useEffect(() => {
    if (job.data && ['succeeded', 'failed', 'cancelled'].includes(job.data.status)) {
      qc.invalidateQueries({ queryKey: ['transactions'] })
      qc.invalidateQueries({ queryKey: ['summary'] })
    }
  }, [job.data?.status, qc, job.data])

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['transactions'] })
    qc.invalidateQueries({ queryKey: ['summary'] })
    setChecked(new Set())
  }
  const suggest = useMutation({
    mutationFn: () => post<Job>('/transactions/suggestions/run', checked.size ? { ids: [...checked] } : {}),
    onSuccess: (j) => setJobId(j.id),
  })
  const scan = useMutation({
    mutationFn: () => post<Job>('/duplicates/scan', {}),
    onSuccess: (j) => navigate(`/duplicates?job=${j.id}`),
  })
  const bulk = useMutation({
    mutationFn: (body: Record<string, unknown>) => post('/transactions/bulk', { ids: [...checked], ...body }),
    onSuccess: invalidate,
  })
  const acceptSel = useMutation({
    mutationFn: (ids: number[]) => post('/transactions/suggestions/accept', { ids }),
    onSuccess: invalidate,
  })
  const rejectSel = useMutation({
    mutationFn: (ids: number[]) => post('/transactions/suggestions/reject', { ids }),
    onSuccess: invalidate,
  })

  const allChecked = items.length > 0 && items.every((t) => checked.has(t.id))
  const toggle = (id: number) =>
    setChecked((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  const suggestedIds = items.filter((t) => t.suggested_category_id && !t.category_id).map((t) => t.id)
  const jobRunning = job.data && ['queued', 'running'].includes(job.data.status)
  const total = list.data?.total ?? 0
  const bulkCategorize = (categoryId: number) => {
    const sel = items.filter((t) => checked.has(t.id))
    const merchants = new Set(sel.map((t) => t.merchant))
    const seed = sel.length === checked.size && merchants.size === 1 && sel[0].merchant ? sel[0] : null
    setBulkPrompt(null)
    bulk.mutate({ category_id: categoryId }, { onSuccess: () => { if (seed) setBulkPrompt({ txn: seed, categoryId }) } })
  }

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <h2>Transactions</h2>
          <div className="text-muted subtitle">
            {total.toLocaleString()} matching · in {money(list.data?.total_in ?? 0)} · out {money(list.data?.total_out ?? 0)}
          </div>
        </div>
        <div className="row">
          <input className="input" placeholder="Search description, notes, amount…" style={{ width: 260, maxWidth: '100%' }} value={search}
            onChange={(e) => onFilter(setSearch)(e.target.value)} />
          <Button onClick={() => suggest.mutate()} disabled={suggest.isPending || !!jobRunning}
            title="Apply categorization rules, then ask Gemini to suggest categories for uncategorized rows">
            {checked.size ? `Suggest categories (${checked.size})` : 'Suggest categories'}
          </Button>
          <Button onClick={() => scan.mutate()} disabled={scan.isPending}>Scan for duplicates</Button>
          <Button onClick={() => setSelected('new')}>New transaction</Button>
          <Button variant="primary" onClick={() => navigate('/import')}>Import</Button>
        </div>
      </header>

      <div className="row-3">
        <Seg name="status" value={status} onChange={onFilter(setStatus)} options={[
          { value: 'all', label: 'All' }, { value: 'uncategorized', label: 'Uncategorized' }, { value: 'suggested', label: 'AI suggested' },
          { value: 'with_statement', label: 'With statements' },
        ]} />
        <select className="input compact" value={period} onChange={(e) => onFilter(setPeriod)(e.target.value as PeriodKey)}>
          {PERIODS.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
        </select>
        {period === 'custom' && <>
          <input className="input compact" type="date" aria-label="Start date" value={dateStart}
            max={dateEnd || undefined} onChange={(e) => onFilter(setDateStart)(e.target.value)} />
          <input className="input compact" type="date" aria-label="End date" value={dateEnd}
            min={dateStart || undefined} onChange={(e) => onFilter(setDateEnd)(e.target.value)} />
        </>}
        <select className="input compact" value={accountId ?? ''}
          onChange={(e) => onFilter(setAccountId)(e.target.value ? Number(e.target.value) : null)}>
          <option value="">All accounts</option>
          {(accounts.data ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <CategorySelect className="input compact" value={categoryId} emptyLabel="All categories"
          onChange={(v) => { if (urlCats.length) dropParams('categories', 'label'); onFilter(setCategoryId)(v) }} />
        {urlCats.length > 0 && (
          <button className="tag tag-outline chip" onClick={() => { dropParams('categories', 'label'); resetPage() }}>
            {urlLabel ?? `${urlCats.length} categories`} ×
          </button>
        )}
        {batchId && (
          <button className="tag tag-outline chip" onClick={() => { setSearchParams({}); resetPage() }}>
            Import #{batchId} ×
          </button>
        )}
        {ruleId && (
          <button className="tag tag-outline chip" onClick={() => { setSearchParams({}); resetPage() }}>
            Categorized by rule #{ruleId} ×
          </button>
        )}
      </div>

      {job.data && (
        <div className="stack">
          <div className={`callout${job.data.status === 'failed' ? ' error' : ''}`}>
            <strong>Category suggestions · {job.data.status}</strong>{' '}
            {job.data.message ?? ''}
            {job.data.status === 'succeeded' && job.data.result &&
              ` · ${job.data.result.categorized_by_rule} categorized by rule, ${job.data.result.suggested} suggested by AI`}
            {job.data.status === 'failed' && <div className="small">{job.data.error?.split('\n')[0]}</div>}
            {!jobRunning && <Button variant="ghost" className="small" onClick={() => setJobId(null)}>Dismiss</Button>}
          </div>
          {jobRunning && <ProgressBar fraction={Number(job.data.progress)} />}
        </div>
      )}

      {checked.size > 0 && (
        <div className="bulk-bar">
          <strong>{checked.size} selected</strong>
          <CategorySelect className="input compact" value={bulkCategory} onChange={setBulkCategory} emptyLabel="Set category…" />
          <Button onClick={() => bulkCategory && bulkCategorize(bulkCategory)} disabled={!bulkCategory || bulk.isPending}>Apply</Button>
          <Button onClick={() => acceptSel.mutate([...checked])} disabled={acceptSel.isPending}>Accept suggestions</Button>
          <Button variant="ghost" onClick={() => rejectSel.mutate([...checked])}>Reject suggestions</Button>
          <span className="spacer" />
          <Button variant="ghost" onClick={() => confirm(`Delete ${checked.size} transactions?`) && bulk.mutate({ delete: true })}>
            Delete
          </Button>
        </div>
      )}
      {status === 'suggested' && suggestedIds.length > 0 && checked.size === 0 && (
        <div className="bulk-bar">
          {suggestedIds.length} suggestions on this page
          <Button onClick={() => acceptSel.mutate(suggestedIds)} disabled={acceptSel.isPending}>Accept all on page</Button>
        </div>
      )}
      {bulkPrompt && 'txn' in bulkPrompt && (
        <RulePrompt key={`${bulkPrompt.txn.id}-${bulkPrompt.categoryId}`} txn={bulkPrompt.txn} categoryId={bulkPrompt.categoryId}
          onDone={(msg) => setBulkPrompt({ done: msg })} onClose={() => setBulkPrompt(null)} />
      )}
      {bulkPrompt && 'done' in bulkPrompt && (
        <div className="callout row" style={{ flexWrap: 'nowrap' }}>
          <span style={{ flex: 1 }}>{bulkPrompt.done}</span>
          <Button variant="ghost" className="small" onClick={() => setBulkPrompt(null)}>Dismiss</Button>
        </div>
      )}
      <ErrorNote error={list.error || suggest.error || bulk.error || acceptSel.error || rejectSel.error || scan.error} />

      <div className={`tx-layout${selected !== null ? ' with-detail' : ''}`}>
        <Card className="table-card">
          <table className="table">
            <thead>
              <tr>
                <th className="check">
                  <input type="checkbox" checked={allChecked}
                    onChange={() => setChecked(allChecked ? new Set() : new Set(items.map((t) => t.id)))} />
                </th>
                <SortTh k="date" sort={sort} onSort={onFilter(setSort)}>Date</SortTh>
                <SortTh k="description" sort={sort} onSort={onFilter(setSort)}>Description</SortTh>
                <SortTh k="category" sort={sort} onSort={onFilter(setSort)}>Category</SortTh>
                <SortTh k="account" sort={sort} onSort={onFilter(setSort)} className="hide-sm">Account</SortTh>
                <th className="hide-sm"></th>
                <SortTh k="amount" sort={sort} onSort={onFilter(setSort)} right>Amount</SortTh>
              </tr>
            </thead>
            <tbody>
              {items.map((t) => (
                <tr key={t.id} className={`clickable${selected === t.id ? ' selected' : ''}`} onClick={() => setSelected(t.id)}>
                  <td className="check" onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" checked={checked.has(t.id)} onChange={() => toggle(t.id)} />
                  </td>
                  <td className="nowrap muted-2">{shortDate(t.txn_date)}</td>
                  <td style={{ fontWeight: 500 }}>{t.description}</td>
                  <td>
                    {t.category_name ? (
                      <Link to={categoryPath(t.category_id!)} className="tag tag-neutral" title={t.category_group_name ?? ''}
                        onClick={(e) => e.stopPropagation()}>{t.category_name}</Link>
                    ) : t.suggested_category_name ? (
                      <span className="tag tag-outline" title={t.suggestion_reason ?? ''}>
                        {t.suggested_category_name}? {Math.round(Number(t.suggestion_confidence) * 100)}%
                      </span>
                    ) : (
                      <span className="text-muted small">Uncategorized</span>
                    )}
                  </td>
                  <td className="text-muted nowrap hide-sm">
                    {t.account_id
                      ? <Link to={accountPath(t.account_id)} className="text-muted" onClick={(e) => e.stopPropagation()}>{t.account_name}</Link>
                      : '—'}
                  </td>
                  <td className="marks hide-sm">
                    {[t.has_statement ? 'statement' : '', t.member_initials, ...t.tags.map((x) => x.name), t.budget_spread_months && t.budget_spread_months > 1 ? 'spread' : '']
                      .filter(Boolean).join(' · ')}
                  </td>
                  <td className={`num${Number(t.amount) > 0 ? ' pos' : ''}`}>{money(t.amount, true)}</td>
                </tr>
              ))}
              {items.length === 0 && !list.isLoading && (
                <tr><td colSpan={7} className="empty text-muted">No transactions match these filters.</td></tr>
              )}
            </tbody>
          </table>
          {total > PAGE && (
            <div className="table-foot">
              <span className="text-muted">{offset + 1}–{Math.min(offset + PAGE, total)} of {total.toLocaleString()}</span>
              <div className="row">
                <Button variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</Button>
                <Button variant="ghost" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Next</Button>
              </div>
            </div>
          )}
        </Card>
        {selected === 'new' && (
          <TransactionDetail key={`new-${accountId}`} txn={null} defaultAccountId={accountId} onClose={() => setSelected(null)}
            onSaved={(t) => setSelected(t.id)} />
        )}
        {typeof selected === 'number' && detailTxn && (
          // Keyed on updated_at so a saved/refreshed record resets the form, but unrelated refetches don't.
          <TransactionDetail key={`${detailTxn.id}-${detailTxn.updated_at}`} txn={detailTxn} onClose={() => setSelected(null)}
            onSaved={(t) => setSelected(t.id)} />
        )}
      </div>
    </section>
  )
}
