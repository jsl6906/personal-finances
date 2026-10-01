import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { del, get, patch, post, upload, type Statement, type Transaction, type TxnNote, type TxnPair, type TxnSource } from '../api'
import { CategorySelect } from '../components/CategorySelect'
import { MerchantInput } from '../components/MerchantInput'
import { Button, Card, ErrorNote, Field, Icon } from '../components/ui'
import { fullDate, iso, money, monthName, parseIso, shortDate } from '../format'
import { useAccounts, useMembers, useTags } from '../hooks'
import { accountPath, categoryPath, merchantKey, merchantPath, txnPath } from '../links'
import { ORIGINS } from '../review'

type Draft = {
  txn_date: string
  description: string
  amount: string
  account_id: number | null
  category_id: number | null
  member_id: number | null
  notes: string
  check_number: string
  budget_spread_months: number | null
  tag_ids: number[]
  merchant_name: string
}

function toDraft(t: Transaction | null, defaults: Partial<Draft>): Draft {
  return {
    txn_date: t?.txn_date ?? iso(new Date()),
    description: t?.description ?? '',
    amount: t?.amount ?? '',
    account_id: t?.account_id ?? defaults.account_id ?? null,
    category_id: t?.category_id ?? null,
    member_id: t?.member_id ?? null,
    notes: t?.notes ?? '',
    check_number: t?.check_number ?? '',
    budget_spread_months: t?.budget_spread_months ?? null,
    tag_ids: t?.tags.map((x) => x.id) ?? [],
    merchant_name: t?.merchant_source === 'user' ? t.merchant_name ?? '' : '',
  }
}

type Props = {
  txn: Transaction | null
  defaultAccountId?: number | null
  standalone?: boolean
  onClose: () => void
  onSaved: (t: Transaction) => void
}

function SourceRow({ s, txn }: { s: TxnSource; txn: Transaction }) {
  const label = s.filename ?? `${ORIGINS[s.origin ?? ''] ?? s.origin ?? 'Import'}${s.source_type === 'spreadsheet' ? ' sheet' : ''}`
  const differs = (s.description && s.description !== txn.description) || (s.txn_date && s.txn_date !== txn.txn_date)
    || (s.amount !== null && Number(s.amount) !== Number(txn.amount))
  return (
    <div className="row" style={{ border: '1px solid var(--color-divider)', padding: 8, flexWrap: 'nowrap' }}>
      <Icon name="file" size={18} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 500, overflowWrap: 'anywhere' }}>{label}</div>
        <div className="small muted-2">
          {s.role === 'created' ? 'Created this transaction' : `Matched as duplicate${s.match_score ? ` (${Math.round(Number(s.match_score) * 100)}%)` : ''}`}
          {s.origin && s.filename ? ` · ${ORIGINS[s.origin] ?? s.origin}` : ''} · {shortDate(s.created_at)}
        </div>
        {differs && (
          <div className="small muted-2">
            Recorded as: {s.txn_date ? shortDate(s.txn_date) : ''} {s.description} {s.amount !== null ? money(s.amount) : ''}
          </div>
        )}
      </div>
      {s.attachment_id && s.filename && (
        <a className="btn btn-ghost small" href={`/api/attachments/${s.attachment_id}/content`} target="_blank" rel="noreferrer">Open</a>
      )}
      {s.import_batch_id && <Link className="btn btn-ghost small" to={`/import/${s.import_batch_id}`}>Import</Link>}
    </div>
  )
}

