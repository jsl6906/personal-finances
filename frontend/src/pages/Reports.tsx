import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { get, post, type CashflowMonth, type CategoryRow, type Job, type Merchant, type Trend } from '../api'
import { seriesColor } from '../chartUtils'
import { AnomalyList } from '../components/AnomalyList'
import { CashflowChart, Legend, TrendChart } from '../components/Charts'
import { Button, Card, ErrorNote, Seg } from '../components/ui'
import { iso, money, parseIso, shortDate } from '../format'
import { useAnomalies, useJob } from '../hooks'
import { categoryPath, groupPath, merchantPath } from '../links'

type Range = '12m' | '24m' | 'ytd' | 'last_year' | 'all' | 'years'

function rangeDates(r: Range, firstYear: number, fromYear: number, toYear: number): { start: string; end: string } {
  const now = new Date()
  const endOfMonth = iso(new Date(now.getFullYear(), now.getMonth() + 1, 0))
  switch (r) {
    case '24m': return { start: iso(new Date(now.getFullYear(), now.getMonth() - 23, 1)), end: endOfMonth }
    case 'ytd': return { start: iso(new Date(now.getFullYear(), 0, 1)), end: endOfMonth }
    case 'last_year': return { start: iso(new Date(now.getFullYear() - 1, 0, 1)), end: iso(new Date(now.getFullYear() - 1, 11, 31)) }
    case 'all': return { start: iso(new Date(firstYear, 0, 1)), end: endOfMonth }
    case 'years': {
      const end = iso(new Date(toYear, 11, 31))
      return { start: iso(new Date(fromYear, 0, 1)), end: end < endOfMonth ? end : endOfMonth }
    }
    default: return { start: iso(new Date(now.getFullYear(), now.getMonth() - 11, 1)), end: endOfMonth }
  }
}

const monthEnd = (m: string) => { const d = parseIso(m); return iso(new Date(d.getFullYear(), d.getMonth() + 1, 0)) }

