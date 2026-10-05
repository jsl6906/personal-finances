import { useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { get, post, type Job, type Transaction, type TransactionPage } from '../api'
import { CategorySelect } from '../components/CategorySelect'
import { Button, ErrorNote, ProgressBar, Seg, SortTh, TableCard } from '../components/ui'
import { fullDate, money, PERIODS, periodRange, shortDate, type PeriodKey } from '../format'
import { useAccounts, useJob } from '../hooks'
import { accountPath, categoryPath } from '../links'
import { TXN_SORT } from '../detail'
import { numParam, oneOf, useKeyedState, useUrl, useUrlSort, useUrlText, type Patch } from '../urlState'
import { TransactionDetail, RulePrompt } from './TransactionDetail'

const STATUSES = ['all', 'uncategorized', 'suggested', 'with_statement'] as const
const PERIOD_KEYS = PERIODS.map((p) => p.key)
const PAGE = 100

export function Transactions() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [searchParams, set] = useUrl()
  const batchId = numParam(searchParams, 'batch')
  const ruleId = numParam(searchParams, 'rule')
  const urlCats = useMemo(() => (searchParams.get('categories') ?? '').split(',').filter(Boolean).map(Number), [searchParams])
  const urlLabel = searchParams.get('label')
  const accounts = useAccounts()
  const [search, setSearch, q] = useUrlText('q', ['page'])
  const status = oneOf(searchParams, 'status', STATUSES, 'all')
  const dateStart = searchParams.get('start') ?? ''
  const dateEnd = searchParams.get('end') ?? ''
  const period = oneOf<PeriodKey>(searchParams, 'period', PERIOD_KEYS,
    dateStart || dateEnd ? 'custom' : batchId || ruleId ? 'all' : 'last_90')
  const accountId = numParam(searchParams, 'account')
  const categoryId = numParam(searchParams, 'category')
  const page = Math.max(1, numParam(searchParams, 'page') ?? 1)
  const offset = (page - 1) * PAGE
  const sort = useUrlSort(TXN_SORT, 'date_desc', 'sort', ['page'])
  const txnParam = searchParams.get('txn')
  const selected: number | 'new' | null = txnParam === 'new' ? 'new' : numParam(searchParams, 'txn')
  // Opening a transaction pushes a history entry (Back closes it); moving between open ones replaces it.
  const select = (v: number | 'new' | null, replace = selected !== null && v !== null) => set({ txn: v }, { replace })
  const [bulkCategory, setBulkCategory] = useState<number | null>(null)
  const [jobId, setJobId] = useState<number | null>(null)
  const [bulkPrompt, setBulkPrompt] = useState<{ txn: Transaction; categoryId: number } | { done: string } | null>(null)

  const range = period === 'custom'
    ? { start: dateStart || undefined, end: dateEnd || undefined }
    : periodRange(period)
  const params = {
    q, status, ...range, account_id: accountId ?? undefined,
    category_id: categoryId ?? (urlCats.length ? urlCats : undefined), limit: PAGE, offset,
    sort: `${sort.key}_${sort.dir}`, import_batch_id: batchId ?? undefined, rule_id: ruleId ?? undefined,
  }
  const [checked, setChecked] = useKeyedState(JSON.stringify(params), () => new Set<number>())
  const filter = (patch: Patch) => set({ ...patch, page: null })

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
            onChange={(e) => setSearch(e.target.value)} />
          <Button onClick={() => suggest.mutate()} disabled={suggest.isPending || !!jobRunning}
            title="Apply categorization rules, then ask Gemini to suggest categories for uncategorized rows">
            {checked.size ? `Suggest categories (${checked.size})` : 'Suggest categories'}
          </Button>
          <Button onClick={() => scan.mutate()} disabled={scan.isPending}>Scan for duplicates</Button>
          <Button onClick={() => select('new')}>New transaction</Button>
          <Button variant="primary" onClick={() => navigate('/import')}>Import</Button>
        </div>
      </header>

      <div className="row-3">
        <Seg name="status" value={status} onChange={(v) => filter({ status: v === 'all' ? null : v })} options={[
          { value: 'all', label: 'All' }, { value: 'uncategorized', label: 'Uncategorized' }, { value: 'suggested', label: 'AI suggested' },
          { value: 'with_statement', label: 'With statements' },
        ]} />
        <select className="input compact" value={period} onChange={(e) => {
          const v = e.target.value as PeriodKey
          filter(v === 'custom' ? { period: v } : { period: v, start: null, end: null })
        }}>
          {PERIODS.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
        </select>
        {period === 'custom' && <>
          <input className="input compact" type="date" aria-label="Start date" value={dateStart}
            max={dateEnd || undefined} onChange={(e) => filter({ start: e.target.value, period: 'custom' })} />
          <input className="input compact" type="date" aria-label="End date" value={dateEnd}
            min={dateStart || undefined} onChange={(e) => filter({ end: e.target.value, period: 'custom' })} />
        </>}
        <select className="input compact" value={accountId ?? ''}
          onChange={(e) => filter({ account: e.target.value || null })}>
          <option value="">All accounts</option>
          {(accounts.data ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <CategorySelect className="input compact" value={categoryId} emptyLabel="All categories"
          onChange={(v) => filter({ category: v, categories: null, label: null })} />
        {urlCats.length > 0 && (
          <button className="tag tag-outline chip" onClick={() => filter({ categories: null, label: null })}>
            {urlLabel ?? `${urlCats.length} categories`} ×
          </button>
        )}
        {batchId && (
          <button className="tag tag-outline chip" onClick={() => filter({ batch: null, period })}>
            Import #{batchId} ×
          </button>
        )}
        {ruleId && (
          <button className="tag tag-outline chip" onClick={() => filter({ rule: null, period })}>
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
        <TableCard foot={total > PAGE && (
          <div className="table-foot">
            <span className="text-muted">{offset + 1}–{Math.min(offset + PAGE, total)} of {total.toLocaleString()}</span>
            <div className="row">
              <Button variant="ghost" disabled={page === 1} onClick={() => set({ page: page > 2 ? page - 1 : null })}>Previous</Button>
              <Button variant="ghost" disabled={offset + PAGE >= total} onClick={() => set({ page: page + 1 })}>Next</Button>
            </div>
          </div>
        )}>
          <table className="table">
            <thead>
              <tr>
                <th className="check">
                  <input type="checkbox" checked={allChecked}
                    onChange={() => setChecked(allChecked ? new Set() : new Set(items.map((t) => t.id)))} />
                </th>
                <SortTh s={sort} k="date">Date</SortTh>
                <SortTh s={sort} k="description">Description</SortTh>
                <SortTh s={sort} k="category">Category</SortTh>
                <SortTh s={sort} k="account" className="hide-sm">Account</SortTh>
                <th className="hide-sm"></th>
                <SortTh s={sort} k="amount" right>Amount</SortTh>
              </tr>
            </thead>
            <tbody>
              {items.map((t) => (
                <tr key={t.id} className={`clickable${selected === t.id ? ' selected' : ''}`} onClick={() => select(t.id)}>
                  <td className="check" onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" checked={checked.has(t.id)} onChange={() => toggle(t.id)} />
                  </td>
                  <td className="nowrap muted-2" title={fullDate(t.txn_date)}>{shortDate(t.txn_date)}</td>
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
        </TableCard>
        {selected === 'new' && (
          <TransactionDetail key={`new-${accountId}`} txn={null} defaultAccountId={accountId} onClose={() => select(null)}
            onSaved={(t) => select(t.id, true)} />
        )}
        {typeof selected === 'number' && detailTxn && (
          // Keyed on updated_at so a saved/refreshed record resets the form, but unrelated refetches don't.
          <TransactionDetail key={`${detailTxn.id}-${detailTxn.updated_at}`} txn={detailTxn} onClose={() => select(null)}
            onSaved={(t) => select(t.id, true)} />
        )}
      </div>
    </section>
  )
}
