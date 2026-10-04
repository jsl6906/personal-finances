import { useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { get, put, type AccountDetail } from '../api'
import { CashflowChart, Legend } from '../components/Charts'
import { LineChart } from '../components/LineChart'
import { Breakdown, Findings, Kpis, RangeSeg, RenameCard, TableCard, TxnList, YearTable } from '../components/Detail'
import { AccountStatements } from '../components/StatementCheck'
import { Button, Card, ErrorNote } from '../components/ui'
import { breakdownPath, rangeLabel, useDetailRange } from '../detail'
import { fullDate, money, monthEnd, monthLabel, parseIso, shortDate } from '../format'
import { merchantPath } from '../links'

export function AccountPage() {
  const id = Number(useParams().id)
  return <AccountView key={id} id={id} />
}

function AccountView({ id }: { id: number }) {
  const r = useDetailRange('12m')
  const q = useQuery({
    queryKey: ['details', 'account', id, r.start],
    queryFn: () => get<AccountDetail>(`/accounts/${id}/detail`, { start: r.start }),
    placeholderData: keepPreviousData,
  })
  const qc = useQueryClient()
  const [renaming, setRenaming] = useState(false)
  const rename = useMutation({
    mutationFn: (name: string) => {
      const { id: _id, institution_name: _i, sources: _s, ...rest } = q.data!.account
      return put(`/accounts/${id}`, { ...rest, name })
    },
    onSuccess: () => {
      setRenaming(false)
      for (const k of ['details', 'accounts', 'transactions', 'analytics']) qc.invalidateQueries({ queryKey: [k] })
    },
  })
  const d = q.data
  if (q.error) return <section className="page"><ErrorNote error={q.error} /></section>
  if (!d) return <section className="page text-muted">Loading…</section>
  const a = d.account
  const inRange = d.monthly.reduce((acc, m) => ({ in: acc.in + m.received, out: acc.out + m.spent, n: acc.n + m.count }), { in: 0, out: 0, n: 0 })
  const months = Math.max(1, d.monthly.length)

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <div className="card-kicker">Account · {a.account_type.replace('_', ' ')}</div>
          <h2>{a.name}{a.mask ? ` ···${a.mask}` : ''}</h2>
          <div className="text-muted subtitle row" style={{ gap: 6 }}>
            {a.institution_name && <span>{a.institution_name}</span>}
            {a.is_closed && <span className="tag tag-neutral">Closed</span>}
            {a.is_hidden && <span className="tag tag-neutral">Hidden</span>}
            {a.sources.map((s) => <span key={s} className="tag tag-accent">Synced · {s}</span>)}
            <span>{d.stats.count.toLocaleString()} transactions since {fullDate(d.stats.first_date)}</span>
          </div>
          {a.notes && <div className="small muted-2">{a.notes}</div>}
        </div>
        <div className="row">
          <RangeSeg value={r.range} onChange={r.setRange} />
          <Button onClick={() => setRenaming(!renaming)}>Rename</Button>
        </div>
      </header>
      <ErrorNote error={rename.error} />
      {renaming && (
        <RenameCard initial={a.name} pending={rename.isPending} onSave={rename.mutate} onCancel={() => setRenaming(false)}
          meta={a.mask ? `The ···${a.mask} suffix is added automatically; don't include it in the name.` : undefined} />
      )}

      <Kpis items={[
        { k: 'Balance', v: d.balance ? money(d.balance.balance) : '—',
          m: d.balance ? `as of ${fullDate(d.balance.as_of)}${d.balance.available !== null ? ` · ${money(d.balance.available)} available` : ''}` : 'No balance data from sources' },
        { k: 'Money in', v: money(inRange.in), m: `${money(inRange.in / months)}/mo · ${rangeLabel(d.start, d.end)}` },
        { k: 'Money out', v: money(inRange.out), m: `${money(inRange.out / months)}/mo` },
        { k: 'Net', v: money(inRange.in - inRange.out), m: `${inRange.n.toLocaleString()} transactions in range` },
      ]} />

      {d.balance_history.length > 1 && (
        <Card>
          <div className="card-kicker">Balance history</div>
          <LineChart points={d.balance_history.map((b) => ({ label: shortDate(b.as_of), value: b.balance, title: `${fullDate(b.as_of)}: ${money(b.balance)}` }))}
            format={(v) => money(v)} fit width={960} />
        </Card>
      )}

      <Card>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div><div className="card-kicker">Cash flow</div><div className="card-title">In vs out by month</div></div>
          <Legend items={[{ label: 'In', color: 'var(--color-accent-300)' }, { label: 'Out', color: 'var(--color-accent-700)' }]} />
        </div>
        <CashflowChart data={d.monthly.map((m) => ({ month: m.month, income: m.received, expenses: m.spent, net: m.net }))} onPick={r.pick}
          tip={(m) => {
            const x = d.monthly.find((y) => y.month === m)
            return { title: `${a.name} · ${monthLabel(parseIso(m))}`,
              lines: [`In ${money(x?.received ?? 0)} · out ${money(x?.spent ?? 0)} · net ${money(x?.net ?? 0, true)}`],
              contrib: { start: m, end: monthEnd(m), basis: 'all', account_id: id } }
          }} />
        <div className="card-meta">Includes transfers. Hover a month for its top merchants; click to filter the transactions below.</div>
      </Card>

      <div className="grid-2" style={{ alignItems: 'start' }}>
        <Breakdown kicker="Spending by category" rows={d.categories.filter((c) => c.spent > 0)} to={breakdownPath.category} value={(x) => x.spent} />
        <Breakdown kicker="Top merchants" rows={d.merchants.filter((m) => m.spent > 0)} to={(x) => merchantPath(String(x.id))} value={(x) => x.spent} />
      </div>

      {d.holdings.length > 0 && (
        <TableCard head={<div className="card-kicker" style={{ padding: 'var(--space-3) var(--space-3) 0' }}>Holdings · as of {fullDate(d.holdings[0].as_of)}</div>}>
          <table className="table">
            <thead><tr><th>Symbol</th><th>Description</th><th style={{ textAlign: 'right' }}>Shares</th>
              <th style={{ textAlign: 'right' }}>Value</th><th style={{ textAlign: 'right' }}>Gain</th></tr></thead>
            <tbody>
              {d.holdings.map((h, i) => (
                <tr key={`${h.symbol}-${i}`}>
                  <td>{h.symbol ?? '—'}</td><td className="text-muted">{h.description ?? ''}</td>
                  <td className="num">{h.shares?.toLocaleString() ?? '—'}</td>
                  <td className="num">{money(h.market_value)}</td>
                  <td className="num">{h.market_value !== null && h.cost_basis !== null ? money(h.market_value - h.cost_basis, true) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableCard>
      )}

      <YearTable rows={d.yearly} show={['received', 'spent', 'net']} />
      <AccountStatements accountId={id} />
      <Findings params={{ account_id: id }} />
      <TxnList params={{ account_id: id, start: r.start }} month={r.month} onClearMonth={r.clearMonth} hide={['account']} />
    </section>
  )
}
