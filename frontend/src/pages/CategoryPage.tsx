import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { get, type CategoryDetail } from '../api'
import { MonthlyBars } from '../components/Charts'
import { Breakdown, Findings, Kpis, RangeSeg, TxnList, YearTable } from '../components/Detail'
import { Card, ErrorNote } from '../components/ui'
import { breakdownPath, measure, rangeLabel, summarize, useDetailRange } from '../detail'
import { fullDate, money, monthEnd, monthLabel, parseIso } from '../format'
import { groupPath, merchantPath } from '../links'

export function CategoryPage() {
  const id = Number(useParams().id)
  return <CategoryView key={id} id={id} />
}

function CategoryView({ id }: { id: number }) {
  const r = useDetailRange('24m')
  const q = useQuery({
    queryKey: ['details', 'category', id, r.start],
    queryFn: () => get<CategoryDetail>(`/categories/${id}/detail`, { start: r.start }),
    placeholderData: keepPreviousData,
  })
  const d = q.data
  if (q.error) return <section className="page"><ErrorNote error={q.error} /></section>
  if (!d) return <section className="page text-muted">Loading…</section>
  const c = d.category
  const value = measure(c.type)
  const sum = summarize(d.monthly, value)
  const verb = c.type === 'income' ? 'received' : 'spent'

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <div className="card-kicker"><Link to={groupPath(c.group_id)}>{c.group_name}</Link> · {c.type}</div>
          <h2>{c.name}</h2>
          <div className="text-muted subtitle row" style={{ gap: 6 }}>
            {!c.is_active && <span className="tag tag-neutral">Inactive</span>}
            {c.hide_from_reports && <span className="tag tag-neutral">Hidden from reports</span>}
            <span>{d.stats.count.toLocaleString()} transactions since {fullDate(d.stats.first_date)}</span>
            {c.description && <span>· {c.description}</span>}
          </div>
        </div>
        <RangeSeg value={r.range} onChange={r.setRange} />
      </header>

      <Kpis items={[
        { k: 'This month', v: money(sum.current),
          m: d.budget ? `${Math.round((sum.current / d.budget.monthly) * 100)}% of ${money(d.budget.monthly)} budget` : 'No budget' },
        { k: `Average / month`, v: money(sum.avg), m: rangeLabel(d.start, d.end) },
        { k: 'Last 12 months', v: money(sum.last12), m: `${money(sum.total)} ${verb} in range` },
        { k: 'Budget', v: d.budget ? money(d.budget.amount) : '—',
          m: d.budget ? `per ${d.budget.period_type}${d.budget.period_type !== 'month' ? ` · ${money(d.budget.monthly)}/mo` : ''}` : <Link to="/budgets">Set a budget</Link> },
      ]} />

      <Card>
        <div className="card-kicker">By month</div>
        <div className="card-title">{money(sum.total)} {verb}</div>
        <MonthlyBars data={d.monthly.map((m) => ({ month: m.month, value: value(m) }))} budget={d.budget?.monthly} highlight={r.month} onPick={r.pick}
          tip={(m, v) => ({ title: `${c.name} · ${monthLabel(parseIso(m))}`, lines: [money(v)],
            contrib: { start: m, end: monthEnd(m), basis: 'all', level: 'category', ids: [id] } })} />
        <div className="card-meta">Net of refunds. Hover a month for its top merchants; click to filter the transactions below.</div>
      </Card>

      <div className="grid-2" style={{ alignItems: 'start' }}>
        <Breakdown kicker="Top merchants" rows={d.merchants} to={(x) => merchantPath(String(x.id))} value={value} />
        <Breakdown kicker="Accounts" rows={d.accounts} to={breakdownPath.account} value={value} />
      </div>

      <RelatedSetup d={d} />
      <YearTable rows={d.yearly} show={c.type === 'income' ? ['received', 'spent', 'net'] : ['spent', 'received', 'net']} />
      <Findings params={{ category_id: id }} />
      <TxnList params={{ category_id: id, start: r.start }} month={r.month} onClearMonth={r.clearMonth} hide={['category']} />
    </section>
  )
}

export function RelatedSetup({ d }: { d: Pick<CategoryDetail, 'bill_series' | 'spread_rules' | 'merchant_rules'> }) {
  if (!d.bill_series.length && !d.spread_rules.length && !d.merchant_rules) return null
  return (
    <Card style={{ gap: 'var(--space-2)' }}>
      <div className="card-kicker">Related setup</div>
      <div className="stack small">
        {d.bill_series.length > 0 && (
          <div>Bills: {d.bill_series.map((s, i) => <span key={s.id}>{i > 0 && ', '}<Link to={`/bills?series=${s.id}`}>{s.name}</Link></span>)}</div>
        )}
        {d.spread_rules.length > 0 && (
          <div>Budget spreading: {d.spread_rules.map((s) => `${s.name} (${s.months} mo)`).join(', ')} · <Link to="/budgets">manage</Link></div>
        )}
        {d.merchant_rules > 0 && <div>{d.merchant_rules} merchant rule{d.merchant_rules === 1 ? '' : 's'} auto-assign this category</div>}
      </div>
    </Card>
  )
}
