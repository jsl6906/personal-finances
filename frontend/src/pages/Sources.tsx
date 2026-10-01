import { useEffect, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  get, patch, post, put,
  type Balances, type DataSource, type HoldingRow, type Job, type SourcesInfo,
} from '../api'
import { BackfillCard } from '../components/Backfill'
import { Button, Card, ErrorNote } from '../components/ui'
import { useJob } from '../hooks'
import { fullDate, money, shortDate } from '../format'

const LIABILITIES = ['credit_card', 'loan', 'mortgage']

function when(ts: string | null) {
  if (!ts) return 'never'
  const d = new Date(ts)
  return `${shortDate(ts)} ${d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`
}

function StatusTag({ s }: { s?: DataSource }) {
  if (!s || !s.connected) return <span className="tag tag-outline">Not set up</span>
  if (!s.enabled) return <span className="tag tag-neutral">Paused</span>
  if (s.last_status === 'failed') return <span className="tag tag-outline" style={{ color: '#8a2b2b' }}>Failed</span>
  if (s.last_status === 'needs_review') return <span className="tag tag-outline">Needs review</span>
  return <span className="tag tag-accent">Connected</span>
}

export function Sources() {
  const qc = useQueryClient()
  const info = useQuery({ queryKey: ['sources'], queryFn: () => get<SourcesInfo>('/sources') })
  const [jobId, setJobId] = useState<number | null>(null)
  const job = useJob(jobId)
  const done = !!job.data && ['succeeded', 'failed', 'cancelled'].includes(job.data.status)
  useEffect(() => {
    if (done) {
      qc.invalidateQueries({ queryKey: ['sources'] })
      qc.invalidateQueries({ queryKey: ['balances'] })
      qc.invalidateQueries({ queryKey: ['holdings'] })
      qc.invalidateQueries({ queryKey: ['transactions'] })
    }
  }, [done, jobId, qc])
  const sync = useMutation({
    mutationFn: ({ id, full }: { id: number; full?: boolean }) => post<Job>(`/sources/${id}/sync${full ? '?full=true' : ''}`),
    onSuccess: (j) => setJobId(j.id),
  })
  const toggle = useMutation({
    mutationFn: (s: DataSource) => patch<DataSource>(`/sources/${s.id}`, { enabled: !s.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['sources'] }),
  })
  const byKind = (k: DataSource['kind']) => info.data?.sources.find((s) => s.kind === k)
  const syncing = (s?: DataSource) => !!s && !!job.data && !done && job.data.payload.source_id === s.id

  const actions = (s?: DataSource) => s?.connected && (
    <div className="row">
      <Button onClick={() => sync.mutate({ id: s.id })} disabled={syncing(s) || sync.isPending}>
        {syncing(s) ? job.data?.message ?? 'Syncing…' : 'Sync now'}
      </Button>
      <Button variant="ghost" onClick={() => sync.mutate({ id: s.id, full: true })} disabled={syncing(s)}
        title={s.kind === 'tiller' ? 'Read every row in the sheet' : 'Pull the maximum 90 days'}>Full resync</Button>
      <Button variant="ghost" onClick={() => toggle.mutate(s)}>{s.enabled ? 'Pause' : 'Resume'}</Button>
    </div>
  )

  return (
    <section className="page" style={{ gap: 'var(--space-6)' }}>
      <header className="page-header">
        <div>
          <h2>Sources &amp; backfill</h2>
          <div className="text-muted subtitle">Where transactions come from. Connected sources sync nightly; new rows go through duplicate checks before landing.</div>
        </div>
      </header>
      <ErrorNote error={info.error || sync.error || toggle.error} />
      {job.data?.status === 'failed' && <div className="callout error">Sync failed: {job.data.error}</div>}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(300px,100%),1fr))', gap: 'var(--space-4)', alignItems: 'start' }}>
        <SourceCard kicker="Tiller" source={byKind('tiller')} title="Google Sheet feed"
          cost="Cost: existing annual subscription" actions={actions(byKind('tiller'))}>
          <TillerSetup source={byKind('tiller')} serviceAccount={info.data?.google_service_account ?? null} />
        </SourceCard>
        <SourceCard kicker="SimpleFIN" source={byKind('simplefin')} title="Direct bank connection"
          cost="Cost: about $15/year via SimpleFIN Bridge" actions={actions(byKind('simplefin'))}>
          <SimpleFinSetup source={byKind('simplefin')} />
        </SourceCard>
        <Card>
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <div className="card-kicker">Manual</div><span className="tag tag-neutral">Always on</span>
          </div>
          <div className="card-title">Uploads</div>
          <p className="card-body">Spreadsheets, statement PDFs and bills from the <Link to="/import">Import</Link> and{' '}
            <Link to="/bills">Bills</Link> screens, or attached in <Link to="/chat">chat</Link>. Home, vehicles and TSP values are entered here.</p>
        </Card>
      </div>

      <BackfillCard />
      <Evaluation />
      <BalancesCard />
      <HoldingsCard />
    </section>
  )
}

