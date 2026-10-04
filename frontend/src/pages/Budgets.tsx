import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { del, get, post, put, type Budget, type BudgetRow, type BudgetStatus, type BudgetSuggestion, type PeriodType, type SpreadRule } from '../api'
import { CategorySelect } from '../components/CategorySelect'
import { TableCard } from '../components/Detail'
import { Button, Card, ErrorNote, Field, Seg } from '../components/ui'
import { iso, money, parseIso } from '../format'
import { useGroups } from '../hooks'
import { categoryPath, txnsPath } from '../links'

function shift(on: string, period: PeriodType, dir: number): string {
  const d = parseIso(on)
  const months = period === 'month' ? 1 : period === 'quarter' ? 3 : 12
  return iso(new Date(d.getFullYear(), d.getMonth() + dir * months, 1))
}

function BudgetBar({ pct, elapsed, status }: { pct: number; elapsed: number; status: BudgetRow['status'] }) {
  return (
    <div className={`progress budget-bar ${status}`}>
      <div style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} />
      {elapsed > 0 && elapsed < 1 && <span className="elapsed" style={{ left: `${elapsed * 100}%` }} title="Time elapsed in period" />}
    </div>
  )
}

const STATUS_TAG: Record<BudgetRow['status'], [string, string] | null> = {
  over: ['tag-over', 'Over'], pace: ['tag-pace', 'At risk'], ok: null,
}
const STATUS_RANK: Record<BudgetRow['status'], number> = { over: 0, pace: 1, ok: 2 }

