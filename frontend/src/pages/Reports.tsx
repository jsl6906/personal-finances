import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { get, type CashflowMonth, type CategoryRow, type Merchant, type Trend } from '../api'
import { stackColor } from '../chartUtils'
import { Legend, NET_DOWN, NET_UP, NetChart, SeriesHistory, StackedBars } from '../components/Charts'
import { Button, Card, ErrorNote, Seg } from '../components/ui'
import { iso, money, moneyRound, monthEnd, parseIso, shortDate } from '../format'
import { categoryPath, groupPath, merchantPath } from '../links'
import { tipHandlers, useTip, type TipSpec } from '../tip'

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

export function Reports() {
  const tip = useTip()
  const [params, setParams] = useSearchParams()
  const [range, setRange] = useState<Range>('12m')
  const [histLevel, setHistLevel] = useState<'group' | 'category'>('group')
  const [histKind, setHistKind] = useState<'expense' | 'income'>('expense')
  const [histAll, setHistAll] = useState(false)
  const [netMode, setNetMode] = useState<'waterfall' | 'monthly'>('waterfall')
  const [incLevel, setIncLevel] = useState<'group' | 'category'>('category')
  const [expLevel, setExpLevel] = useState<'group' | 'category'>('group')
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
  const hist = useQuery({
    queryKey: ['analytics', 'trend', 'history', start, end, histLevel, histKind],
    queryFn: () => get<Trend>('/analytics/category-trend', { start, end, level: histLevel, kind: histKind, top: 20 }),
  })
  const incStack = useQuery({
    queryKey: ['analytics', 'trend', 'income', start, end, incLevel],
    queryFn: () => get<Trend>('/analytics/category-trend', { start, end, level: incLevel, kind: 'income', top: 5, other: true }),
  })
  const expStack = useQuery({
    queryKey: ['analytics', 'trend', 'expense', start, end, expLevel],
    queryFn: () => get<Trend>('/analytics/category-trend', { start, end, level: expLevel, kind: 'expense', top: 7, other: true }),
  })
  const merchants = useQuery({
    queryKey: ['analytics', 'merchants', focus.start, focus.end],
    queryFn: () => get<Merchant[]>('/analytics/merchants', { ...focus, limit: 12 }),
  })
  const [openGroup, setOpenGroup] = useState<string | null>(null)

  const totals = (flow.data ?? []).reduce((a, m) => ({ inc: a.inc + m.income, exp: a.exp + m.expenses }), { inc: 0, exp: 0 })
  const n = Math.max(1, flow.data?.length ?? 1)
  const net = totals.inc - totals.exp
  const byNet = [...(flow.data ?? [])].sort((a, b) => b.net - a.net)
  const upMonths = byNet.filter((m) => m.net > 0).length
  const monthName = (m: string) => parseIso(m).toLocaleString('en-US', { month: 'short', year: 'numeric' })
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
  const histSeries = (hist.data?.series ?? []).slice(0, histAll ? undefined : 10)
  const sid = (s: { id: number | null }) => s.id ?? 0
  const stackTip = (q: typeof incStack, kind: 'income' | 'expense', level: 'group' | 'category') =>
    (i: number, si: number | null, ctx: { total: number; avg: number }): TipSpec | null => {
      const d = q.data
      if (!d) return null
      const m = d.months[i]
      const base = { start: m, end: monthEnd(m), kind, level }
      if (si === null) {
        return { title: `${monthName(m)} · ${kind === 'income' ? 'income' : 'expenses'}`,
          lines: [`${money(ctx.total)} · 12-mo avg ${money(ctx.avg)}`], contrib: base }
      }
      const s = d.series[si]
      const others = d.series.filter((x) => x.name !== 'Other').map(sid)
      return { title: `${s.name} · ${monthName(m)}`, lines: [money(s.values[i])],
        contrib: s.name === 'Other' ? { ...base, exclude: others } : { ...base, ids: [sid(s)] } }
    }

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
      <ErrorNote error={span.error || flow.error || cats.error || hist.error || merchants.error || incStack.error || expStack.error} />

      {([
        { key: 'inc', kind: 'income', kicker: 'Income', total: totals.inc, q: incStack, lvl: incLevel, setLvl: setIncLevel },
        { key: 'exp', kind: 'expense', kicker: 'Expenses', total: totals.exp, q: expStack, lvl: expLevel, setLvl: setExpLevel },
      ] as const).map((c) => (
        <Card key={c.key}>
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
            <div>
              <div className="card-kicker">{c.kicker}</div>
              <div className="card-title">{moneyRound(c.total)} · {moneyRound(c.total / n)}/mo by {c.lvl === 'group' ? 'group' : 'category'}</div>
            </div>
            <Seg name={`${c.key}-level`} value={c.lvl} onChange={c.setLvl}
              options={[{ value: 'group', label: 'Groups' }, { value: 'category', label: 'Categories' }]} />
          </div>
          <StackedBars months={c.q.data?.months ?? []} series={c.q.data?.series ?? []} highlight={month}
            onPick={(m) => setParams({ month: m })} tip={stackTip(c.q, c.kind, c.lvl)} />
          <Legend items={[
            ...(c.q.data?.series ?? []).map((s, i) => ({ label: `${s.name} ${moneyRound(s.total / n)}/mo`, color: stackColor(s.name, i) })),
            { label: '12-mo avg', color: 'var(--color-text)', dashed: true },
          ]} />
          {c.key === 'exp' && <div className="card-meta">Hover a bar or segment for its top merchants; click a month to break it down below.</div>}
        </Card>
      ))}

      <Card>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div>
            <div className="card-kicker">Net income</div>
            <div className="card-title">{netMode === 'waterfall' ? 'Running total of monthly changes' : 'Monthly net with 12-month average'}</div>
          </div>
          <div className="row">
            <Legend items={[
              { label: 'Net increase', color: NET_UP }, { label: 'Net decrease', color: NET_DOWN },
              ...(netMode === 'monthly' ? [{ label: '12-mo avg', color: 'var(--color-text)' }] : []),
            ]} />
            <Seg name="netmode" value={netMode} onChange={setNetMode} options={[
              { value: 'waterfall', label: 'Waterfall' }, { value: 'monthly', label: 'Monthly' },
            ]} />
          </div>
        </div>
        <NetChart data={flow.data ?? []} mode={netMode} onPick={(m) => setParams({ month: m })}
          tip={(m, ctx) => {
            const f = flow.data?.find((x) => x.month === m)
            return { title: `${monthName(m)} · net ${money(ctx.net, true)}`,
              lines: [`Income ${money(f?.income ?? 0)} · expenses ${money(f?.expenses ?? 0)}`,
                netMode === 'waterfall' ? `Running total ${money(ctx.running, true)}` : `12-mo avg ${money(ctx.avg ?? 0, true)}`],
              contrib: { start: m, end: monthEnd(m) } }
          }} />
        {byNet.length > 0 && (
          <div className="card-meta">
            Net {moneyRound(net)} over {n} months · avg {moneyRound(net / n)}/mo · {upMonths} of {n} months positive
            · best {monthName(byNet[0].month)} ({moneyRound(byNet[0].net)})
            · worst {monthName(byNet[byNet.length - 1].month)} ({moneyRound(byNet[byNet.length - 1].net)})
          </div>
        )}
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
                onClick={() => setOpenGroup(openGroup === name ? null : name)}
                {...tipHandlers(tip, () => ({ title: name, lines: [money(g.spent)],
                  contrib: { ...focus, kind: 'expense', level: 'group', ids: [g.id ?? 0] } }))}>
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
                    <div key={c.category} className="row" style={{ justifyContent: 'space-between' }}
                      {...tipHandlers(tip, () => ({ title: c.category, lines: [money(c.spent)],
                        contrib: { ...focus, kind: 'expense', level: 'category', ids: [c.category_id ?? 0] } }))}>
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
                <tr key={m.merchant} {...tipHandlers(tip, () => ({ title: m.example, lines: [`${money(m.spent)} · ${m.count} transactions`],
                  contrib: { ...focus, merchant: m.merchant }, show: 'transactions' }))}>
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
          <div>
            <div className="card-kicker">Monthly history</div>
            <div className="card-title">{histKind === 'expense' ? 'Expenses' : 'Income'} by {histLevel === 'group' ? 'group' : 'category'}</div>
          </div>
          <div className="row">
            <Seg name="hist-kind" value={histKind} onChange={setHistKind}
              options={[{ value: 'expense', label: 'Expenses' }, { value: 'income', label: 'Income' }]} />
            <Seg name="hist-level" value={histLevel} onChange={setHistLevel}
              options={[{ value: 'group', label: 'Groups' }, { value: 'category', label: 'Categories' }]} />
          </div>
        </div>
        <SeriesHistory months={hist.data?.months ?? []} series={histSeries} highlight={month}
          onPick={(m) => setParams({ month: m })}
          label={(si, avg) => {
            const s = histSeries[si]
            const to = s.id ? (histLevel === 'group' ? groupPath(s.id) : categoryPath(s.id)) : null
            return (
              <>
                {to ? <Link to={to}>{s.name}</Link> : <span>{s.name}</span>}
                <span className="small text-muted">avg {moneyRound(avg)}/mo</span>
              </>
            )
          }}
          tip={(i, si, avg) => {
            const s = histSeries[si]
            const m = hist.data?.months[i] ?? ''
            return { title: `${s.name} · ${monthName(m)}`, lines: [`${money(s.values[i])} · avg ${money(avg)}/mo`],
              contrib: { start: m, end: monthEnd(m), kind: histKind, level: histLevel, ids: [sid(s)] } }
          }} />
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div className="card-meta">Each row has its own scale; the dashed line is the average for the range. Hover a bar for its top merchants.</div>
          {(hist.data?.series.length ?? 0) > 10 && (
            <Button variant="ghost" onClick={() => setHistAll(!histAll)}>
              {histAll ? 'Show top 10' : `Show all ${hist.data?.series.length}`}
            </Button>
          )}
        </div>
      </Card>
    </section>
  )
}