function SourceCard({ kicker, title, source, cost, actions, children }: {
  kicker: string; title: string; source?: DataSource; cost: string; actions: ReactNode; children: ReactNode
}) {
  const r = source?.last_result ?? {}
  return (
    <Card style={{ gap: 'var(--space-2)' }}>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div className="card-kicker">{kicker}</div><StatusTag s={source} />
      </div>
      <div className="card-title">{title}</div>
      {source?.connected && (
        <p className="card-body" style={{ margin: 0 }}>
          {source.config.sheet_title ? <>Sheet “{source.config.sheet_title}”. </> : null}
          {source.linked_accounts} linked account{source.linked_accounts === 1 ? '' : 's'} · last sync {when(source.last_sync_at)}
          {source.last_status === 'ok' && r.fetched !== undefined && (
            <> · {r.fetched} rows read, {r.inserted ?? 0} added{r.skipped_duplicates ? `, ${r.skipped_duplicates} duplicates skipped` : ''}
              {r.categories_filled ? `, ${r.categories_filled} categorized` : ''}</>
          )}
        </p>
      )}
      {source?.last_status === 'needs_review' && r.batch_id && (
        <div className="callout">
          {r.needs_review} possible duplicate{r.needs_review === 1 ? '' : 's'} need a decision before {r.new} new rows land.{' '}
          <Link to={`/import/${r.batch_id}`}>Review import</Link>
        </div>
      )}
      {source?.last_status === 'failed' && source.last_error && <div className="callout error">{source.last_error}</div>}
      {(r.warnings ?? []).map((w) => <div key={w} className="small text-muted">⚠ {w}</div>)}
      {actions}
      {children}
      <div className="card-meta">{cost}</div>
    </Card>
  )
}

function TillerSetup({ source, serviceAccount }: { source?: DataSource; serviceAccount: string | null }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(!source?.connected)
  const [sheet, setSheet] = useState(source?.config.sheet_id ?? '')
  const [days, setDays] = useState(String(source?.config.lookback_days ?? 60))
  const save = useMutation({
    mutationFn: () => put<DataSource>('/sources/tiller', { sheet, lookback_days: Number(days) || 60 }),
    onSuccess: () => { setOpen(false); qc.invalidateQueries({ queryKey: ['sources'] }) },
  })
  if (!open) return <button className="btn btn-ghost small" style={{ alignSelf: 'start' }} onClick={() => setOpen(true)}>Settings</button>
  return (
    <div className="stack" style={{ borderTop: '1px solid var(--color-divider)', paddingTop: 'var(--space-2)' }}>
      {serviceAccount ? (
        <div className="small">
          Share your Tiller sheet (Viewer) with <code style={{ userSelect: 'all' }}>{serviceAccount}</code>, then paste its URL.
        </div>
      ) : (
        <div className="callout small">
          No Google service account configured. Create one in Google Cloud (enable the Sheets API), download its JSON key and
          set <code>GOOGLE_SERVICE_ACCOUNT_FILE</code> (or <code>GOOGLE_SERVICE_ACCOUNT_JSON</code>) in <code>.env</code>.
        </div>
      )}
      <input className="input" placeholder="https://docs.google.com/spreadsheets/d/…" value={sheet} onChange={(e) => setSheet(e.target.value)} />
      <label className="row small">Look back
        <input className="input compact" style={{ width: 70 }} value={days} onChange={(e) => setDays(e.target.value)} /> days each sync
      </label>
      <ErrorNote error={save.error} />
      <div className="row">
        <Button variant="primary" onClick={() => save.mutate()} disabled={!sheet.trim() || save.isPending || !serviceAccount}>
          {save.isPending ? 'Checking sheet…' : 'Save'}
        </Button>
        {source?.connected && <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>}
      </div>
    </div>
  )
}

function SimpleFinSetup({ source }: { source?: DataSource }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [token, setToken] = useState('')
  const claim = useMutation({
    mutationFn: () => post<DataSource>('/sources/simplefin/claim', { setup_token: token }),
    onSuccess: () => { setOpen(false); setToken(''); qc.invalidateQueries({ queryKey: ['sources'] }) },
  })
  if (!open) {
    return (
      <button className="btn btn-ghost small" style={{ alignSelf: 'start' }} onClick={() => setOpen(true)}>
        {source?.connected ? 'Reconnect' : 'Connect'}
      </button>
    )
  }
  return (
    <div className="stack" style={{ borderTop: '1px solid var(--color-divider)', paddingTop: 'var(--space-2)' }}>
      <div className="small">
        Link your banks at <a href="https://bridge.simplefin.org/simplefin/create" target="_blank" rel="noreferrer">SimpleFIN Bridge</a>,
        create a setup token for this app and paste it here. It can only be claimed once; the resulting access key is stored encrypted.
      </div>
      <textarea className="input" rows={3} placeholder="Setup token" value={token} onChange={(e) => setToken(e.target.value)} />
      <ErrorNote error={claim.error} />
      <div className="row">
        <Button variant="primary" onClick={() => claim.mutate()} disabled={token.trim().length < 20 || claim.isPending}>
          {claim.isPending ? 'Connecting…' : 'Connect'}
        </Button>
        <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
      </div>
    </div>
  )
}

