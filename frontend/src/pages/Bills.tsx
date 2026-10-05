import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { del, get, post, upload, type Series, type SeriesPoint, type Statement, type Usage } from '../api'
import { CategorySelect } from '../components/CategorySelect'
import { LineChart } from '../components/LineChart'
import { Button, Card, ErrorNote, Field, SortTh } from '../components/ui'
import { fullDate, money, shortDate } from '../format'
import { categoryPath, txnPath } from '../links'
import { sortRows, useUrlSort } from '../urlState'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const periodLabel = (s: { period_start: string | null; period_end: string | null; statement_date?: string | null }) =>
  s.period_start && s.period_end ? `${shortDate(s.period_start)} – ${fullDate(s.period_end)}`
    : s.statement_date ? fullDate(s.statement_date) : '—'
const monthOf = (d: string | null) => (d ? `${MONTHS[Number(d.slice(5, 7)) - 1]} ${d.slice(2, 4)}` : '')
const fmtUsage = (v: string | number | null, unit: string | null) =>
  v === null ? '—'
    : `${Number(v).toLocaleString(undefined, { maximumFractionDigits: Math.abs(Number(v)) >= 100 ? 0 : 2 })} ${unit ?? ''}`.trim()

export function Bills() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const fileInput = useRef<HTMLInputElement>(null)
  const series = useQuery({ queryKey: ['series'], queryFn: () => get<Series[]>('/statement-series') })
  const pending = useQuery({
    queryKey: ['statements', 'pending'],
    queryFn: () => get<Statement[]>('/statements', { status: ['processing', 'suggested', 'failed'] }),
    refetchInterval: (q) => (q.state.data?.some((s) => s.status === 'processing') ? 2000 : false),
  })
  const selected = params.get('series') ? Number(params.get('series')) : series.data?.[0]?.id ?? null
  const send = useMutation({
    mutationFn: (f: File) => upload<Statement>('/statements', f),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['statements'] }),
  })

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <h2>Bills &amp; statements</h2>
          <div className="text-muted subtitle">Documents attached to transactions, with usage extracted per period</div>
        </div>
        <input ref={fileInput} type="file" hidden accept=".pdf,.png,.jpg,.jpeg,.webp,.heic,.gif,.tif,.tiff"
          onChange={(e) => { const f = e.target.files?.[0]; if (f) send.mutate(f); e.target.value = '' }} />
        <Button variant="primary" onClick={() => fileInput.current?.click()} disabled={send.isPending}>
          {send.isPending ? 'Uploading…' : 'Upload statement'}
        </Button>
      </header>
      <ErrorNote error={send.error || series.error || pending.error} />
      <div className="split-nav">
        <nav className="side-nav">
          {(series.data ?? []).map((s) => (
            <button key={s.id} className={`nav-item${s.id === selected ? ' active' : ''}`}
              style={{ border: `1px solid ${s.id === selected ? 'var(--color-accent-300)' : 'var(--color-divider)'}` }}
              onClick={() => setParams({ series: String(s.id) })}>
              <span className="label">{s.name}</span><span className="small" style={{ opacity: 0.7 }}>{s.statement_count}</span>
            </button>
          ))}
          {series.data?.length === 0 && <div className="small text-muted">Series appear as you approve statements.</div>}
        </nav>
        <div className="stack-3" style={{ minWidth: 0 }}>
          {(pending.data ?? []).map((s) => <Suggestion key={`${s.id}-${s.status}`} st={s} series={series.data ?? []} />)}
          {selected && <SeriesView id={selected} series={series.data?.find((s) => s.id === selected)} />}
        </div>
      </div>
    </section>
  )
}

