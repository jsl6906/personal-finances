import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { get, type GroupDetail } from '../api'
import { seriesColor } from '../chartUtils'
import { Legend, MonthlyBars, TrendChart } from '../components/Charts'
import { Breakdown, Findings, Kpis, RangeSeg, TxnList, YearTable } from '../components/Detail'
import { Card, ErrorNote } from '../components/ui'
import { breakdownPath, measure, rangeLabel, summarize, useDetailRange } from '../detail'
import { fullDate, money, monthEnd, monthLabel, parseIso } from '../format'
import { categoryPath, merchantPath } from '../links'
import { RelatedSetup } from './CategoryPage'

export function GroupPage() {
  const id = Number(useParams().id)
  return <GroupView key={id} id={id} />
}

function GroupView({ id }: { id: number }) {
  const r = useDetailRange('24m')
  const q = useQuery({
    queryKey: ['details', 'group', id, r.start],
    queryFn: () => get<GroupDetail>(`/category-groups/${id}/detail`, { start: r.start }),
    placeholderData: keepPreviousData,
  })
  const d = q.data
  if (q.error) return <section className="page"><ErrorNote error={q.error} /></section>
  if (!d) return <section className="page text-muted">Loading…</section>
  const g = d.group
  const value = measure(g.type)
  const sign = g.type === 'income' ? 1 : -1
  const sum = summarize(d.monthly, value)
  const catBudgets = d.categories.reduce((a, c) => a + (c.budget?.monthly ?? 0), 0)
  const budget = d.budget?.monthly ?? (catBudgets || null)
  const cats = d.categories.map((c) => ({ ...c, v: value(c) })).sort((a, b) => b.v - a.v)
  const maxCat = Math.max(1, ...cats.map((c) => Math.abs(c.v)))
  const top = d.trend.series.slice(0, 6).map((s) => ({ ...s, values: s.values.map((v) => Math.max(0, g.type === 'transfer' ? Math.abs(v) : sign * v)) }))

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <div className="card-kicker">Category group · {g.type}</div>
          <h2>{g.name}</h2>
          <div className="text-muted subtitle row" style={{ gap: 6 }}>
            {g.hide_from_reports && <span className="tag tag-neutral">Hidden from reports</span>}
            <span>{d.categories.length} categories · {d.stats.count.toLocaleString()} transactions since {fullDate(d.stats.first_date)}</span>
          </div>
        </div>
        <RangeSeg value={r.range} onChange={r.setRange} />
      </header>

      <Kpis items={[
        { k: 'This month', v: money(sum.current), m: budget ? `${Math.round((sum.current / budget) * 100)}% of ${money(budget)} budget` : 'No budget' },
        { k: 'Average / month', v: money(sum.avg), m: rangeLabel(d.start, d.end) },
        { k: 'Last 12 months', v: money(sum.last12), m: `${money(sum.total)} in range` },
        { k: 'Budget / month', v: budget ? money(budget) : '—',
          m: d.budget ? `group budget per ${d.budget.period_type}` : catBudgets ? 'sum of category budgets' : <Link to="/budgets">Set a budget</Link> },
      ]} />

      <Card>
        <div className="card-kicker">By month</div>
        <div className="card-title">{money(sum.total)} {g.type === 'income' ? 'received' : 'spent'}</div>
        <MonthlyBars data={d.monthly.map((m) => ({ month: m.month, value: value(m) }))} budget={budget} highlight={r.month} onPick={r.pick}
          tip={(m, v) => ({ title: `${g.name} · ${monthLabel(parseIso(m))}`, lines: [money(v)],
            contrib: { start: m, end: monthEnd(m), basis: 'all', level: 'group', ids: [id] } })} />
        <div className="card-meta">Net of refunds. Hover a month for its top merchants; click to filter the transactions below.</div>
      </Card>

      <div className="grid-main-side">
        <Card>
          <div className="card-kicker">Categories over time</div>
          <TrendChart months={d.trend.months} series={top} />
          <Legend items={top.map((s, i) => ({ label: s.name, color: seriesColor(i) }))} />
        </Card>
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="card-kicker">Categories · {rangeLabel(d.start, d.end)}</div>
          {cats.map((c) => (
            <div key={c.id} className="stack" style={{ gap: 2 }}>
              <div className="row small" style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
                <Link to={categoryPath(c.id)} style={{ opacity: c.is_active ? 1 : 0.6 }}>{c.name}</Link>
                <span className="nowrap" style={{ fontVariantNumeric: 'tabular-nums' }}>
                  {money(c.v)}
                  <span className="text-muted"> · {money(c.v / Math.max(1, d.monthly.length))}/mo{c.budget ? ` of ${money(c.budget.monthly)}` : ''}</span>
                </span>
              </div>
              <div className="progress"><div style={{ width: `${(Math.abs(c.v) / maxCat) * 100}%` }} /></div>
            </div>
          ))}
        </Card>
      </div>

      <div className="grid-2" style={{ alignItems: 'start' }}>
        <Breakdown kicker="Top merchants" rows={d.merchants} to={(x) => merchantPath(String(x.id))} value={value} />
        <Breakdown kicker="Accounts" rows={d.accounts} to={breakdownPath.account} value={value} />
      </div>

      <RelatedSetup d={d} />
      <YearTable rows={d.yearly} show={g.type === 'income' ? ['received', 'spent', 'net'] : ['spent', 'received', 'net']} />
      <Findings params={{ group_id: id }} />
      <TxnList params={{ group_id: id, start: r.start }} month={r.month} onClearMonth={r.clearMonth} />
    </section>
  )
}