export function Reports() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [range, setRange] = useState<Range>('12m')
  const [level, setLevel] = useState<'group' | 'category'>('group')
  const [anomalyStatus, setAnomalyStatus] = useState<'open' | 'reviewed' | 'dismissed'>('open')
  const [jobId, setJobId] = useState<number | null>(null)
  const [thisYear] = useState(() => new Date().getFullYear())
  const span = useQuery({ queryKey: ['analytics', 'span'], queryFn: () => get<{ start: string | null; end: string | null }>('/analytics/span') })
  const firstYear = span.data?.start ? Number(span.data.start.slice(0, 4)) : thisYear
  const years = Array.from({ length: thisYear - firstYear + 1 }, (_, i) => thisYear - i)
  const [fromYear, setFromYear] = useState(thisYear - 1)
  const [toYear, setToYear] = useState(thisYear)
  const { start, end } = rangeDates(range, firstYear, fromYear, toYear)
  const month = params.get('month')
  const focus = month ? { start: month, end: monthEnd(month) } : { start, end }

  const flow = useQuery({ queryKey: ['analytics', 'cashflow', start, end], queryFn: () => get<CashflowMonth[]>('/analytics/cashflow', { start, end }) })
  const cats = useQuery({
    queryKey: ['analytics', 'categories', focus.start, focus.end],
    queryFn: () => get<{ rows: CategoryRow[] }>('/analytics/categories', focus),
  })
  const trend = useQuery({
    queryKey: ['analytics', 'trend', start, end, level],
    queryFn: () => get<Trend>('/analytics/category-trend', { start, end, level, top: 6 }),
  })
  const merchants = useQuery({
    queryKey: ['analytics', 'merchants', focus.start, focus.end],
    queryFn: () => get<Merchant[]>('/analytics/merchants', { ...focus, limit: 12 }),
  })
  const anomalies = useAnomalies(anomalyStatus)
  const job = useJob(jobId)
  const run = useMutation({
    mutationFn: () => post<Job>('/anomalies/run'),
    onSuccess: (j) => setJobId(j.id),
  })
  const done = job.data?.status === 'succeeded' || job.data?.status === 'failed'
  const checking = jobId !== null && !done
  useEffect(() => {
    if (done) qc.invalidateQueries({ queryKey: ['anomalies'] })
  }, [done, qc])
  const [openGroup, setOpenGroup] = useState<string | null>(null)

  const totals = (flow.data ?? []).reduce((a, m) => ({ inc: a.inc + m.income, exp: a.exp + m.expenses }), { inc: 0, exp: 0 })
  const n = Math.max(1, flow.data?.length ?? 1)
  const groups = new Map<string, { id: number | null; spent: number; cats: CategoryRow[] }>()
  for (const r of cats.data?.rows ?? []) {
    if (r.spent <= 0) continue
    const g = groups.get(r.group) ?? { id: r.group_id, spent: 0, cats: [] }
    g.spent += r.spent
    g.cats.push(r)
    groups.set(r.group, g)
  }
  const groupList = [...groups.entries()].sort((a, b) => b[1].spent - a[1].spent)
  const maxGroup = Math.max(1, ...groupList.map(([, g]) => g.spent))
  const exportUrl = `/api/transactions/export.csv?start=${focus.start}&end=${focus.end}`

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <h2>Reports</h2>
          <div className="text-muted subtitle">
            {shortDate(start)} {start.slice(0, 4)} – {shortDate(end)} {end.slice(0, 4)} · income {money(totals.inc)} · expenses {money(totals.exp)}
            · avg {money(totals.exp / n)}/mo
          </div>
        </div>
        <div className="row">
          <Seg name="range" value={range} onChange={setRange} options={[
            { value: '12m', label: '12 months' }, { value: '24m', label: '24 months' },
            { value: 'ytd', label: 'Year to date' }, { value: 'last_year', label: 'Last year' },
            { value: 'all', label: 'All time' }, { value: 'years', label: 'Years' },
          ]} />
          {range === 'years' && (
            <>
              <select className="input compact" aria-label="From year" value={fromYear}
                onChange={(e) => { const y = Number(e.target.value); setFromYear(y); if (y > toYear) setToYear(y) }}>
                {years.map((y) => <option key={y} value={y}>{y}</option>)}
              </select>
              <span className="text-muted">to</span>
              <select className="input compact" aria-label="To year" value={toYear}
                onChange={(e) => { const y = Number(e.target.value); setToYear(y); if (y < fromYear) setFromYear(y) }}>
                {years.map((y) => <option key={y} value={y}>{y}</option>)}
              </select>
            </>
          )}
          <a className="btn btn-secondary" href={exportUrl}>Export CSV</a>
        </div>
      </header>
      <ErrorNote error={span.error || flow.error || cats.error || trend.error || merchants.error || run.error} />

      <Card>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div><div className="card-kicker">Cash flow</div><div className="card-title">Income vs expenses by month</div></div>
          <Legend items={[{ label: 'Income', color: 'var(--color-accent-300)' }, { label: 'Expenses', color: 'var(--color-accent-700)' }]} />
        </div>
        <CashflowChart data={flow.data ?? []} onPick={(m) => setParams({ month: m })} />
        <div className="card-meta">Click a month to break it down below.</div>
      </Card>

      <div className="grid-2" style={{ alignItems: 'start' }}>
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
            <div>
              <div className="card-kicker">Spending by group</div>
              <div className="card-title">{month ? parseIso(month).toLocaleString('en-US', { month: 'long', year: 'numeric' }) : 'Whole range'}</div>
            </div>
            {month && <Button variant="ghost" onClick={() => setParams({})}>Whole range</Button>}
          </div>
          {groupList.map(([name, g]) => (
            <div key={name} className="stack" style={{ gap: 2 }}>
              <div className="row" style={{ justifyContent: 'space-between', cursor: 'pointer', flexWrap: 'nowrap' }}
                onClick={() => setOpenGroup(openGroup === name ? null : name)}>
                <span style={{ fontSize: 13 }}>
                  {openGroup === name ? '▾' : '▸'} {g.id
                    ? <Link to={groupPath(g.id)} onClick={(e) => e.stopPropagation()}>{name}</Link>
                    : name}
                </span>
                <span className="small" style={{ fontVariantNumeric: 'tabular-nums' }}>{money(g.spent)}</span>
              </div>
              <div className="progress"><div style={{ width: `${(g.spent / maxGroup) * 100}%` }} /></div>
              {openGroup === name && (
                <div className="stack small" style={{ paddingLeft: 14, marginTop: 4 }}>
                  {g.cats.map((c) => (
                    <div key={c.category} className="row" style={{ justifyContent: 'space-between' }}>
                      <Link to={c.category_id ? categoryPath(c.category_id) : '/transactions'}>{c.category}</Link>
                      <span>{money(c.spent)} · {c.count}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
          {groupList.length === 0 && <div className="small text-muted">No spending in this period.</div>}
        </Card>
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="card-kicker">Top merchants</div>
          <table className="table" style={{ fontSize: 13 }}>
            <thead><tr><th>Merchant</th><th style={{ textAlign: 'right' }}>Count</th><th style={{ textAlign: 'right' }}>Spent</th></tr></thead>
            <tbody>
              {(merchants.data ?? []).map((m) => (
                <tr key={m.merchant}>
                  <td><Link to={merchantPath(m.merchant)}>{m.example}</Link></td>
                  <td className="num">{m.count}</td><td className="num">{money(m.spent)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>

      <Card>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div><div className="card-kicker">Trend</div><div className="card-title">Top {level === 'group' ? 'groups' : 'categories'} by month</div></div>
          <Seg name="level" value={level} onChange={setLevel} options={[{ value: 'group', label: 'Groups' }, { value: 'category', label: 'Categories' }]} />
        </div>
        <TrendChart months={trend.data?.months ?? []} series={trend.data?.series ?? []} />
        <Legend items={(trend.data?.series ?? []).map((s, i) => ({ label: `${s.name} (${money(s.total / n)}/mo)`, color: seriesColor(i) }))} />
      </Card>

      <Card>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div><div className="card-kicker">Out of norm</div><div className="card-title">Findings</div></div>
          <div className="row">
            <Seg name="astatus" value={anomalyStatus} onChange={setAnomalyStatus} options={[
              { value: 'open', label: 'Open' }, { value: 'reviewed', label: 'Reviewed' }, { value: 'dismissed', label: 'Dismissed' },
            ]} />
            <Button onClick={() => run.mutate()} disabled={run.isPending || checking}>
              {checking ? 'Checking…' : 'Run check now'}
            </Button>
          </div>
        </div>
        <AnomalyList items={anomalies.data ?? []} />
        <div className="card-meta">Checks run nightly and after each import: category spikes vs. 12-month norm, unusually large purchases for a
          merchant, large one-off transactions, first purchases at new merchants, and bill increases.</div>
      </Card>
    </section>
  )
}