function Suggestion({ st, series }: { st: Statement; series: Series[] }) {
  const qc = useQueryClient()
  const sug = st.suggestion
  const [editing, setEditing] = useState(false)
  const [seriesId, setSeriesId] = useState<number | null>(sug.series_id ?? null)
  const [newName, setNewName] = useState(sug.new_series_name ?? '')
  const [newCat, setNewCat] = useState<number | null>(sug.new_series_category_id ?? null)
  const [links, setLinks] = useState<number[]>(sug.transaction_ids ?? [])
  const [fields, setFields] = useState({
    vendor: st.vendor ?? '', period_start: st.period_start ?? '', period_end: st.period_end ?? '',
    due_date: st.due_date ?? '', amount_due: st.amount_due ?? '',
  })
  const [usage, setUsage] = useState<Usage[]>(st.usage)
  const done = () => {
    qc.invalidateQueries({ queryKey: ['statements'] })
    qc.invalidateQueries({ queryKey: ['series'] })
    qc.invalidateQueries({ queryKey: ['transactions'] })
  }
  const approve = useMutation({
    mutationFn: () => post<Statement>(`/statements/${st.id}/approve`, {
      series_id: seriesId, new_series_name: seriesId ? null : newName, new_series_category_id: seriesId ? null : newCat,
      transaction_ids: links, vendor: fields.vendor || null, period_start: fields.period_start || null,
      period_end: fields.period_end || null, due_date: fields.due_date || null, amount_due: fields.amount_due || null,
      usage: usage.map((u) => ({ metric: u.metric, value: u.value, unit: u.unit, is_primary: u.is_primary })),
    }),
    onSuccess: done,
  })
  const dismiss = useMutation({ mutationFn: () => post(`/statements/${st.id}/dismiss`), onSuccess: done })
  const retry = useMutation({ mutationFn: () => post(`/statements/${st.id}/reprocess`), onSuccess: done })
  const remove = useMutation({ mutationFn: () => del(`/statements/${st.id}`), onSuccess: done })
  const primary = usage.find((u) => u.is_primary) ?? usage[0]

  if (st.status === 'processing') {
    return (
      <Card>
        <div className="card-kicker">Reading document</div>
        <div className="card-title">{st.filename}</div>
        <div className="card-meta">Gemini is extracting vendor, period, amount and usage…</div>
      </Card>
    )
  }
  if (st.status === 'failed') {
    return (
      <Card>
        <div className="card-kicker">Extraction failed</div>
        <div className="card-title">{st.filename}</div>
        <div className="callout error">{st.error?.split('\n')[0]}</div>
        <div className="row">
          <Button onClick={() => retry.mutate()} disabled={retry.isPending}>Try again</Button>
          <Button variant="ghost" onClick={() => remove.mutate()}>Delete</Button>
        </div>
      </Card>
    )
  }
  const candidates = sug.candidates ?? []
  return (
    <Card style={{ borderColor: 'var(--color-accent-400)', gap: 'var(--space-3)' }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div>
          <div className="card-kicker">Suggested assignment · needs approval</div>
          <div className="card-title">
            <a href={`/api/attachments/${st.attachment_id}/content`} target="_blank" rel="noreferrer">{st.filename}</a>
          </div>
          <div className="small text-muted">{st.summary}</div>
        </div>
        <span className="tag tag-accent">Uploaded {fullDate(st.created_at)}</span>
      </div>

      {!editing ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: 'var(--space-3)', fontSize: 13 }}>
          <div><div className="text-muted" style={{ fontSize: 11 }}>Link to</div>
            <div style={{ fontWeight: 500 }}>
              {links.length === 0 ? 'No matching payment found' : candidates.filter((c) => links.includes(c.transaction_id))
                .map((c) => `${shortDate(c.date)} · ${c.description} · ${money(c.amount)}`).join('; ')}
            </div></div>
          <div><div className="text-muted" style={{ fontSize: 11 }}>Series</div>
            <div style={{ fontWeight: 500 }}>{seriesId ? series.find((s) => s.id === seriesId)?.name : `${newName} (new)`}</div></div>
          <div><div className="text-muted" style={{ fontSize: 11 }}>Period</div>
            <div style={{ fontWeight: 500 }}>{periodLabel(st)}</div></div>
          <div><div className="text-muted" style={{ fontSize: 11 }}>Amount · usage</div>
            <div style={{ fontWeight: 500 }}>{money(st.amount_due)}{primary ? ` · ${fmtUsage(primary.value, primary.unit)}` : ''}</div></div>
        </div>
      ) : (
        <div className="stack-3">
          <div className="grid-form">
            <Field label="Series">
              <select className="input" value={seriesId ?? ''} onChange={(e) => setSeriesId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">New series…</option>
                {series.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </Field>
            {!seriesId && <Field label="New series name"><input className="input" value={newName} onChange={(e) => setNewName(e.target.value)} /></Field>}
            {!seriesId && <Field label="Category for payments"><CategorySelect value={newCat} onChange={setNewCat} emptyLabel="Leave as is" /></Field>}
          </div>
          <div className="grid-form">
            <Field label="Vendor"><input className="input" value={fields.vendor} onChange={(e) => setFields({ ...fields, vendor: e.target.value })} /></Field>
            <Field label="Period start"><input className="input" type="date" value={fields.period_start} onChange={(e) => setFields({ ...fields, period_start: e.target.value })} /></Field>
            <Field label="Period end"><input className="input" type="date" value={fields.period_end} onChange={(e) => setFields({ ...fields, period_end: e.target.value })} /></Field>
            <Field label="Due"><input className="input" type="date" value={fields.due_date} onChange={(e) => setFields({ ...fields, due_date: e.target.value })} /></Field>
            <Field label="Amount"><input className="input" type="number" step="0.01" value={fields.amount_due} onChange={(e) => setFields({ ...fields, amount_due: e.target.value })} /></Field>
          </div>
          {usage.length > 0 && (
            <table className="table" style={{ fontSize: 13 }}>
              <thead><tr><th>Usage metric</th><th>Value</th><th>Unit</th><th>Primary</th></tr></thead>
              <tbody>
                {usage.map((u, i) => (
                  <tr key={i}>
                    <td>{u.metric}</td>
                    <td><input className="input" type="number" value={u.value}
                      onChange={(e) => setUsage(usage.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} /></td>
                    <td><input className="input" value={u.unit ?? ''}
                      onChange={(e) => setUsage(usage.map((x, j) => (j === i ? { ...x, unit: e.target.value } : x)))} /></td>
                    <td><input type="radio" name={`primary-${st.id}`} checked={u.is_primary}
                      onChange={() => setUsage(usage.map((x, j) => ({ ...x, is_primary: j === i })))} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className="stack">
            <div className="card-kicker">Link to payment</div>
            {candidates.length === 0 && <div className="small text-muted">No payment of a matching amount near the due date.</div>}
            {candidates.map((c) => (
              <label key={c.transaction_id} className="radio" style={{ alignItems: 'flex-start' }}>
                <input type="checkbox" checked={links.includes(c.transaction_id)}
                  onChange={(e) => setLinks(e.target.checked ? [...links, c.transaction_id] : links.filter((x) => x !== c.transaction_id))} />
                <span>{shortDate(c.date)} · {c.description} · {money(c.amount)}
                  <span className="small text-muted"> — {c.reason}</span></span>
              </label>
            ))}
          </div>
        </div>
      )}
      <ErrorNote error={approve.error || dismiss.error} />
      <div className="row">
        <Button variant="primary" onClick={() => approve.mutate()} disabled={approve.isPending || (!seriesId && !newName.trim())}>Approve</Button>
        <Button onClick={() => setEditing(!editing)}>{editing ? 'Done editing' : 'Edit fields'}</Button>
        <Button variant="ghost" onClick={() => dismiss.mutate()} disabled={dismiss.isPending}>Dismiss</Button>
        {sug.series_reason && <span className="small text-muted" style={{ marginLeft: 'auto' }}>{sug.series_reason}</span>}
      </div>
    </Card>
  )
}

function SeriesView({ id, series }: { id: number; series?: Series }) {
  const hist = useQuery({ queryKey: ['series', id, 'history'], queryFn: () => get<SeriesPoint[]>(`/statement-series/${id}/history`) })
  const sort = useUrlSort({ statement: 'asc', period: 'desc', usage: 'desc', cost: 'desc', paid: 'desc', amount: 'desc' })
  const pts = hist.data ?? []
  const num = (v: string | null) => (v === null ? null : Number(v))
  const table = sortRows([...pts].reverse(), sort, {
    statement: (p) => p.filename, period: (p) => p.period_end ?? p.statement_date, usage: (p) => num(p.usage_value),
    cost: (p) => num(p.cost_per_unit), paid: (p) => p.transactions[0]?.txn_date, amount: (p) => num(p.amount_due),
  })
  const last12 = pts.slice(-12)
  const withUsage = last12.filter((p) => p.usage_value !== null)
  const avg = withUsage.length ? withUsage.reduce((a, p) => a + Number(p.usage_value), 0) / withUsage.length : null
  const latest = [...pts].reverse().find((p) => p.usage_value !== null)
  const unit = series?.unit ?? latest?.usage_unit ?? ''
  return (
    <>
      <Card style={{ gap: 'var(--space-2)' }}>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div>
            <div className="card-kicker">
              {series?.category_id && <><Link to={categoryPath(series.category_id)}>{series.category_name}</Link> · </>}
              {series?.vendor ?? series?.name}
            </div>
            <div className="card-title">Usage · {unit || 'per statement'} per statement period</div>
          </div>
          <div className="row small muted-2" style={{ gap: 'var(--space-4)' }}>
            <span>12-mo avg <strong style={{ color: 'var(--color-text)' }}>{avg === null ? '—' : fmtUsage(avg, unit)}</strong></span>
            <span>Latest <strong style={{ color: 'var(--color-text)' }}>{latest ? fmtUsage(latest.usage_value, latest.usage_unit) : '—'}</strong></span>
          </div>
        </div>
        <LineChart points={last12.map((p) => ({
          label: monthOf(p.period_end ?? p.statement_date),
          value: p.usage_value === null ? null : Number(p.usage_value),
          title: `${periodLabel(p)}: ${fmtUsage(p.usage_value, p.usage_unit)} · ${money(p.amount_due)}`,
        }))} format={(v) => fmtUsage(v, unit)} />
      </Card>
      <Card className="table-card">
        <table className="table">
          <thead>
            <tr><SortTh s={sort} k="statement">Statement</SortTh><SortTh s={sort} k="period">Period</SortTh>
              <SortTh s={sort} k="usage">Usage</SortTh><SortTh s={sort} k="cost">Cost / unit</SortTh>
              <SortTh s={sort} k="paid">Linked transaction</SortTh><SortTh s={sort} k="amount" right>Amount</SortTh></tr>
          </thead>
          <tbody>
            {table.map((p) => (
              <tr key={p.statement_id}>
                <td style={{ fontWeight: 500 }}>
                  <a href={`/api/attachments/${p.attachment_id}/content`} target="_blank" rel="noreferrer">{p.filename}</a>
                </td>
                <td className="text-muted nowrap">{periodLabel(p)}</td>
                <td className="nowrap">{fmtUsage(p.usage_value, p.usage_unit)}</td>
                <td className="nowrap text-muted">{p.cost_per_unit ? `$${Number(p.cost_per_unit).toFixed(4)}` : '—'}</td>
                <td>
                  {p.transactions.length === 0 ? <span className="text-muted small">— (no payment linked)</span> : p.transactions.map((t) => (
                    <div key={t.id} className="small">
                      <Link to={txnPath(t.id)}>{shortDate(t.txn_date)} · {t.description}</Link>
                    </div>
                  ))}
                </td>
                <td className="num">{money(p.amount_due)}</td>
              </tr>
            ))}
            {pts.length === 0 && !hist.isLoading && <tr><td colSpan={6} className="text-muted">No approved statements yet.</td></tr>}
          </tbody>
        </table>
      </Card>
    </>
  )
}
