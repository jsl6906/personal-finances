import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { del, get, put, type CategoryAlias, type CategoryRule } from '../api'
import { CategorySelect } from '../components/CategorySelect'
import { CrudTable } from '../components/CrudTable'
import { MerchantReview } from '../components/MerchantReview'
import { RuleEditor } from '../components/RuleEditor'
import { Button, Card, ErrorNote, Seg, SortTh, TableCard } from '../components/ui'
import { useCategories } from '../hooks'
import { categoryPath } from '../links'
import { ruleBody, ruleDraft, SOURCE_LABELS } from '../rules'
import { numParam, oneOf, sortRows, useUrl, useUrlSort, useUrlText } from '../urlState'

type Tab = 'rules' | 'aliases' | 'merchants'
const TABS: Tab[] = ['rules', 'aliases', 'merchants']
const SHOW = 200

export function Rules() {
  const [params, set] = useUrl()
  const tab = oneOf(params, 'tab', TABS, 'rules')
  // Each tab has its own filters, so switching starts clean (Back returns to the previous tab as it was).
  const setTab = (t: Tab) => set({ tab: t === 'rules' ? null : t }, { reset: true })
  return (
    <section className="page">
      <header className="page-header">
        <div>
          <h2>Categorization rules</h2>
          <div className="text-muted subtitle">How transactions get a category without you picking one</div>
        </div>
      </header>
      <Card style={{ gap: 'var(--space-2)' }}>
        <div className="card-kicker">How a category is chosen</div>
        <ol className="small" style={{ margin: 0, paddingLeft: '1.3em', display: 'grid', gap: 4 }}>
          <li>An import file or bank feed that carries its own category label uses it, translated through the
            {' '}<button type="button" className="link-btn" onClick={() => setTab('aliases')}>import label mappings</button>.</li>
          <li>Otherwise the first matching active rule below wins: lowest priority number first, then the newest rule.</li>
          <li>Anything still uncategorized can get an AI suggestion (Transactions → Suggest categories). Accepting one saves a
            merchant rule marked “Accepted AI suggestion”.</li>
          <li>Picking a category by hand changes only that transaction. You're then offered to turn it into a rule, which can
            recategorize existing matches too. Rules never touch hand-set categories unless you ask.</li>
        </ol>
      </Card>
      <Seg name="tab" value={tab} onChange={setTab} options={[
        { value: 'rules', label: 'Rules' }, { value: 'aliases', label: 'Import label mappings' },
        { value: 'merchants', label: 'Merchant cleanup' },
      ]} />
      {tab === 'rules' ? <RuleList /> : tab === 'aliases' ? <AliasTable /> : <MerchantReview />}
    </section>
  )
}

const RULE_SORT = { priority: 'asc', when: 'asc', category: 'asc', source: 'asc', applied: 'desc', active: 'desc' } as const

