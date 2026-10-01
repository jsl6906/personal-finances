import { useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { get, post, put, type MerchantDetail } from '../api'
import { ChargesChart, MonthlyBars } from '../components/Charts'
import { Breakdown, Findings, Kpis, RangeSeg, TxnList, YearTable } from '../components/Detail'
import { MerchantPicker } from '../components/MerchantInput'
import { Button, Card, ErrorNote } from '../components/ui'
import { breakdownPath, rangeLabel, useDetailRange } from '../detail'
import { fullDate, money, monthEnd, monthLabel, parseIso, shortDate } from '../format'
import { categoryPath, merchantPath, txnPath } from '../links'

export function MerchantPage() {
  const key = useSearchParams()[0].get('key') ?? ''
  return key ? <MerchantView key={key} merchant={key} /> : <section className="page"><ErrorNote error="No merchant selected" /></section>
}

function MerchantView({ merchant: key }: { merchant: string }) {
  const navigate = useNavigate()
  const r = useDetailRange('all')
  const q = useQuery({
    queryKey: ['details', 'merchant', key, r.start],
    queryFn: () => get<MerchantDetail>('/merchants/detail', { key, start: r.start }),
    placeholderData: keepPreviousData,
  })
  const qc = useQueryClient()
  const [mode, setMode] = useState<'rename' | 'merge' | null>(null)
  const [nameDraft, setNameDraft] = useState('')
  const refresh = () => {
    for (const k of ['details', 'transactions', 'analytics', 'merchants']) qc.invalidateQueries({ queryKey: [k] })
  }
  const rename = useMutation({
    mutationFn: (name: string | null) => put('/merchants/name', { key: q.data!.key, display_name: name }),
    onSuccess: () => { setMode(null); refresh() },
  })
  const merge = useMutation({
    mutationFn: (target: string) => post<{ key: string; moved: number }>('/merchants/merge', { source: q.data!.key, target }),
    onSuccess: (res) => { refresh(); navigate(merchantPath(res.key)) },
  })
  const unmerge = useMutation({
    mutationFn: (alias: string) => post('/merchants/unmerge', { key: alias }),
    onSuccess: refresh,
  })
  const d = q.data
  if (q.error) return <section className="page"><ErrorNote error={q.error} /></section>
  if (!d) return <section className="page text-muted">Loading…</section>
  const s = d.stats
  const income = s.received > s.spent
  const value = (m: { spent: number; received: number }) => (income ? m.received : m.spent)
  const rangeTotal = d.monthly.reduce((a, m) => a + value(m), 0)

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <div className="card-kicker">Merchant</div>
          <h2>{d.name}</h2>
          <div className="text-muted subtitle">
            “{d.latest_description}” · {s.count.toLocaleString()} transactions · first {fullDate(s.first_date)} · last {fullDate(s.last_date)}
            {d.rule && <> · auto-categorized as <Link to={categoryPath(d.rule.category_id)}>{d.rule.category_name}</Link></>}
          </div>
          {d.key !== key && <div className="small muted-2">“{key}” is merged into this merchant.</div>}
        </div>
        <div className="row">
          <RangeSeg value={r.range} onChange={r.setRange} />
          <Button onClick={() => { setNameDraft(d.display_name ?? d.name); setMode(mode === 'rename' ? null : 'rename') }}>Rename</Button>
          <Button onClick={() => setMode(mode === 'merge' ? null : 'merge')}>Merge into…</Button>
        </div>
      </header>
      <ErrorNote error={rename.error || merge.error || unmerge.error} />

      {mode === 'rename' && (
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="card-kicker">Display name</div>
          <form className="row" onSubmit={(e) => { e.preventDefault(); rename.mutate(nameDraft.trim() || null) }}>
            <input className="input" style={{ flex: 1, minWidth: 220 }} autoFocus value={nameDraft} onChange={(e) => setNameDraft(e.target.value)} />
            <Button variant="primary" type="submit" disabled={rename.isPending}>Save</Button>
            {d.display_name && <Button type="button" variant="ghost" onClick={() => rename.mutate(null)}>Use automatic name</Button>}
            <Button type="button" variant="ghost" onClick={() => setMode(null)}>Cancel</Button>
          </form>
          <div className="card-meta">Shown everywhere for this merchant; the underlying key “{d.key}” stays the same.</div>
        </Card>
      )}
      {mode === 'merge' && (
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="card-kicker">Merge {d.name} into another merchant</div>
          <div className="small muted-2">
            All {s.count.toLocaleString()} transactions move to the merchant you pick, and future imports that clean up to “{d.key}” follow.
            Its category rule moves too unless the target already has one. You can undo this from the target's page.
          </div>
          <MerchantPicker exclude={d.key} onPick={(m) => confirm(`Merge “${d.name}” into “${m.name}”?`) && merge.mutate(m.key)} />
        </Card>
      )}

      <Kpis items={[
        { k: income ? 'Received · all time' : 'Spent · all time', v: money(income ? s.received : s.spent),
          m: income ? (s.spent ? `${money(s.spent)} paid out` : undefined) : (s.received ? `${money(s.received)} refunded / credited` : undefined) },
        { k: 'Last 12 months', v: money(income ? s.received_12m : s.spent_12m), m: `${s.count_12m} transactions · ${money((income ? s.received_12m : s.spent_12m) / 12)}/mo` },
        { k: 'Typical charge', v: s.median_out !== null ? money(s.median_out) : '—',
          m: s.avg_out !== null ? `avg ${money(s.avg_out)} · largest ${money(s.max_out)}` : undefined },
        { k: 'Rhythm', v: d.cadence?.label ?? 'Irregular',
          m: d.cadence ? `${d.cadence.lapsed ? 'lapsed — was due' : 'next'} ~${fullDate(d.cadence.next_date)}${d.cadence.typical_amount ? ` · ${money(d.cadence.typical_amount)}` : ''}` : 'no regular pattern' },
      ]} />

      <Card>
        <div className="card-kicker">Every transaction</div>
        <ChargesChart charges={d.charges} median={s.median_out} onPick={(id) => navigate(txnPath(id))} />
        <div className="card-meta">Click a point to open that transaction.</div>
      </Card>

      <Card>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div>
            <div className="card-kicker">By month · {rangeLabel(d.start, d.end)}</div>
            <div className="card-title">{money(rangeTotal)} {income ? 'received' : 'spent'}</div>
          </div>
        </div>
        <MonthlyBars data={d.monthly.map((m) => ({ month: m.month, value: value(m) }))} highlight={r.month} onPick={r.pick}
          tip={(m, v) => ({ title: `${d.name} · ${monthLabel(parseIso(m))}`, lines: [money(v)],
            contrib: { start: m, end: monthEnd(m), basis: 'all', merchant: d.key }, show: 'transactions' })} />
        <div className="card-meta">Hover a month for its transactions; click to filter the list below.</div>
      </Card>

      <div className="grid-2" style={{ alignItems: 'start' }}>
        <Breakdown kicker="Categories" rows={d.categories} to={breakdownPath.category} value={(x) => (income ? x.received : x.spent)} />
        <Breakdown kicker="Accounts" rows={d.accounts} to={breakdownPath.account} value={(x) => (income ? x.received : x.spent)} />
      </div>

      <YearTable rows={d.yearly} show={['spent', 'received', 'net']} />

      <div className="grid-2" style={{ alignItems: 'start' }}>
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="card-kicker">Bank descriptions</div>
          {d.descriptions.map((x) => (
            <div key={x.description} className="row small" style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
              <span style={{ fontFamily: 'var(--font-mono, monospace)' }}>{x.description}</span>
              <span className="text-muted nowrap">{x.count} · last {shortDate(x.last_date)} {x.last_date.slice(2, 4)}</span>
            </div>
          ))}
          <div className="card-meta">Most common raw descriptions grouped under this merchant.</div>
        </Card>
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="card-kicker">Merged into this merchant</div>
          {d.aliases.map((a) => (
            <div key={a} className="row small" style={{ justifyContent: 'space-between' }}>
              <span>{a}</span>
              <Button variant="ghost" className="small" disabled={unmerge.isPending}
                onClick={() => confirm(`Split “${a}” back out into its own merchant?`) && unmerge.mutate(a)}>Unmerge</Button>
            </div>
          ))}
          {d.aliases.length === 0 && <div className="small text-muted">None. Use “Merge into…” on another merchant's page to fold it in here.</div>}
          {d.overridden > 0 && (
            <div className="small muted-2">{d.overridden} transaction{d.overridden === 1 ? ' was' : 's were'} assigned here by hand.</div>
          )}
        </Card>
      </div>
      <Findings params={{ merchant: key }} />
      <TxnList params={{ merchant: key, start: r.start }} month={r.month} onClearMonth={r.clearMonth} />
    </section>
  )
}
