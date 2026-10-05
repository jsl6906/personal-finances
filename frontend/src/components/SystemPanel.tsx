import { useQuery } from '@tanstack/react-query'
import { get } from '../api'
import { oneOf, sortRows, useUrl, useUrlSort } from '../urlState'
import { Card, ErrorNote, Seg, SortTh } from './ui'

interface Health {
  db: string; schema: string | null; schema_current: boolean; worker: boolean; ai_configured: boolean; smtp_configured: boolean
}
interface AiUsage {
  days: number; last_error: string | null
  items: { purpose: string; model: string; calls: number; errors: number; input_tokens: number; output_tokens: number; avg_latency_ms: number | null }[]
}

const ok = (good: boolean, yes = 'OK', no = 'Not configured') => (
  <span className={`tag ${good ? 'tag-accent' : 'tag-outline'}`}>{good ? yes : no}</span>
)

export function SystemPanel() {
  const [params, set] = useUrl()
  const days = oneOf(params, 'days', ['7', '30', '90'] as const, '30')
  const setDays = (d: typeof days) => set({ days: d === '30' ? null : d })
  const sort = useUrlSort({ purpose: 'asc', model: 'asc', calls: 'desc', errors: 'desc', input: 'desc', output: 'desc', latency: 'desc' })
  const health = useQuery({ queryKey: ['health'], queryFn: () => get<Health>('/health') })
  const usage = useQuery({ queryKey: ['ai-usage', days], queryFn: () => get<AiUsage>('/ai-usage', { days }) })
  const h = health.data
  const totals = (usage.data?.items ?? []).reduce(
    (t, i) => ({ calls: t.calls + i.calls, errors: t.errors + i.errors, tin: t.tin + i.input_tokens, tout: t.tout + i.output_tokens }),
    { calls: 0, errors: 0, tin: 0, tout: 0 },
  )

  return (
    <div className="stack-3">
      <ErrorNote error={health.error || usage.error} />
      <div className="grid-2">
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="card-kicker">Status</div>
          {h && (
            <div className="facts">
              <span className="text-muted">Database</span><span>{ok(h.db === 'ok', 'Connected', h.db)}</span>
              <span className="text-muted">Schema</span><span>{h.schema ?? '—'} {ok(h.schema_current, 'current', 'migration pending')}</span>
              <span className="text-muted">Background worker</span><span>{ok(h.worker, 'Running', 'Stopped')}</span>
              <span className="text-muted">Gemini</span><span>{ok(h.ai_configured)}</span>
              <span className="text-muted">Email (SMTP)</span><span>{ok(h.smtp_configured)}</span>
            </div>
          )}
        </Card>
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="card-kicker">Your data</div>
          <div className="card-title">Export everything</div>
          <p className="card-body" style={{ margin: 0 }}>
            A zip of CSV files: transactions, accounts, categories, budgets, bills and usage, balances, holdings and rules.
            Documents stay in the database, which Azure backs up with point-in-time restore.
          </p>
          <a className="btn btn-secondary" style={{ alignSelf: 'start' }} href="/api/export.zip">Download export</a>
        </Card>
      </div>
      <Card style={{ padding: 0, gap: 0 }}>
        <div className="row" style={{ padding: 'var(--space-3) var(--space-4)', justifyContent: 'space-between' }}>
          <div>
            <div className="card-kicker">Gemini usage</div>
            <div className="card-title">
              {totals.calls.toLocaleString()} calls · {(totals.tin / 1000).toFixed(0)}k in / {(totals.tout / 1000).toFixed(0)}k out tokens
              {totals.errors ? ` · ${totals.errors} errors` : ''}
            </div>
          </div>
          <Seg name="ai-days" value={days} onChange={setDays}
            options={[{ value: '7', label: '7 days' }, { value: '30', label: '30 days' }, { value: '90', label: '90 days' }]} />
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table className="table">
            <thead>
              <tr><SortTh s={sort} k="purpose">Purpose</SortTh><SortTh s={sort} k="model">Model</SortTh>
                <SortTh s={sort} k="calls" right>Calls</SortTh><SortTh s={sort} k="errors" right>Errors</SortTh>
                <SortTh s={sort} k="input" right>Input tokens</SortTh><SortTh s={sort} k="output" right>Output tokens</SortTh>
                <SortTh s={sort} k="latency" right>Avg latency</SortTh></tr>
            </thead>
            <tbody>
              {sortRows(usage.data?.items ?? [], sort, {
                purpose: (i) => i.purpose, model: (i) => i.model, calls: (i) => i.calls, errors: (i) => i.errors,
                input: (i) => i.input_tokens, output: (i) => i.output_tokens, latency: (i) => i.avg_latency_ms,
              }).map((i) => (
                <tr key={`${i.purpose}-${i.model}`}>
                  <td>{i.purpose.replace(/_/g, ' ')}</td>
                  <td className="text-muted">{i.model}</td>
                  <td className="num">{i.calls.toLocaleString()}</td>
                  <td className="num">{i.errors || '—'}</td>
                  <td className="num">{i.input_tokens.toLocaleString()}</td>
                  <td className="num">{i.output_tokens.toLocaleString()}</td>
                  <td className="num">{i.avg_latency_ms === null ? '—' : `${(i.avg_latency_ms / 1000).toFixed(1)} s`}</td>
                </tr>
              ))}
              {usage.data?.items.length === 0 && <tr><td className="text-muted">No Gemini calls in this period.</td></tr>}
            </tbody>
          </table>
        </div>
        {usage.data?.last_error && (
          <div className="small text-muted" style={{ padding: 'var(--space-2) var(--space-4)' }}>Last error: {usage.data.last_error}</div>
        )}
      </Card>
    </div>
  )
}
