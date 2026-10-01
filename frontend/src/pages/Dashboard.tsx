import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { ACCOUNT_TYPES, get, type AlertEvent, type Balances, type BudgetStatus, type CashflowMonth, type Summary, type TransactionPage } from '../api'
import { AnomalyList } from '../components/AnomalyList'
import { CashflowChart, Legend } from '../components/Charts'
import { BalanceSparkline } from '../components/LineChart'
import { Button, Card } from '../components/ui'
import { fullDate, iso, money, moneyRound, monthEnd, monthLabel, parseIso, shortDate } from '../format'
import { useAccounts, useAnomalies, useBalanceTrend } from '../hooks'
import { accountPath, categoryPath, groupPath, txnPath } from '../links'

export function Dashboard() {
  const navigate = useNavigate()
  const [now] = useState(() => new Date())
  const start = iso(new Date(now.getFullYear(), now.getMonth(), 1))
  const end = iso(new Date(now.getFullYear(), now.getMonth() + 1, 0))
  const summary = useQuery({ queryKey: ['summary', start, end], queryFn: () => get<Summary>('/summary', { start, end }) })
  const recent = useQuery({
    queryKey: ['transactions', 'recent'],
    queryFn: () => get<TransactionPage>('/transactions', { limit: 8, sort: 'date_desc' }),
  })
  const accounts = useAccounts()
  const budget = useQuery({
    queryKey: ['budgets', 'status', 'month', start],
    queryFn: () => get<BudgetStatus>('/budgets/status', { period: 'month', on: start }),
  })
  const bs = budget.data
  const flow = useQuery({ queryKey: ['analytics', 'cashflow', 12], queryFn: () => get<CashflowMonth[]>('/analytics/cashflow', { months: 12 }) })
  const anomalies = useAnomalies()
  const balances = useQuery({ queryKey: ['balances'], queryFn: () => get<Balances>('/balances') })
  const trend = useBalanceTrend()
  const balanceBy = new Map((balances.data?.accounts ?? []).map((b) => [b.account_id, b]))
  const accountGroups = new Map<string, { a: NonNullable<typeof accounts.data>[number]; bal: number; asOf: string }[]>()
  for (const a of accounts.data ?? []) {
    const b = balanceBy.get(a.id)
    if (a.is_closed || a.is_hidden || !b || !Number(b.balance)) continue
    const bal = ['credit_card', 'loan', 'mortgage'].includes(a.account_type) ? -Math.abs(Number(b.balance)) : Number(b.balance)
    const rows = accountGroups.get(a.account_type) ?? []
    rows.push({ a, bal, asOf: b.as_of })
    accountGroups.set(a.account_type, rows)
  }
  const typeRank = (t: string) => { const i = ACCOUNT_TYPES.indexOf(t); return i < 0 ? ACCOUNT_TYPES.length : i }
  const groupedAccounts = [...accountGroups.entries()].sort(([x], [y]) => typeRank(x) - typeRank(y))
  const alertEvents = useQuery({ queryKey: ['alerts', 'events'], queryFn: () => get<AlertEvent[]>('/alerts/events', { limit: 12 }) })
  const lastSent = alertEvents.data?.find((e) => e.status === 'sent' && e.kind !== 'weekly_digest')
  const s = summary.data
  const mon = now.toLocaleString('en-US', { month: 'short' })

  const stats = [
    { k: `Spent · ${mon}`, v: s ? moneyRound(s.spent) : '—', m: `${s?.count ?? 0} transactions, excl. transfers` },
    { k: `Income · ${mon}`, v: s ? moneyRound(s.income) : '—', m: 'Excl. transfers & hidden' },
    { k: 'Budget left', v: bs && bs.total.count ? moneyRound(bs.total.left) : '—',
      m: bs && bs.total.count ? `${bs.total.over_count} of ${bs.total.count} categories over` : 'No budgets yet' },
    { k: 'Uncategorized', v: s ? String(s.uncategorized_all) : '—', m: `${s?.uncategorized ?? 0} this month` },
  ]

  return (
    <section className="page" style={{ gap: 'var(--space-6)' }}>
      <header className="page-header">
        <div>
          <h2>{monthLabel(now)}</h2>
          <div className="text-muted subtitle">
            Household overview · {s?.accounts ?? 0} accounts · last transaction {s?.last_transaction ? shortDate(s.last_transaction) : '—'}
          </div>
        </div>
        <div className="row">
          <Button onClick={() => navigate('/import')}>Import transactions</Button>
          <Button variant="primary" onClick={() => navigate('/chat')}>Ask the ledger</Button>
        </div>
      </header>

      <div className="grid-kpi">
        {stats.map((st) => (
          <Card key={st.k} style={{ gap: 4 }}>
            <div className="card-kicker">{st.k}</div>
            <div className="kpi-value">{st.v}</div>
            <div className="card-meta">{st.m}</div>
          </Card>
        ))}
      </div>

      <div className="grid-main-side">
        <Card>
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
            <div><div className="card-kicker">Trend</div><div className="card-title">Income vs expenses · trailing 12 months</div></div>
            <Legend items={[{ label: 'Income', color: 'var(--color-accent-300)' }, { label: 'Expenses', color: 'var(--color-accent-700)' }]} />
          </div>
          <CashflowChart data={flow.data ?? []} onPick={(m) => navigate(`/reports?month=${m}`)}
            tip={(m, part) => {
              const f = flow.data?.find((x) => x.month === m)
              const label = monthLabel(parseIso(m))
              if (part) {
                return { title: `${part === 'income' ? 'Income' : 'Expenses'} · ${label}`, lines: [money(part === 'income' ? f?.income : f?.expenses)],
                  contrib: { start: m, end: monthEnd(m), kind: part === 'income' ? 'income' : 'expense' } }
              }
              return { title: label, lines: [`Income ${money(f?.income)} · expenses ${money(f?.expenses)} · net ${money(f?.net, true)}`],
                contrib: { start: m, end: monthEnd(m) } }
            }} />
        </Card>
        <Card>
          <div className="card-kicker">Out of norm</div>
          <Link to="/findings" className="card-title">
            {anomalies.data ? `${anomalies.data.length} item${anomalies.data.length === 1 ? '' : 's'} flagged` : '…'}
          </Link>
          <AnomalyList items={anomalies.data ?? []} limit={4} />
          {lastSent && (
            <Link to="/alerts" className="card-meta">
              Email alert sent to {lastSent.recipients.length} recipient{lastSent.recipients.length === 1 ? '' : 's'} · {shortDate(lastSent.sent_at)}
            </Link>
          )}
          <Link to="/findings" className="card-meta">
            {anomalies.data && anomalies.data.length > 4 ? `Review all ${anomalies.data.length} findings` : 'Review findings'}
          </Link>
        </Card>
      </div>

      <div className="grid-2">
        <Card>
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
            <div>
              <div className="card-kicker">Latest</div>
              <div className="card-title">Recent transactions</div>
            </div>
            <Link to="/transactions" className="btn btn-ghost">All transactions</Link>
          </div>
          <table className="table">
            <tbody>
              {(recent.data?.items ?? []).map((t) => (
                <tr key={t.id}>
                  <td className="nowrap muted-2">{shortDate(t.txn_date)}</td>
                  <td><Link to={txnPath(t.id)}>{t.description}</Link></td>
                  <td>
                    {t.category_id
                      ? <Link to={categoryPath(t.category_id)} className="tag tag-neutral">{t.category_name}</Link>
                      : <span className="tag tag-neutral">Uncategorized</span>}
                  </td>
                  <td className={`num${Number(t.amount) > 0 ? ' pos' : ''}`}>{money(t.amount, true)}</td>
                </tr>
              ))}
              {recent.data?.items.length === 0 && (
                <tr><td className="text-muted">No transactions yet. Import or add one on the Transactions screen.</td></tr>
              )}
            </tbody>
          </table>
        </Card>
        <Card>
          <div className="card-kicker">Accounts</div>
          <div className="card-title">{balances.data?.accounts.length ? `Net worth ${moneyRound(balances.data.net_worth)}` : 'Open accounts'}</div>
          <table className="table">
            <thead><tr><th>Account</th><th>Institution</th><th className="hide-sm">24 months</th><th className="num">Balance</th></tr></thead>
            {groupedAccounts.map(([type, rows]) => (
              <tbody key={type}>
                <tr className="group-row">
                  <td colSpan={2}>{type.replace('_', ' ')}</td>
                  <td className="hide-sm" />
                  <td className="num">{money(rows.reduce((sum, r) => sum + r.bal, 0))}</td>
                </tr>
                {rows.map(({ a, bal, asOf }) => (
                  <tr key={a.id}>
                    <td><Link to={accountPath(a.id)}>{a.name}{a.mask ? ` ···${a.mask}` : ''}</Link></td>
                    <td className="text-muted">{a.institution_name ?? '—'}</td>
                    <td className="hide-sm">
                      <BalanceSparkline width={64} points={trend.data?.[a.id]} liability={['credit_card', 'loan', 'mortgage'].includes(a.account_type)} />
                    </td>
                    <td className="num" title={`as of ${fullDate(asOf)}`}>{money(bal)}</td>
                  </tr>
                ))}
              </tbody>
            ))}
            {groupedAccounts.length === 0 && (
              <tbody><tr><td colSpan={4} className="text-muted">No accounts with a balance yet.</td></tr></tbody>
            )}
          </table>
          <Link to="/sources" className="card-meta">
            {balances.data?.accounts.length ? 'Balances from connected sources' : 'Balances arrive with source sync'} · as of {fullDate(iso(now))}
          </Link>
        </Card>
      </div>
      <Card>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div>
            <div className="card-kicker">Budget</div>
            <div className="card-title">{bs?.period.label ?? ''} · {Math.round((bs?.period.elapsed ?? 0) * 100)}% through</div>
          </div>
          <Link to="/budgets" className="btn btn-ghost">All budgets</Link>
        </div>
        <div className="stack-3">
          {(bs?.rows ?? []).filter((r) => r.kind === 'expense').slice(0, 6).map((r) => (
            <div key={r.budget_id} className="budget-row">
              <div>{r.category_id ? <Link to={categoryPath(r.category_id)}>{r.name}</Link>
                : r.group_id ? <Link to={groupPath(r.group_id)}>{r.name}</Link> : r.name}</div>
              <div className="progress"><div style={{ width: `${Math.min(100, r.pct)}%`, background: r.status === 'over' ? 'var(--color-accent-700)' : 'var(--color-accent-400)' }} /></div>
              <div className="nowrap" style={{ fontVariantNumeric: 'tabular-nums', color: r.status === 'over' ? 'var(--color-accent-700)' : undefined }}>
                {moneyRound(r.actual)} / {moneyRound(r.budget)}
              </div>
            </div>
          ))}
          {bs && bs.rows.length === 0 && <div className="text-muted small">No budgets yet. <Link to="/budgets">Set some up</Link>.</div>}
        </div>
      </Card>
    </section>
  )
}
