import { useId, useState } from 'react'
import { type MerchantHit } from '../api'
import { shortDate } from '../format'
import { useMerchantSearch } from '../hooks'

/** Free-text merchant name with suggestions from existing merchants. */
export function MerchantInput({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  const listId = useId()
  const hits = useMerchantSearch(value)
  return (
    <>
      <input className="input" list={listId} value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      <datalist id={listId}>
        {(hits.data ?? []).map((m) => <option key={m.key} value={m.name}>{m.count} transactions</option>)}
      </datalist>
    </>
  )
}

/** Search box listing merchants to pick one (e.g. a merge target). */
export function MerchantPicker({ exclude, onPick }: { exclude?: string; onPick: (m: MerchantHit) => void }) {
  const [q, setQ] = useState('')
  const hits = useMerchantSearch(q)
  const rows = (hits.data ?? []).filter((m) => m.key !== exclude)
  return (
    <div className="stack">
      <input className="input" autoFocus placeholder="Search merchants…" value={q} onChange={(e) => setQ(e.target.value)} />
      {rows.map((m) => (
        <button key={m.key} type="button" className="btn btn-ghost" style={{ justifyContent: 'space-between', display: 'flex' }}
          onClick={() => onPick(m)}>
          <span>{m.name} <span className="text-muted small">· {m.key}</span></span>
          <span className="text-muted small">{m.count} · last {shortDate(m.last_date)}</span>
        </button>
      ))}
      {q.trim().length >= 2 && hits.isSuccess && rows.length === 0 && <div className="small text-muted">No other merchants match.</div>}
    </div>
  )
}