export function Budgets() {
  const qc = useQueryClient()
  const [period, setPeriod] = useState<PeriodType>('month')
  const [on, setOn] = useState(() => iso(new Date()))
  const [editing, setEditing] = useState<number | 'new' | null>(null)
  const status = useQuery({
    queryKey: ['budgets', 'status', period, on],
    queryFn: () => get<BudgetStatus>('/budgets/status', { period, on }),
  })
  const budgets = useQuery({ queryKey: ['budgets', 'list'], queryFn: () => get<Budget[]>('/budgets') })
  const s = status.data
  const refresh = () => qc.invalidateQueries({ queryKey: ['budgets'] })
  const remove = useMutation({ mutationFn: (id: number) => del(`/budgets/${id}`), onSuccess: refresh })
  const hasOverall = (budgets.data ?? []).some((b) => !b.category_id && !b.group_id)

  const elapsed = s?.period.elapsed ?? 0
  const rows = [...(s?.rows ?? [])].sort((a, b) =>
    Number(a.kind !== 'expense') - Number(b.kind !== 'expense') || STATUS_RANK[a.status] - STATUS_RANK[b.status])
  const expense = rows.filter((r) => r.kind === 'expense')
  const counts = { over: 0, pace: 0, ok: 0 }
  for (const r of expense) counts[r.status] += 1
  return (
    <section className="page">
      <header className="page-header">
        <div>
          <h2>Budgets</h2>
          <div className="text-muted subtitle">
            {s?.period.label ?? '…'}{elapsed > 0 && elapsed < 1 ? ` · ${Math.round(elapsed * 100)}% through` : ''} · annual
            and one-off payments spread by rule
          </div>
        </div>
        <div className="row">
          <Button variant="ghost" onClick={() => setOn(shift(on, period, -1))}>‹</Button>
          <Seg name="per" value={period} onChange={setPeriod} options={[
            { value: 'month', label: 'Month' }, { value: 'quarter', label: 'Quarter' }, { value: 'year', label: 'Year' },
          ]} />
          <Button variant="ghost" onClick={() => setOn(shift(on, period, 1))}>›</Button>
          <Button variant="ghost" onClick={() => setOn(iso(new Date()))}>Today</Button>
        </div>
      </header>
      <ErrorNote error={status.error || remove.error} />
      <div className="grid-main-side">
        <div className="stack-3" style={{ minWidth: 0 }}>
          {s && s.total.count > 0 && (
            <Card className="budget-summary">
              <div className="budget-summary-head">
                <div>
                  <div className="card-kicker">Expense budgets · {s.period.label}</div>
                  <div className="kpi-value">{money(s.total.spent)} <span className="of">of {money(s.total.budget)}</span></div>
                  <div className={`small ${s.total.left < 0 ? 'text-over' : 'text-muted'}`} style={{ marginTop: 4 }}>
                    {s.total.left < 0 ? `Over by ${money(-s.total.left)}` : `${money(s.total.left)} remaining`}
                  </div>
                </div>
                <div className="row">
                  {counts.over > 0 && <span className="tag tag-over">{counts.over} over</span>}
                  {counts.pace > 0 && <span className="tag tag-pace">{counts.pace} at risk</span>}
                  {counts.ok > 0 && <span className="tag tag-neutral">{counts.ok} on track</span>}
                </div>
              </div>
              <BudgetBar pct={s.total.budget ? (s.total.spent / s.total.budget) * 100 : 0} elapsed={elapsed}
                status={s.total.left < 0 ? 'over' : 'ok'} />
            </Card>
          )}
          <TableCard foot={
            <div className="table-foot">
              <Button variant="ghost" onClick={() => setEditing('new')} disabled={editing === 'new'}>Add budget</Button>
            </div>
          }>
            <table className="table budget-table">
              <thead>
                <tr><th>Category</th><th style={{ width: '32%' }}>Progress</th><th style={{ textAlign: 'right' }}>Spent</th>
                  <th style={{ textAlign: 'right' }}>Budget</th><th style={{ textAlign: 'right' }}>Left</th><th /></tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const b = budgets.data?.find((x) => x.id === r.budget_id)
                  if (editing === r.budget_id && b) {
                    return <BudgetEditor key={r.budget_id} budget={b} hasOverall={hasOverall} onDone={() => { setEditing(null); refresh() }} />
                  }
                  const over = r.status === 'over'
                  const note = [r.group, r.period_type !== period ? `${money(r.base_amount)}/${r.period_type}` : '',
                    r.allocated ? `${money(r.total_budget)} less ${money(r.allocated)} budgeted elsewhere` : '',
                    r.spread_amount ? `incl. ${money(r.spread_amount)} spread` : '',
                    r.status === 'pace' ? `on pace for ${money(r.projected)}` : '', r.notes].filter(Boolean).join(' · ')
                  const tag = STATUS_TAG[r.status]
                  const txns = s && r.category_ids.length ? txnsPath({
                    ...(r.category_id ? { category: r.category_id } : { categories: r.category_ids, label: `${r.name} · ${s.period.label}` }),
                    start: s.period.start, end: s.period.end,
                  }) : null
                  return (
                    <tr key={r.budget_id} className={`b-row ${r.status}`}>
                      <td className="b-name">
                        <div className="b-title">
                          {txns ? <Link to={txns} title={`Transactions · ${s?.period.label}`}>{r.name}</Link> : r.name}
                          {tag && <span className={`tag ${tag[0]}`}>{tag[1]} · {Math.round(r.pct)}%</span>}
                        </div>
                        {note && <div className="small muted-2">{note}</div>}
                      </td>
                      <td className="b-bar"><BudgetBar pct={r.pct} elapsed={elapsed} status={r.status} /></td>
                      <td className="num b-spent" data-label="Spent">{money(r.actual)}</td>
                      <td className="num text-muted b-budget" data-label="Budget">{money(r.budget)}</td>
                      <td className={`num b-left${over ? ' text-over' : ''}`} data-label={over ? 'Over by' : 'Left'}>
                        {over ? <>{money(-r.left)}<span className="hide-sm"> over</span></> : money(r.left)}
                      </td>
                      <td className="nowrap b-act" style={{ textAlign: 'right' }}>
                        <Button variant="ghost" className="small" onClick={() => setEditing(r.budget_id)}>Edit</Button>
                        <Button variant="ghost" className="small" onClick={() => confirm(`Remove the ${r.name} budget?`) && remove.mutate(r.budget_id)}>×</Button>
                      </td>
                    </tr>
                  )
                })}
                {editing === 'new' && <BudgetEditor budget={null} hasOverall={hasOverall} onDone={() => { setEditing(null); refresh() }} />}
                {s && s.rows.length === 0 && editing !== 'new' && (
                  <tr><td colSpan={6} className="text-muted">No budgets yet — add one for a category, a group or all spending, or use suggestions from your history.</td></tr>
                )}
              </tbody>
            </table>
          </TableCard>
          {s && s.unbudgeted.length > 0 && (
            <Card style={{ gap: 'var(--space-2)' }}>
              <div className="card-kicker">Unbudgeted spending · {s.period.label}</div>
              <div className="stack small">
                {s.unbudgeted.slice(0, 10).map((u) => (
                  <div key={u.category_id} className="row" style={{ justifyContent: 'space-between' }}>
                    <span><Link to={categoryPath(u.category_id)}>{u.name}</Link> <span className="text-muted">· {u.group}</span></span><span>{money(u.actual)}</span>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
        <div className="stack-3">
          <SpreadRules />
          <Suggestions onAdded={refresh} />
        </div>
      </div>
    </section>
  )
}

type Target = 'category' | 'group' | 'overall'

function BudgetEditor({ budget, hasOverall, onDone }: { budget: Budget | null; hasOverall: boolean; onDone: () => void }) {
  const groups = useGroups()
  const [target, setTarget] = useState<Target>(budget?.group_id ? 'group' : budget?.category_id ? 'category' : budget ? 'overall' : 'category')
  const [categoryId, setCategoryId] = useState<number | null>(budget?.category_id ?? null)
  const [groupId, setGroupId] = useState<number | null>(budget?.group_id ?? null)
  const [periodType, setPeriodType] = useState<PeriodType>(budget?.period_type ?? 'month')
  const [amount, setAmount] = useState(budget?.amount ?? '')
  const [notes, setNotes] = useState(budget?.notes ?? '')
  const save = useMutation({
    mutationFn: () => {
      const body = {
        category_id: target === 'category' ? categoryId : null, group_id: target === 'group' ? groupId : null,
        period_type: periodType, amount: Number(amount).toFixed(2), notes: notes || null,
      }
      return budget ? put(`/budgets/${budget.id}`, body) : post('/budgets', body)
    },
    onSuccess: onDone,
  })
  const editingOverall = !!budget && !budget.category_id && !budget.group_id
  const valid = Number(amount) > 0 && (target === 'category' ? categoryId : target === 'group' ? groupId : true)
  return (
    <tr className="selected">
      <td colSpan={6}>
        <div className="grid-form" style={{ alignItems: 'end' }}>
          <Field label="Budget for">
            <select className="input" value={target} onChange={(e) => setTarget(e.target.value as Target)}>
              <option value="category">Category</option><option value="group">Category group</option>
              <option value="overall" disabled={hasOverall && !editingOverall}>All spending</option>
            </select>
          </Field>
          {target === 'category' ? (
            <Field label="Category"><CategorySelect value={categoryId} onChange={setCategoryId} emptyLabel="Choose…" /></Field>
          ) : target === 'group' ? (
            <Field label="Group">
              <select className="input" value={groupId ?? ''} onChange={(e) => setGroupId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">Choose…</option>
                {(groups.data ?? []).map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
              </select>
            </Field>
          ) : (
            <Field label="Scope"><div className="small text-muted" style={{ paddingBottom: 8 }}>All expense categories; shown as “Everything else” after other budgets.</div></Field>
          )}
          <Field label="Amount"><input className="input" type="number" min="0" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} /></Field>
          <Field label="Per">
            <select className="input" value={periodType} onChange={(e) => setPeriodType(e.target.value as PeriodType)}>
              <option value="month">Month</option><option value="quarter">Quarter</option><option value="year">Year</option>
            </select>
          </Field>
          <Field label="Notes"><input className="input" value={notes} onChange={(e) => setNotes(e.target.value)} /></Field>
        </div>
        <ErrorNote error={save.error} />
        <div className="row" style={{ marginTop: 8 }}>
          <Button variant="primary" disabled={!valid || save.isPending} onClick={() => save.mutate()}>Save</Button>
          <Button variant="ghost" onClick={onDone}>Cancel</Button>
        </div>
      </td>
    </tr>
  )
}

function SpreadRules() {
  const qc = useQueryClient()
  const rules = useQuery({ queryKey: ['budgets', 'rules'], queryFn: () => get<SpreadRule[]>('/spread-rules') })
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState({ name: '', category_id: null as number | null, merchant_pattern: '', min_amount: '', months: 12 })
  const refresh = () => qc.invalidateQueries({ queryKey: ['budgets'] })
  const save = useMutation({
    mutationFn: () => post('/spread-rules', {
      ...draft, merchant_pattern: draft.merchant_pattern || null, min_amount: draft.min_amount || null,
    }),
    onSuccess: () => { setAdding(false); setDraft({ name: '', category_id: null, merchant_pattern: '', min_amount: '', months: 12 }); refresh() },
  })
  const remove = useMutation({ mutationFn: (id: number) => del(`/spread-rules/${id}`), onSuccess: refresh })
  return (
    <Card style={{ gap: 'var(--space-2)' }}>
      <div className="card-kicker">Spread rules</div>
      <div className="stack small">
        {(rules.data ?? []).map((r) => (
          <div key={r.id} className="row" style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
            <span>
              {r.name}
              <span className="text-muted"> · {[r.category_name, r.merchant_pattern && `“${r.merchant_pattern}”`, r.min_amount && `≥ ${money(r.min_amount)}`].filter(Boolean).join(', ')}
                {' '}→ {r.months} mo · {r.matches_12m} in last 12 mo</span>
            </span>
            <Button variant="ghost" className="small" onClick={() => remove.mutate(r.id)}>×</Button>
          </div>
        ))}
        {rules.data?.length === 0 && !adding && <span className="text-muted">Spread annual or one-off payments over months.</span>}
      </div>
      {adding ? (
        <div className="stack">
          <Field label="Name"><input className="input" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="State Farm auto (annual)" /></Field>
          <Field label="Category (optional)"><CategorySelect value={draft.category_id} onChange={(v) => setDraft({ ...draft, category_id: v })} emptyLabel="Any category" /></Field>
          <div className="grid-form">
            <Field label="Description contains"><input className="input" value={draft.merchant_pattern} onChange={(e) => setDraft({ ...draft, merchant_pattern: e.target.value })} /></Field>
            <Field label="Min amount"><input className="input" type="number" value={draft.min_amount} onChange={(e) => setDraft({ ...draft, min_amount: e.target.value })} /></Field>
            <Field label="Months"><input className="input" type="number" min={1} max={60} value={draft.months} onChange={(e) => setDraft({ ...draft, months: Number(e.target.value) || 1 })} /></Field>
          </div>
          <ErrorNote error={save.error} />
          <div className="row">
            <Button variant="primary" disabled={!draft.name || (!draft.category_id && !draft.merchant_pattern) || save.isPending} onClick={() => save.mutate()}>Save rule</Button>
            <Button variant="ghost" onClick={() => setAdding(false)}>Cancel</Button>
          </div>
        </div>
      ) : (
        <Button variant="ghost" style={{ alignSelf: 'flex-start' }} onClick={() => setAdding(true)}>Add rule</Button>
      )}
    </Card>
  )
}

function Suggestions({ onAdded }: { onAdded: () => void }) {
  const [open, setOpen] = useState(false)
  const sug = useQuery({ queryKey: ['budgets', 'suggestions'], queryFn: () => get<BudgetSuggestion[]>('/budgets/suggestions'), enabled: open })
  const add = useMutation({
    mutationFn: (s: BudgetSuggestion) => post('/budgets', { category_id: s.category_id, amount: String(s.suggested) }),
    onSuccess: onAdded,
  })
  return (
    <Card style={{ gap: 'var(--space-2)' }}>
      <div className="card-kicker">Suggestions</div>
      {!open ? (
        <>
          <div className="card-body">Monthly budgets from your trailing 12-month average spend (spread rules applied).</div>
          <Button variant="ghost" style={{ alignSelf: 'flex-start' }} onClick={() => setOpen(true)}>Suggest from history</Button>
        </>
      ) : (
        <div className="stack small">
          {(sug.data ?? []).map((s) => (
            <div key={s.category_id} className="row" style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
              <span>{s.name} <span className="text-muted">· avg {money(s.monthly_average)}</span></span>
              <Button variant="ghost" className="small" disabled={add.isPending} onClick={() => add.mutate(s)}>Add {money(s.suggested)}/mo</Button>
            </div>
          ))}
          {sug.data?.length === 0 && <span className="text-muted">Every category with regular spending already has a budget.</span>}
          <ErrorNote error={sug.error || add.error} />
        </div>
      )}
    </Card>
  )
}