function NotesSection({ txnId }: { txnId: number }) {
  const qc = useQueryClient()
  const key = ['transactions', 'notes', txnId]
  const notes = useQuery({ queryKey: key, queryFn: () => get<TxnNote[]>(`/transactions/${txnId}/notes`) })
  const [text, setText] = useState('')
  const [editing, setEditing] = useState<{ id: number; body: string } | null>(null)
  const refresh = () => qc.invalidateQueries({ queryKey: key })
  const add = useMutation({
    mutationFn: () => post<TxnNote>(`/transactions/${txnId}/notes`, { body: text }),
    onSuccess: () => { setText(''); refresh() },
  })
  const save = useMutation({
    mutationFn: (n: { id: number; body: string }) => patch<TxnNote>(`/transactions/${txnId}/notes/${n.id}`, { body: n.body }),
    onSuccess: () => { setEditing(null); refresh() },
  })
  const remove = useMutation({
    mutationFn: (id: number) => del(`/transactions/${txnId}/notes/${id}`),
    onSuccess: refresh,
  })
  return (
    <div className="section-rule">
      <div className="card-kicker">More notes</div>
      {(notes.data ?? []).map((n) => (
        <div key={n.id} style={{ borderLeft: '2px solid var(--color-divider)', paddingLeft: 8 }}>
          {editing?.id === n.id ? (
            <>
              <textarea className="input" style={{ minHeight: 48 }} value={editing.body}
                onChange={(e) => setEditing({ id: n.id, body: e.target.value })} />
              <div className="row small" style={{ gap: 6 }}>
                <Button variant="ghost" className="small" disabled={!editing.body.trim() || save.isPending} onClick={() => save.mutate(editing)}>Save</Button>
                <Button variant="ghost" className="small" onClick={() => setEditing(null)}>Cancel</Button>
              </div>
            </>
          ) : (
            <>
              <div style={{ fontSize: 13, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{n.body}</div>
              <div className="row small muted-2" style={{ gap: 8 }}>
                <span>{n.source === 'import' ? `From ${n.filename ?? 'an import'}` : 'Added by you'} · {shortDate(n.created_at)}</span>
                <button type="button" className="link-btn" onClick={() => setEditing({ id: n.id, body: n.body })}>Edit</button>
                <button type="button" className="link-btn" disabled={remove.isPending}
                  onClick={() => confirm('Delete this note?') && remove.mutate(n.id)}>Delete</button>
              </div>
            </>
          )}
        </div>
      ))}
      <textarea className="input" style={{ minHeight: 48 }} placeholder="Add a note…" value={text} onChange={(e) => setText(e.target.value)} />
      <Button className="btn-block" style={{ margin: 0 }} disabled={!text.trim() || add.isPending} onClick={() => add.mutate()}>
        {add.isPending ? 'Adding…' : 'Add note'}
      </Button>
      <ErrorNote error={notes.error || add.error || save.error || remove.error} />
    </div>
  )
}

export function TransactionDetail({ txn, defaultAccountId, standalone = false, onClose, onSaved }: Props) {
  const qc = useQueryClient()
  const accounts = useAccounts()
  const members = useMembers()
  const tags = useTags()
  const [draft, setDraft] = useState<Draft>(() => toDraft(txn, { account_id: defaultAccountId ?? null }))
  const isNew = txn === null
  const dups = useQuery({
    queryKey: ['duplicates', 'for', txn?.id],
    queryFn: () => get<TxnPair[]>(`/duplicates/for/${txn!.id}`),
    enabled: !isNew,
  })
  const stmts = useQuery({
    queryKey: ['statements', 'for', txn?.id],
    queryFn: () => get<Statement[]>(`/statements/for-transaction/${txn!.id}`),
    enabled: !isNew,
  })
  const sources = useQuery({
    queryKey: ['transactions', 'sources', txn?.id],
    queryFn: () => get<TxnSource[]>(`/transactions/${txn!.id}/sources`),
    enabled: !isNew,
  })
  const fileInput = useRef<HTMLInputElement>(null)
  const navigate = useNavigate()
  const attach = useMutation({
    mutationFn: (f: File) => upload<Statement>('/statements', f, { transaction_ids: String(txn!.id) }),
    onSuccess: () => navigate('/bills'),
  })

  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }))
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['transactions'] })
    qc.invalidateQueries({ queryKey: ['summary'] })
    qc.invalidateQueries({ queryKey: ['details'] })
  }

  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        ...draft,
        notes: draft.notes || null,
        check_number: draft.check_number || null,
        merchant_name: draft.merchant_name.trim() || null,
        amount: Number(draft.amount).toFixed(2),
      }
      if (isNew) return post<Transaction>('/transactions', body)
      const orig = toDraft(txn, {})
      const origBody: Record<string, unknown> = {
        ...orig, notes: orig.notes || null, check_number: orig.check_number || null, merchant_name: orig.merchant_name || null,
        amount: Number(orig.amount).toFixed(2),
      }
      const changed = Object.fromEntries(
        Object.entries(body).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(origBody[k])),
      )
      return patch<Transaction>(`/transactions/${txn.id}`, changed)
    },
    onSuccess: (t) => {
      invalidate()
      onSaved(t)
    },
  })
  const remove = useMutation({
    mutationFn: () => del(`/transactions/${txn!.id}`),
    onSuccess: () => {
      invalidate()
      onClose()
    },
  })
  const accept = useMutation({
    mutationFn: () => post('/transactions/suggestions/accept', { ids: [txn!.id] }),
    onSuccess: () => {
      invalidate()
      set('category_id', txn!.suggested_category_id)
    },
  })

  const amountNum = Number(draft.amount || 0)
  const spread = draft.budget_spread_months !== null && draft.budget_spread_months > 1
  const valid = draft.description.trim() && draft.amount !== '' && !Number.isNaN(amountNum) && draft.txn_date

  return (
    <Card as="aside" className="detail">
      <div className="detail-body">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'nowrap' }}>
        <div style={{ minWidth: 0 }}>
          <div className="card-kicker">{isNew ? 'New transaction' : standalone ? 'Edit' : 'Transaction'}</div>
          <div className="card-title">{isNew ? 'Manual entry' : standalone ? 'Details' : txn.description}</div>
          {!isNew && !standalone && (
            <div className="text-muted small">
              {fullDate(txn.txn_date)} · added {fullDate(txn.created_at)} · {txn.source_type}
            </div>
          )}
          {!isNew && !standalone && (
            <div className="row small" style={{ gap: 10, marginTop: 4 }}>
              <Link to={txnPath(txn.id)}>Full page ›</Link>
              <Link to={merchantPath(merchantKey(txn))}>Merchant</Link>
              {txn.category_id && <Link to={categoryPath(txn.category_id)}>Category</Link>}
              {txn.account_id && <Link to={accountPath(txn.account_id)}>Account</Link>}
            </div>
          )}
        </div>
        {!standalone && <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">×</button>}
      </div>
      {!isNew && !standalone && <div className={`big-amount${amountNum > 0 ? ' pos' : ''}`}>{money(txn.amount, true)}</div>}

      {!isNew && txn.suggested_category_name && !txn.category_id && (
        <div className="callout">
          <strong>Suggested: {txn.suggested_category_name}</strong>
          {txn.suggestion_confidence && ` · ${Math.round(Number(txn.suggestion_confidence) * 100)}%`}
          <div className="small">{txn.suggestion_reason}</div>
          <Button variant="ghost" className="small" onClick={() => accept.mutate()} disabled={accept.isPending}>Accept</Button>
        </div>
      )}

      <div className="grid-form">
        <Field label="Date">
          <input className="input" type="date" value={draft.txn_date} onChange={(e) => set('txn_date', e.target.value)} />
        </Field>
        <Field label="Amount (− = out)">
          <input className="input" type="number" step="0.01" value={draft.amount} onChange={(e) => set('amount', e.target.value)} />
        </Field>
      </div>
      <Field label="Description">
        <input className="input" value={draft.description} onChange={(e) => set('description', e.target.value)} />
      </Field>
      <Field label="Merchant">
        <MerchantInput value={draft.merchant_name} onChange={(v) => set('merchant_name', v)}
          placeholder={txn && txn.merchant_source !== 'user' && txn.merchant_name ? `${txn.merchant_name} (automatic)` : 'Automatic from description'} />
      </Field>
      <Field label="Category">
        <CategorySelect value={draft.category_id} onChange={(v) => set('category_id', v)} />
      </Field>
      <Field label="Account">
        <select className="input" value={draft.account_id ?? ''}
          onChange={(e) => set('account_id', e.target.value ? Number(e.target.value) : null)}>
          <option value="">—</option>
          {(accounts.data ?? []).map((a) => (
            <option key={a.id} value={a.id}>{a.name}{a.institution_name ? ` · ${a.institution_name}` : ''}</option>
          ))}
        </select>
      </Field>
      <div className="grid-form">
        <Field label="Household member">
          <select className="input" value={draft.member_id ?? ''}
            onChange={(e) => set('member_id', e.target.value ? Number(e.target.value) : null)}>
            <option value="">Shared</option>
            {(members.data ?? []).map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </Field>
        <Field label="Check #">
          <input className="input" value={draft.check_number} onChange={(e) => set('check_number', e.target.value)} />
        </Field>
      </div>
      <Field label="Notes">
        <textarea className="input" style={{ minHeight: 56 }} value={draft.notes} onChange={(e) => set('notes', e.target.value)} />
      </Field>

      {!isNew && <NotesSection txnId={txn.id} />}

      {(tags.data?.length ?? 0) > 0 && (
        <div className="section-rule">
          <div className="card-kicker">Tags</div>
          <div className="row" style={{ gap: 6 }}>
            {tags.data!.map((tg) => {
              const on = draft.tag_ids.includes(tg.id)
              return (
                <button key={tg.id} type="button" className={`tag chip${on ? ' on' : ''}`}
                  onClick={() => set('tag_ids', on ? draft.tag_ids.filter((x) => x !== tg.id) : [...draft.tag_ids, tg.id])}>
                  {tg.name}
                </button>
              )
            })}
          </div>
        </div>
      )}

      <div className="section-rule">
        <div className="card-kicker">Budget treatment</div>
        <label className="radio">
          <input type="radio" name="spread" checked={draft.budget_spread_months === null} onChange={() => set('budget_spread_months', null)} />
          <span className="dot" />Default — spread rules apply, otherwise {draft.txn_date ? monthName(parseIso(draft.txn_date)) : 'its month'}
        </label>
        <label className="radio">
          <input type="radio" name="spread" checked={draft.budget_spread_months === 1} onChange={() => set('budget_spread_months', 1)} />
          <span className="dot" />Count only in {draft.txn_date ? monthName(parseIso(draft.txn_date)) : 'its month'}
        </label>
        <label className="radio">
          <input type="radio" name="spread" checked={spread} onChange={() => set('budget_spread_months', 12)} />
          <span className="dot" />Spread over
          <input className="input compact" type="number" min={2} max={60} style={{ width: 64 }} disabled={!spread}
            value={spread ? draft.budget_spread_months! : 12}
            onChange={(e) => set('budget_spread_months', Math.max(2, Math.min(60, Number(e.target.value) || 2)))} />
          months ({money(Math.abs(amountNum) / (spread ? draft.budget_spread_months! : 12))}/mo)
        </label>
      </div>

      {!isNew && (
        <div className="section-rule">
          <div className="card-kicker">Statement</div>
          {(stmts.data ?? []).map((s) => (
            <div key={s.id} className="row" style={{ border: '1px solid var(--color-divider)', padding: 8, flexWrap: 'nowrap' }}>
              <Icon name="file" size={18} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 500 }}>{s.filename}</div>
                <div className="small muted-2">
                  {s.series_name ?? s.vendor ?? s.document_type}
                  {s.usage.find((u) => u.is_primary) ? ` · ${Number(s.usage.find((u) => u.is_primary)!.value).toLocaleString()} ${s.usage.find((u) => u.is_primary)!.unit ?? ''}` : ''}
                  {s.period_start && s.period_end ? ` · ${shortDate(s.period_start)} – ${shortDate(s.period_end)}` : ''}
                </div>
              </div>
              <a className="btn btn-ghost small" href={`/api/attachments/${s.attachment_id}/content`} target="_blank" rel="noreferrer">Open</a>
            </div>
          ))}
          <input ref={fileInput} type="file" hidden accept=".pdf,.png,.jpg,.jpeg,.webp,.heic,.gif,.tif,.tiff"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) attach.mutate(f); e.target.value = '' }} />
          <Button className="btn-block" style={{ margin: 0 }} disabled={attach.isPending} onClick={() => fileInput.current?.click()}>
            {attach.isPending ? 'Uploading…' : 'Attach statement (PDF, image)'}
          </Button>
        </div>
      )}

      {!isNew && (
        <div className="section-rule">
          <div className="card-kicker">Sources</div>
          {(sources.data ?? []).length === 0 ? (
            <div className="small muted-2">
              {txn.source_type === 'manual' ? 'Entered by hand; no imported records match it yet.' : 'No import records for this transaction.'}
              {txn.import_batch_id && <> <Link to={`/import/${txn.import_batch_id}`}>Import #{txn.import_batch_id}</Link></>}
            </div>
          ) : (sources.data ?? []).map((s) => <SourceRow key={s.id} s={s} txn={txn} />)}
        </div>
      )}

      {!isNew && (
        <div className="section-rule">
          <div className="card-kicker">Duplicate check</div>
          <div className="small muted-2">
            {(dups.data ?? []).length === 0 ? 'No duplicate candidates recorded.' : (dups.data ?? []).map((p) => {
              const other = p.a.id === txn.id ? p.b : p.a
              const label = { pending: 'Possible duplicate of', confirmed_separate: 'Confirmed separate from',
                confirmed_duplicate: 'Merged with' }[p.status] ?? p.status
              return <div key={p.id}>{label} <Link to={txnPath(other.id)}>{other.description}</Link> {shortDate(other.txn_date)} {money(other.amount)}
                {other.account_name ? ` (${other.account_name})` : ''}</div>
            })}
          </div>
        </div>
      )}

      <ErrorNote error={save.error || remove.error || accept.error} />
      <div className="row">
        <Button variant="primary" onClick={() => save.mutate()} disabled={!valid || save.isPending}>
          {isNew ? 'Add transaction' : 'Save'}
        </Button>
        {!isNew && (
          <Button variant="ghost" onClick={() => confirm('Delete this transaction?') && remove.mutate()} disabled={remove.isPending}>
            Delete
          </Button>
        )}
      </div>
      </div>
    </Card>
  )
}