function RuleList() {
  const qc = useQueryClient()
  const [params, set] = useUrl()
  const rules = useQuery({ queryKey: ['rules'], queryFn: () => get<CategoryRule[]>('/rules') })
  const [q, setQ] = useUrlText('q')
  const source = params.get('source') ?? ''
  const categoryId = numParam(params, 'category')
  const showAll = params.get('all') === '1'
  const sort = useUrlSort(RULE_SORT)
  const [message, setMessage] = useState<string | null>(null)
  const editParam = params.get('id')
  const editing: number | 'new' | null = editParam === 'new' ? 'new' : numParam(params, 'id')
  const setEditing = (v: number | 'new' | null) => set({ id: v })

  const refresh = () => qc.invalidateQueries({ queryKey: ['rules'] })
  const toggle = useMutation({
    mutationFn: (r: CategoryRule) => put(`/rules/${r.id}`, ruleBody({ ...ruleDraft(r), is_active: !r.is_active })),
    onSuccess: refresh,
  })
  const remove = useMutation({
    mutationFn: (id: number) => del(`/rules/${id}`),
    onSuccess: () => { refresh(); qc.invalidateQueries({ queryKey: ['transactions'] }) },
  })

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return (rules.data ?? []).filter((r) =>
      (!source || r.source === source) && (!categoryId || r.category_id === categoryId) &&
      (!needle || [r.pattern, r.description, r.category_name, r.category_group_name, r.account_name ?? '', r.note ?? '']
        .some((s) => s.toLowerCase().includes(needle))))
  }, [rules.data, q, source, categoryId])
  const sorted = sortRows(filtered, sort, {
    priority: (r) => r.priority, when: (r) => r.description, category: (r) => r.category_name,
    source: (r) => SOURCE_LABELS[r.source] ?? r.source, applied: (r) => r.applied_count, active: (r) => Number(r.is_active),
  })
  const shown = showAll ? sorted : sorted.slice(0, SHOW)
  const current = typeof editing === 'number' ? rules.data?.find((r) => r.id === editing) : undefined

  const close = (msg?: string) => {
    setEditing(null)
    if (msg) setMessage(msg)
  }

  return (
    <>
      {message && (
        <div className="callout row" style={{ flexWrap: 'nowrap' }}>
          <span style={{ flex: 1 }}>{message}</span>
          <Button variant="ghost" className="small" onClick={() => setMessage(null)}>Dismiss</Button>
        </div>
      )}
      {(editing === 'new' || current) && (
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="card-kicker">{editing === 'new' ? 'New rule' : `Edit rule #${current!.id}`}</div>
          {current && (
            <div className="small muted-2">
              Source: {SOURCE_LABELS[current.source] ?? current.source} · created {new Date(current.created_at).toLocaleDateString()}
              {' '}· currently categorizes{' '}
              <Link to={`/transactions?rule=${current.id}`}>{current.applied_count.toLocaleString()} transaction{current.applied_count === 1 ? '' : 's'}</Link>
              {current.source !== 'user' && '. Saving marks it as yours, so AI acceptances won\'t change it.'}
            </div>
          )}
          <RuleEditor key={editing} initial={ruleDraft(current)} ruleId={current?.id} onDone={(m) => close(m)} onCancel={() => close()} />
        </Card>
      )}
      {typeof editing === 'number' && rules.isSuccess && !current && <ErrorNote error={`Rule #${editing} not found`} />}

      <div className="row-3">
        <input className="input compact" placeholder="Search rules…" style={{ width: 220 }} value={q} onChange={(e) => setQ(e.target.value)} />
        <select className="input compact" value={source} onChange={(e) => set({ source: e.target.value })}>
          <option value="">Any source</option>
          {Object.entries(SOURCE_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <CategorySelect className="input compact" value={categoryId} onChange={(v) => set({ category: v })} emptyLabel="All categories" />
        <span className="spacer" />
        <Button variant="primary" onClick={() => { setMessage(null); setEditing('new') }} disabled={editing === 'new'}>New rule</Button>
      </div>
      <ErrorNote error={rules.error || toggle.error || remove.error} />

      <TableCard foot={filtered.length > shown.length && (
        <div className="table-foot">
          <span className="text-muted">Showing {shown.length} of {filtered.length.toLocaleString()}</span>
          <Button variant="ghost" onClick={() => set({ all: true })}>Show all</Button>
        </div>
      )}>
        <table className="table">
          <thead>
            <tr>
              <SortTh s={sort} k="priority" right style={{ width: 60 }}>Priority</SortTh><SortTh s={sort} k="when">When</SortTh>
              <SortTh s={sort} k="category">Category</SortTh><SortTh s={sort} k="source" className="hide-sm">Source</SortTh>
              <SortTh s={sort} k="applied" right>Applied</SortTh><SortTh s={sort} k="active">Active</SortTh><th />
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.id} className={editing === r.id ? 'selected' : undefined} style={r.is_active ? undefined : { opacity: 0.55 }}>
                <td className="num muted-2">{r.priority}</td>
                <td>
                  <div style={{ overflowWrap: 'anywhere' }}>{r.description}</div>
                  {(r.account_name || r.note) && (
                    <div className="small muted-2">{[r.account_name && `only ${r.account_name}`, r.note].filter(Boolean).join(' · ')}</div>
                  )}
                </td>
                <td><Link to={categoryPath(r.category_id)} className="tag tag-neutral" title={r.category_group_name}>{r.category_name}</Link></td>
                <td className="small muted-2 hide-sm">{SOURCE_LABELS[r.source] ?? r.source}</td>
                <td className="num">
                  {r.applied_count ? <Link to={`/transactions?rule=${r.id}`}>{r.applied_count.toLocaleString()}</Link> : <span className="muted-2">0</span>}
                </td>
                <td><input type="checkbox" checked={r.is_active} disabled={toggle.isPending} onChange={() => toggle.mutate(r)} /></td>
                <td className="nowrap">
                  <Button variant="ghost" className="small" onClick={() => { setMessage(null); setEditing(r.id); window.scrollTo(0, 0) }}>Edit</Button>
                  <Button variant="ghost" className="small" disabled={remove.isPending}
                    onClick={() => confirm(`Delete rule “${r.description}”? Transactions keep their current category.`) && remove.mutate(r.id)}>
                    Delete
                  </Button>
                </td>
              </tr>
            ))}
            {rules.isSuccess && filtered.length === 0 && (
              <tr><td colSpan={7} className="empty text-muted">No rules{rules.data.length ? ' match these filters' : ' yet'}.</td></tr>
            )}
          </tbody>
        </table>
      </TableCard>
    </>
  )
}

function AliasTable() {
  const aliases = useQuery({ queryKey: ['category-aliases'], queryFn: () => get<CategoryAlias[]>('/category-aliases') })
  const cats = useCategories()
  const options = (cats.data ?? []).filter((c) => c.is_active).map((c) => ({ value: c.id, label: `${c.group_name} › ${c.name}` }))
  return (
    <>
      <div className="small muted-2">
        When a file or feed (e.g. Tiller) brings its own category label, it is matched to one of your categories by name, then
        through these mappings. Imports add mappings automatically when Gemini is confident about an unfamiliar label.
      </div>
      <CrudTable title="Import label mappings" kicker="Their label → your category" path="/category-aliases" queryKey="category-aliases"
        query={aliases as unknown as Parameters<typeof CrudTable>[0]['query']}
        columns={[
          { key: 'alias', label: 'Label in the file (case-insensitive)', kind: 'text', required: true },
          { key: 'category_id', label: 'Category', kind: 'select', required: true, options },
        ]}
        defaults={{ alias: '', category_id: null }}
        display={(row, key) => (key === 'category_id' ? String(row.category_name) : undefined)}
      />
    </>
  )
}