function Evaluation() {
  const rows = [
    ['Tiller (current)', 'Google Sheet kept current by Tiller; read here through a service account.', 'Existing subscription', 'Broad bank coverage, you already categorize there', 'Sheet is the middleman; no holdings detail'],
    ['SimpleFIN Bridge', 'Direct pull of accounts, balances, transactions and some holdings.', '~$15/year', 'Cheap, built for personal apps, no sheet needed', 'Up to 90 days per pull; coverage varies by bank'],
    ['Plaid / Yodlee', 'Commercial aggregation APIs.', 'Per-connection pricing', 'Best coverage', 'Production access requires an application and security review aimed at businesses; not practical for a household app'],
  ]
  return (
    <Card style={{ padding: 0, gap: 0 }}>
      <div style={{ padding: 'var(--space-3) var(--space-4) 0' }}>
        <div className="card-kicker">Evaluation</div>
        <div className="card-title">Automated bank data options</div>
      </div>
      <div style={{ overflowX: 'auto' }}><table className="table">
        <thead><tr><th>Option</th><th>What it is</th><th>Cost</th><th>Pros</th><th>Cons</th></tr></thead>
        <tbody>{rows.map((r) => <tr key={r[0]}>{r.map((c, i) => <td key={i} className={i === 0 ? 'nowrap' : 'small'}>{c}</td>)}</tr>)}</tbody>
      </table></div>
      <div className="small text-muted" style={{ padding: '0 var(--space-4) var(--space-3)' }}>
        Recommendation: keep Tiller while it's paid for and connect SimpleFIN alongside it; both feeds are de-duplicated against each
        other, so you can compare coverage before letting the Tiller subscription lapse.
      </div>
    </Card>
  )
}

function BalancesCard() {
  const q = useQuery({ queryKey: ['balances'], queryFn: () => get<Balances>('/balances') })
  const b = q.data
  if (!b || b.accounts.length === 0) return null
  return (
    <Card style={{ padding: 0, gap: 0 }}>
      <div className="row" style={{ padding: 'var(--space-3) var(--space-4) 0', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div><div className="card-kicker">Balances</div><div className="card-title">Net worth {money(b.net_worth)}</div></div>
        <div className="small text-muted">Assets {money(b.assets)} · Liabilities {money(b.liabilities)}</div>
      </div>
      <div style={{ overflowX: 'auto' }}><table className="table">
        <thead><tr><th>Account</th><th>Institution</th><th>Type</th><th className="num">Balance</th><th className="num">30-day change</th><th>As of</th><th>Source</th></tr></thead>
        <tbody>
          {b.accounts.map((a) => {
            const change = a.balance_30d_ago === null ? null : a.balance - a.balance_30d_ago
            return (
              <tr key={a.account_id}>
                <td>{a.account}</td>
                <td className="text-muted">{a.institution ?? '—'}</td>
                <td className="text-muted">{a.account_type.replace('_', ' ')}</td>
                <td className="num">{money(LIABILITIES.includes(a.account_type) ? -Math.abs(a.balance) : a.balance)}</td>
                <td className="num text-muted">{change === null ? '—' : money(change, true)}</td>
                <td className="nowrap text-muted">{fullDate(a.as_of)}</td>
                <td className="text-muted">{a.source}</td>
              </tr>
            )
          })}
        </tbody>
      </table></div>
    </Card>
  )
}

function HoldingsCard() {
  const q = useQuery({ queryKey: ['holdings'], queryFn: () => get<HoldingRow[]>('/holdings') })
  if (!q.data?.length) return null
  const total = q.data.reduce((s, h) => s + (h.market_value ?? 0), 0)
  return (
    <Card style={{ padding: 0, gap: 0 }}>
      <div style={{ padding: 'var(--space-3) var(--space-4) 0' }}>
        <div className="card-kicker">Investments</div><div className="card-title">Holdings · {money(total)}</div>
      </div>
      <div style={{ overflowX: 'auto' }}><table className="table">
        <thead><tr><th>Account</th><th>Symbol</th><th>Description</th><th className="num">Shares</th><th className="num">Value</th><th className="num">Gain</th><th>As of</th></tr></thead>
        <tbody>
          {q.data.map((h, i) => (
            <tr key={i}>
              <td>{h.account}</td>
              <td>{h.symbol ?? '—'}</td>
              <td className="text-muted">{h.description ?? ''}</td>
              <td className="num">{h.shares?.toLocaleString(undefined, { maximumFractionDigits: 4 }) ?? '—'}</td>
              <td className="num">{h.market_value === null ? '—' : money(h.market_value)}</td>
              <td className="num text-muted">{h.market_value !== null && h.cost_basis !== null ? money(h.market_value - h.cost_basis, true) : '—'}</td>
              <td className="nowrap text-muted">{fullDate(h.as_of)}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
    </Card>
  )
}
