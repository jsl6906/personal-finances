import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { get, type Transaction, type TxnContext, type TxnBriefDetail } from '../api'
import { ChargesChart } from '../components/Charts'
import { Findings, Kpis, TxnList } from '../components/Detail'
import { Card, ErrorNote } from '../components/ui'
import { fullDate, money, shortDate } from '../format'
import { accountPath, categoryPath, groupPath, merchantPath, txnPath } from '../links'
import { TransactionDetail } from './TransactionDetail'

function BriefRow({ t }: { t: TxnBriefDetail }) {
  return (
    <div className="row small" style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
      <span><span className="muted-2">{shortDate(t.txn_date)}</span> <Link to={txnPath(t.id)}>{t.description}</Link>
        {t.account_name && <span className="text-muted"> · {t.account_name}</span>}</span>
      <span className={`num${t.amount > 0 ? ' pos' : ''}`}>{money(t.amount, true)}</span>
    </div>
  )
}

export function TransactionPage() {
  const id = Number(useParams().id)
  const navigate = useNavigate()
  const txn = useQuery({ queryKey: ['transactions', 'one', id], queryFn: () => get<Transaction>(`/transactions/${id}`) })
  const ctx = useQuery({ queryKey: ['transactions', 'context', id], queryFn: () => get<TxnContext>(`/transactions/${id}/context`) })
  const t = txn.data
  const c = ctx.data
  const cmp = c?.comparison
  const amt = t ? Number(t.amount) : 0

  if (txn.error) return <section className="page"><ErrorNote error={txn.error} /></section>
  if (!t) return <section className="page text-muted">Loading…</section>

  const vs = cmp && cmp.diff_pct !== null
    ? Math.abs(cmp.diff_pct) < 0.05 ? 'About typical' : `${Math.round(Math.abs(cmp.diff_pct) * 100)}% ${cmp.diff_pct > 0 ? 'above' : 'below'} typical`
    : 'First at this merchant'

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <div className="card-kicker">Transaction · {fullDate(t.txn_date)}</div>
          <h2>{t.description}</h2>
          <div className="text-muted subtitle row" style={{ gap: 6 }}>
            <Link to={merchantPath(c?.merchant ?? t.merchant ?? t.description.toLowerCase())}>{t.merchant_name ?? c?.merchant_name ?? 'Merchant'}</Link>
            {t.merchant_source === 'user' && <span title="Merchant set by hand">✎</span>}
            {t.category_id && (<>· <Link to={categoryPath(t.category_id)}>{t.category_name}</Link></>)}
            {t.category_group_id && (<>· <Link to={groupPath(t.category_group_id)}>{t.category_group_name}</Link></>)}
            {!t.category_id && <span>· Uncategorized</span>}
            {t.account_id && (<>· <Link to={accountPath(t.account_id)}>{t.account_name}</Link></>)}
            {t.institution_name && <span>({t.institution_name})</span>}
          </div>
        </div>
        <div className={`big-amount${amt > 0 ? ' pos' : ''}`}>{money(t.amount, true)}</div>
      </header>
      <ErrorNote error={ctx.error} />

      <div className="grid-main-side">
        <div className="stack-3" style={{ minWidth: 0 }}>
          <Kpis items={[
            { k: 'Compared to usual', v: vs, m: cmp ? `typical ${money(cmp.median)} · range ${money(cmp.min)}–${money(cmp.max)}` : undefined },
            { k: 'Size rank', v: cmp ? `${Math.round(cmp.rank_pct * 100)}%` : '—',
              m: cmp ? `of ${cmp.count} at this merchant are this size or smaller` : undefined },
            { k: 'Merchant total', v: c ? money(c.stats.spent || c.stats.received) : '—',
              m: c ? `${c.stats.count} transactions since ${fullDate(c.stats.first_date)}` : undefined },
            { k: 'Rhythm', v: c?.cadence ? c.cadence.label : 'Irregular',
              m: c?.cadence ? (c.cadence.lapsed ? `lapsed · last expected ${fullDate(c.cadence.next_date)}` : `next ~${fullDate(c.cadence.next_date)}`) : undefined },
          ]} />

          <Card>
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
              <div>
                <div className="card-kicker">History</div>
                <div className="card-title">Every transaction at {c?.merchant_name ?? '…'}</div>
              </div>
              {c && <Link className="btn btn-ghost" to={merchantPath(c.merchant)}>Merchant page</Link>}
            </div>
            {c && <ChargesChart charges={c.charges} highlightId={id} median={cmp?.median} onPick={(x) => navigate(txnPath(x))} />}
            <div className="card-meta">This transaction is highlighted; click any other point to open it.</div>
          </Card>

          {(c?.transfer_match || (c?.same_day.length ?? 0) > 0) && (
            <div className="grid-2" style={{ alignItems: 'start' }}>
              {c?.transfer_match && (
                <Card style={{ gap: 'var(--space-2)' }}>
                  <div className="card-kicker">Transfer</div>
                  <div className="small muted-2">Matched with the other side of this transfer:</div>
                  <BriefRow t={c.transfer_match} />
                </Card>
              )}
              {(c?.same_day.length ?? 0) > 0 && (
                <Card style={{ gap: 'var(--space-2)' }}>
                  <div className="card-kicker">Same day · same account</div>
                  {c!.same_day.map((x) => <BriefRow key={x.id} t={x} />)}
                </Card>
              )}
            </div>
          )}

          <Findings params={{ transaction_id: id }} />
          {c && <TxnList title={`Other transactions at ${c.merchant_name}`} params={{ merchant: c.merchant }} highlightId={id} />}
        </div>
        <TransactionDetail key={`${t.id}-${t.updated_at}`} txn={t} standalone onClose={() => navigate('/transactions')} onSaved={() => undefined} />
      </div>
    </section>
  )
}
