import { Fragment, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, post, put, type AssetRow, type AssetsInfo, type ValuationMethod } from '../api'
import { useAccounts } from '../hooks'
import { fullDate, iso, money } from '../format'
import { accountPath } from '../links'
import { Button, Card, DateInput, ErrorNote, Field } from './ui'

const METHODS: { value: ValuationMethod; label: string }[] = [
  { value: 'rentcast', label: 'RentCast estimate (monthly)' },
  { value: 'depreciation', label: 'Depreciation curve (monthly)' },
  { value: 'manual', label: 'Manual' },
]

function useInvalidate() {
  const qc = useQueryClient()
  return () => { for (const k of ['assets', 'balances', 'details', 'accounts']) qc.invalidateQueries({ queryKey: [k] }) }
}

function methodLabel(a: AssetRow, defaultRate: number) {
  if (a.method === 'rentcast') return 'RentCast'
  if (a.method === 'depreciation') return `Depreciation ${+((a.depreciation_rate ?? defaultRate) * 100).toFixed(2)}%/yr`
  return 'Manual'
}

type Mode = { kind: 'add' } | { kind: 'edit' | 'value'; id: number } | null

export function AssetsCard() {
  const q = useQuery({ queryKey: ['assets'], queryFn: () => get<AssetsInfo>('/assets') })
  const [mode, setMode] = useState<Mode>(null)
  const invalidate = useInvalidate()
  const refresh = useMutation({ mutationFn: (id: number) => post(`/assets/${id}/refresh`), onSettled: invalidate })
  const info = q.data
  const rows = info?.assets ?? []
  const total = rows.reduce((s, a) => s + (a.value ?? 0), 0)
  const equity = rows.reduce((s, a) => s + (a.equity ?? a.value ?? 0), 0)
  const done = () => { setMode(null); invalidate() }
  return (
    <Card style={{ padding: 0, gap: 0 }}>
      <div className="row" style={{ padding: 'var(--space-3) var(--space-4) 0', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div><div className="card-kicker">Assets</div><div className="card-title">Home &amp; vehicles · {money(total)}</div></div>
        <div className="row">
          {rows.some((a) => a.loan_account_id) && <span className="small text-muted">Equity after loans {money(equity)}</span>}
          <Button onClick={() => setMode({ kind: 'add' })} disabled={!info || mode?.kind === 'add'}>Add asset</Button>
        </div>
      </div>
      <div style={{ padding: '0 var(--space-4)' }}><ErrorNote error={q.error || refresh.error} /></div>
      {mode?.kind === 'add' && info && (
        <div style={{ padding: 'var(--space-3) var(--space-4)', borderBottom: '1px solid var(--color-divider)' }}>
          <AssetForm info={info} onDone={done} onCancel={() => setMode(null)} />
        </div>
      )}
      <div style={{ overflowX: 'auto' }}><table className="table">
        <thead><tr><th>Asset</th><th style={{ textAlign: 'right' }}>Value</th><th>As of</th><th>Valuation</th>
          <th>Loan</th><th style={{ textAlign: 'right' }}>Equity</th><th /></tr></thead>
        <tbody>
          {rows.map((a) => {
            const open = mode && mode.kind !== 'add' && mode.id === a.account_id ? mode.kind : null
            const r = a.last_result
            const range = a.value_source === 'rentcast' && r.low && r.high ? `Range ${money(r.low)} – ${money(r.high)}` : undefined
            return (
              <Fragment key={a.account_id}>
                <tr>
                  <td>
                    <Link to={accountPath(a.account_id)}>{a.name}</Link>
                    <div className="small text-muted">
                      {a.account_type}{a.address ? ` · ${a.address}` : ''}
                      {a.purchase_price !== null ? ` · bought ${money(a.purchase_price)}${a.purchase_date ? ` ${fullDate(a.purchase_date)}` : ''}` : ''}
                    </div>
                  </td>
                  <td className="num" title={range}>{money(a.value)}</td>
                  <td className="nowrap text-muted">{a.value_as_of ? `${fullDate(a.value_as_of)} · ${a.value_source}` : '—'}</td>
                  <td>
                    {methodLabel(a, info!.default_depreciation_rate)}
                    {a.last_error && <div className="small" style={{ color: '#8a2b2b' }}>⚠ {a.last_error}</div>}
                  </td>
                  <td className="text-muted">{a.loan_name ? <>{a.loan_name} <span className="nowrap">{money(a.loan_balance === null ? null : -Math.abs(a.loan_balance))}</span></> : '—'}</td>
                  <td className="num">{money(a.equity)}</td>
                  <td className="nowrap" style={{ textAlign: 'right' }}>
                    <button className="btn btn-ghost small" onClick={() => setMode({ kind: 'value', id: a.account_id })}>Record value</button>
                    {a.method !== 'manual' && (
                      <button className="btn btn-ghost small" disabled={refresh.isPending} onClick={() => refresh.mutate(a.account_id)}>
                        {refresh.isPending && refresh.variables === a.account_id ? 'Updating…' : 'Update now'}
                      </button>
                    )}
                    <button className="btn btn-ghost small" onClick={() => setMode({ kind: 'edit', id: a.account_id })}>Edit</button>
                  </td>
                </tr>
                {open && (
                  <tr className="selected"><td colSpan={7}>
                    {open === 'edit'
                      ? <AssetForm info={info!} asset={a} onDone={done} onCancel={() => setMode(null)} />
                      : <ValueForm asset={a} onDone={done} onCancel={() => setMode(null)} />}
                  </td></tr>
                )}
              </Fragment>
            )
          })}
          {info && rows.length === 0 && (
            <tr><td colSpan={7} className="text-muted">No homes or vehicles yet. Add one to include it in net worth.</td></tr>
          )}
        </tbody>
      </table></div>
      <div className="card-meta" style={{ padding: 'var(--space-2) var(--space-4) var(--space-3)' }}>
        Automatic values refresh on the 1st of each month. A recorded value (e.g. from KBB or an appraisal) restarts a depreciation curve from that value.
      </div>
    </Card>
  )
}

function AssetForm({ info, asset, onDone, onCancel }: { info: AssetsInfo; asset?: AssetRow; onDone: () => void; onCancel: () => void }) {
  const accounts = useAccounts()
  const [name, setName] = useState('')
  const [type, setType] = useState<'property' | 'vehicle'>(asset?.account_type ?? 'property')
  const [method, setMethod] = useState<ValuationMethod>(asset?.method ?? (info.rentcast_configured ? 'rentcast' : 'manual'))
  const [address, setAddress] = useState(asset?.address ?? '')
  const [price, setPrice] = useState(asset?.purchase_price?.toString() ?? '')
  const [bought, setBought] = useState(asset?.purchase_date ?? '')
  const [rate, setRate] = useState(asset?.depreciation_rate !== null && asset?.depreciation_rate !== undefined ? String(+(asset.depreciation_rate * 100).toFixed(2)) : '')
  const [loan, setLoan] = useState<number | null>(asset?.loan_account_id ?? null)
  const [value, setValue] = useState('')
  const loans = (accounts.data ?? []).filter((a) => ['loan', 'mortgage'].includes(a.account_type) && (!a.is_closed || a.id === loan))
  const save = useMutation({
    mutationFn: () => {
      const body = {
        method, loan_account_id: loan, address: method === 'rentcast' ? address.trim() : asset?.address ?? null,
        purchase_price: price ? Number(price).toFixed(2) : null, purchase_date: bought || null,
        depreciation_rate: rate ? (Number(rate) / 100).toFixed(4) : null,
      }
      return asset
        ? put(`/assets/${asset.account_id}`, body)
        : post('/assets', { ...body, name: name.trim(), account_type: type, value: value ? Number(value).toFixed(2) : null })
    },
    onSuccess: onDone,
  })
  const pickType = (t: 'property' | 'vehicle') => {
    setType(t)
    setMethod(t === 'vehicle' ? 'depreciation' : info.rentcast_configured ? 'rentcast' : 'manual')
  }
  const valid = (asset || name.trim()) && (method !== 'rentcast' || address.trim())
    && (method !== 'depreciation' || (price && bought) || value || (asset && asset.value !== null))
  return (
    <div className="stack">
      <div className="grid-form" style={{ alignItems: 'end' }}>
        {!asset && (
          <>
            <Field label="Name"><input className="input" value={name} placeholder="e.g. 8904 Longmead Ct" onChange={(e) => setName(e.target.value)} /></Field>
            <Field label="Type">
              <select className="input" value={type} onChange={(e) => pickType(e.target.value as 'property' | 'vehicle')}>
                <option value="property">Home / property</option><option value="vehicle">Vehicle</option>
              </select>
            </Field>
          </>
        )}
        <Field label="Valuation">
          <select className="input" value={method} onChange={(e) => setMethod(e.target.value as ValuationMethod)}>
            {METHODS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </Field>
        {method === 'rentcast' && (
          <Field label="Address"><input className="input" value={address} placeholder="Street, City, State, Zip" onChange={(e) => setAddress(e.target.value)} /></Field>
        )}
        <Field label="Purchase price"><input className="input" type="number" min="0" step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} /></Field>
        <Field label="Purchase date"><DateInput className="input" value={bought} onChange={setBought} /></Field>
        {method === 'depreciation' && (
          <Field label="Depreciation %/yr">
            <input className="input" type="number" min="0" max="50" step="0.5" value={rate}
              placeholder={String(info.default_depreciation_rate * 100)} onChange={(e) => setRate(e.target.value)} />
          </Field>
        )}
        <Field label="Linked loan">
          <select className="input" value={loan ?? ''} onChange={(e) => setLoan(e.target.value ? Number(e.target.value) : null)}>
            <option value="">None</option>
            {loans.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </Field>
        {!asset && (
          <Field label="Current value (optional)"><input className="input" type="number" min="0" step="0.01" value={value} onChange={(e) => setValue(e.target.value)} /></Field>
        )}
      </div>
      {method === 'rentcast' && !info.rentcast_configured && (
        <div className="callout small">
          Set <code>RENTCAST_API_KEY</code> in <code>.env</code> (free key at app.rentcast.io, 50 lookups/month) and restart; until then this asset shows an error and keeps its last value.
        </div>
      )}
      {method === 'depreciation' && (
        <div className="small text-muted">Declines from the purchase price, or from the latest recorded value, by the yearly rate (default {info.default_depreciation_rate * 100}%).</div>
      )}
      <ErrorNote error={save.error} />
      <div className="row">
        <Button variant="primary" disabled={!valid || save.isPending} onClick={() => save.mutate()}>{save.isPending ? 'Saving…' : 'Save'}</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  )
}

function ValueForm({ asset, onDone, onCancel }: { asset: AssetRow; onDone: () => void; onCancel: () => void }) {
  const [value, setValue] = useState(asset.value?.toString() ?? '')
  const [asOf, setAsOf] = useState(iso(new Date()))
  const save = useMutation({
    mutationFn: () => post(`/assets/${asset.account_id}/valuations`, { value: Number(value).toFixed(2), as_of: asOf || null }),
    onSuccess: onDone,
  })
  return (
    <div className="stack">
      <div className="grid-form" style={{ alignItems: 'end' }}>
        <Field label="Value"><input className="input" type="number" min="0" step="0.01" value={value} autoFocus onChange={(e) => setValue(e.target.value)} /></Field>
        <Field label="As of"><DateInput className="input" value={asOf} max={iso(new Date())} onChange={setAsOf} /></Field>
      </div>
      <ErrorNote error={save.error} />
      <div className="row">
        <Button variant="primary" disabled={value === '' || Number(value) < 0 || save.isPending} onClick={() => save.mutate()}>Save</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  )
}
