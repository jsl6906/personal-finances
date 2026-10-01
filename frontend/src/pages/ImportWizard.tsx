import { Fragment, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  IMPORT_FIELDS, del, get, post, type BatchDetail, type ImportDefaults, type ImportField, type ImportOptions,
  type ImportPair, type ImportRow, type StatementAccount,
} from '../api'
import { CategorySelect } from '../components/CategorySelect'
import { PairReview } from '../components/PairReview'
import { Button, Card, ErrorNote, Field, ProgressBar } from '../components/ui'
import { fullDate, money, shortDate } from '../format'
import { useAccounts, useJob, useMembers, useTags } from '../hooks'
import { STATUS_TAG, briefFacts, pairConfidence } from '../review'
import { Steps } from './Import'

const DATE_FORMATS = [
  { value: 'auto', label: 'Detect automatically' },
  { value: '%m/%d/%Y', label: 'MM/DD/YYYY' },
  { value: '%m/%d/%y', label: 'MM/DD/YY' },
  { value: '%d/%m/%Y', label: 'DD/MM/YYYY' },
  { value: '%Y-%m-%d', label: 'YYYY-MM-DD' },
]
const MAPPING_SOURCE: Record<string, string> = {
  template: 'Mapping from saved preset', ai: 'Mapping suggested by Gemini', heuristic: 'Mapping guessed from column headers',
  document: 'Columns extracted by Gemini',
}

function num(s: string | undefined): number | null {
  if (!s) return null
  let t = s.replace(/[$,\s]/g, '').replace('−', '-')
  let neg = false
  if (t.startsWith('(') && t.endsWith(')')) { neg = true; t = t.slice(1, -1) }
  const n = Number(t)
  return Number.isNaN(n) ? null : neg ? -n : n
}

function landing(raw: Record<string, string>, mapping: Record<string, ImportField>, invert: boolean) {
  const pick = (f: ImportField) => Object.entries(mapping).filter(([, v]) => v === f).map(([k]) => raw[k]).filter(Boolean).join(' ')
  let amount = num(pick('amount'))
  if (amount === null && (pick('debit') || pick('credit'))) amount = Math.abs(num(pick('credit')) ?? 0) - Math.abs(num(pick('debit')) ?? 0)
  if (amount !== null && invert) amount = -amount
  return { date: pick('txn_date') || pick('posted_date'), description: pick('description') || pick('original_description'), amount }
}

export function ImportWizard() {
  const { id } = useParams()
  const batchId = Number(id)
  const batch = useQuery({
    queryKey: ['import', batchId],
    queryFn: () => get<BatchDetail>(`/imports/${batchId}`),
    refetchInterval: (q) => (q.state.data && ['extracting', 'preparing'].includes(q.state.data.status) ? 1500 : false),
  })
  if (batch.error) return <ErrorNote error={batch.error} />
  if (!batch.data) return null
  const b = batch.data
  // Re-seed local mapping/defaults state once extraction finishes.
  return <Wizard key={`${b.id}-${b.status === 'extracting' || b.status === 'failed' ? 'x' : 'r'}`} b={b} />
}

function Wizard({ b }: { b: BatchDetail }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const members = useMembers()
  const tags = useTags()
  const job = useJob(['extracting', 'preparing'].includes(b.status) ? b.job_id : null)

  const [mapping, setMapping] = useState<Record<string, ImportField>>(b.mapping)
  const [options, setOptions] = useState<ImportOptions>({
    date_format: b.options.date_format ?? 'auto', dayfirst: !!b.options.dayfirst, invert_sign: !!b.options.invert_sign,
  })
  const [defaults, setDefaults] = useState<ImportDefaults>({
    account_id: b.defaults.account_id ?? null, account_map: b.defaults.account_map ?? {},
    category_id: b.defaults.category_id ?? null,
    member_id: b.defaults.member_id ?? null, notes: b.defaults.notes ?? `Imported from ${b.filename}`,
    tag_ids: b.defaults.tag_ids ?? [],
  })
  const [savePreset, setSavePreset] = useState(b.source_type === 'spreadsheet' && !b.template_name)
  const [presetName, setPresetName] = useState(b.template_name ?? (b.filename ?? '').replace(/\.[^.]+$/, ''))

  const stmtAccounts = b.doc_meta?.accounts ?? []
  const noAccount = b.status === 'review' ? b.stats.no_account ?? 0 : 0
  const pending = b.decisions.pending ?? 0
  const defaultStep = b.status === 'review' ? (noAccount ? 3 : pending ? 4 : 5) : ['committed', 'rolled_back'].includes(b.status) ? 5
    : b.status === 'preparing' ? 3 : 2
  const [step, setStep] = useState(defaultStep)
  const reachable = b.status === 'review' ? 5 : ['committed', 'rolled_back'].includes(b.status) ? 5 : 3
  const locked = ['committed', 'rolled_back'].includes(b.status)

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['import', b.id] })
    qc.invalidateQueries({ queryKey: ['imports'] })
  }
  const prepare = useMutation({
    mutationFn: () => post<BatchDetail>(`/imports/${b.id}/prepare`, {
      mapping, options, defaults, save_template: savePreset, template_name: presetName || null,
    }),
    onSuccess: (nb) => {
      qc.setQueryData(['import', b.id], nb)
      setStep(4)
    },
  })
  const commit = useMutation({
    mutationFn: () => post<BatchDetail>(`/imports/${b.id}/commit`, { pending_as: 'skip' }),
    onSuccess: (nb) => {
      qc.setQueryData(['import', b.id], nb)
      qc.invalidateQueries({ queryKey: ['transactions'] })
      qc.invalidateQueries({ queryKey: ['summary'] })
      qc.invalidateQueries({ queryKey: ['imports'] })
    },
  })
  const rollback = useMutation({
    mutationFn: () => post<BatchDetail>(`/imports/${b.id}/rollback`),
    onSuccess: (nb) => {
      qc.setQueryData(['import', b.id], nb)
      qc.invalidateQueries({ queryKey: ['transactions'] })
      qc.invalidateQueries({ queryKey: ['imports'] })
    },
  })
  const discard = useMutation({
    mutationFn: () => del(`/imports/${b.id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['imports'] })
      navigate('/import')
    },
  })
  const reread = useMutation({
    mutationFn: () => post<BatchDetail>(`/imports/${b.id}/extract`),
    onSuccess: (nb) => qc.setQueryData(['import', b.id], nb),
  })
  const chooseSheet = useMutation({
    mutationFn: (sheet: string) => post<BatchDetail>(`/imports/${b.id}/sheet`, { sheet }),
    onSuccess: (nb) => {
      qc.setQueryData(['import', b.id], nb)
      setMapping(nb.mapping)
    },
  })

  const isDoc = b.source_type === 'document'
  const fields = new Set(Object.values(mapping))
  const mappingOk = fields.has('txn_date') && (fields.has('description') || fields.has('original_description'))
    && (fields.has('amount') || fields.has('debit') || fields.has('credit'))
  const toInsert = (b.decisions.insert ?? 0) + (b.decisions.keep ?? 0)
  const toLink = (b.decisions.skip_duplicate ?? 0) + pending

  const busy = b.status === 'extracting' || b.status === 'preparing'
  const progressCard = busy && (
    <Card style={{ maxWidth: 640 }}>
      <div className="card-kicker">{b.status === 'extracting' ? 'Reading document' : 'Preparing rows'}</div>
      <div className="card-title">
        {b.status === 'extracting' ? `Gemini is extracting transactions from ${b.filename}…`
          : `Parsing ${b.row_count} rows and checking them against your ledger…`}
      </div>
      <ProgressBar fraction={Number(job.data?.progress ?? 0.05)} />
      <div className="card-meta">{job.data?.message ?? 'Queued'}</div>
    </Card>
  )

  return (
    <section className="page" style={{ gap: 'var(--space-6)' }}>
      <header className="page-header">
        <div>
          <h2 style={{ margin: 0 }}>Import transactions</h2>
          <div className="text-muted subtitle">
            {b.filename} · {b.source_type} · <span className={`tag ${STATUS_TAG[b.status]}`}>{b.status.replace('_', ' ')}</span>
          </div>
        </div>
        <Link to="/import" className="btn btn-ghost">All imports</Link>
      </header>
      <Steps current={step} reachable={busy ? 0 : reachable} onGo={locked ? undefined : (n) => (n === 1 ? navigate('/import') : setStep(n))} />

      {b.status === 'failed' && (
        <div className="callout error">
          <strong>Import failed.</strong> {b.error}
          <div className="row" style={{ marginTop: 8 }}>
            <Button onClick={() => discard.mutate()}>Discard</Button>
          </div>
        </div>
      )}
      {progressCard}

      {!busy && b.status !== 'failed' && step === 2 && (
        <div className="grid-main-side wide-side">
          {isDoc ? <DocRows b={b} /> : (
            <Card style={{ gap: 'var(--space-3)' }}>
              <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
                <div><div className="card-kicker">Spreadsheet</div><div className="card-title">{b.filename}</div></div>
                <span className="tag tag-accent">{b.row_count} rows detected</span>
              </div>
              {b.sheets.length > 1 && (
                <Field label="Sheet">
                  <select className="input" value={b.sheet_name ?? ''} disabled={chooseSheet.isPending}
                    onChange={(e) => chooseSheet.mutate(e.target.value)}>
                    {b.sheets.map((s) => <option key={s}>{s}</option>)}
                  </select>
                </Field>
              )}
              <table className="table">
                <thead><tr><th>Source column</th><th>Sample</th><th>Maps to</th></tr></thead>
                <tbody>
                  {b.columns.map((c) => (
                    <tr key={c.name}>
                      <td style={{ fontWeight: 500 }}>{c.name}</td>
                      <td className="text-muted" style={{ fontVariantNumeric: 'tabular-nums' }}>{c.samples.slice(0, 2).join(' · ')}</td>
                      <td>
                        <select className="input" value={mapping[c.name] ?? 'ignore'}
                          onChange={(e) => setMapping({ ...mapping, [c.name]: e.target.value as ImportField })}>
                          {IMPORT_FIELDS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
                        </select>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="grid-form">
                <Field label="Date format">
                  <select className="input" value={options.date_format} onChange={(e) => setOptions({ ...options, date_format: e.target.value })}>
                    {DATE_FORMATS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
                  </select>
                </Field>
                <Field label="Amount sign">
                  <label className="radio" style={{ minHeight: 36 }}>
                    <input type="checkbox" checked={options.invert_sign}
                      onChange={(e) => setOptions({ ...options, invert_sign: e.target.checked })} />
                    Charges are positive — flip signs
                  </label>
                </Field>
              </div>
              <div className="row">
                <label className="radio">
                  <input type="checkbox" checked={savePreset} onChange={(e) => setSavePreset(e.target.checked)} />
                  Save as preset
                </label>
                <input className="input compact" style={{ flex: 1 }} value={presetName} disabled={!savePreset}
                  onChange={(e) => setPresetName(e.target.value)} placeholder="Preset name" />
              </div>
              <div className="card-meta">
                {MAPPING_SOURCE[b.mapping_source ?? ''] ?? ''}{b.template_name ? ` “${b.template_name}”` : ''} · presets are reused
                automatically for files with the same columns
              </div>
              {!mappingOk && <div className="callout error">Map a date, a description, and an amount (or debit/credit) column.</div>}
            </Card>
          )}
          <div className="stack-3">
            {!isDoc && (
              <Card style={{ gap: 'var(--space-3)' }}>
                <div className="card-kicker">Preview</div><div className="card-title">First rows as they'll land</div>
                <table className="table" style={{ fontSize: 13 }}>
                  <thead><tr><th>Date</th><th>Description</th><th style={{ textAlign: 'right' }}>Amount</th></tr></thead>
                  <tbody>
                    {b.preview.slice(0, 5).map((raw, i) => {
                      const l = landing(raw, mapping, options.invert_sign)
                      return (
                        <tr key={i}>
                          <td className="nowrap">{l.date || '—'}</td><td>{l.description || '—'}</td>
                          <td className="num">{l.amount === null ? '—' : money(l.amount)}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </Card>
            )}
            {isDoc && b.doc_meta && <DocMetaCard b={b} />}
          </div>
        </div>
      )}

      {!busy && b.status !== 'failed' && step === 3 && (
        <div className="grid-2" style={{ alignItems: 'start' }}>
          <Card style={{ gap: 'var(--space-3)' }}>
            <div className="card-kicker">Defaults</div>
            <div className="card-title">Applied to fields the source doesn't carry</div>
            {noAccount > 0 && (
              <div className="callout">
                {noAccount} row{noAccount === 1 ? ' has' : 's have'} no account yet. Choose the account
                {stmtAccounts.length > 1 ? ' for each account on the statement' : ''} below, then Re-check rows.
                {isDoc && !b.doc_meta?.accounts && ' This document was read before multi-account statements were'
                  + ' supported; Re-read document to split its rows by account.'}
              </div>
            )}
            {stmtAccounts.length > 0 && (
              <Field label={stmtAccounts.length > 1 ? `Accounts on this statement (${stmtAccounts.length})` : 'Account'}>
                <div className="stack-3">
                  {stmtAccounts.map((sa) => (
                    <div key={sa.ref} className="stack" style={{ gap: 4 }}>
                      {stmtAccounts.length > 1 && (
                        <div className="small">
                          {stmtLabel(sa)} <span className="text-muted">· {sa.rows} row{sa.rows === 1 ? '' : 's'}</span>
                        </div>
                      )}
                      <AccountSelect value={defaults.account_map[sa.ref] ?? null} empty="— choose an account —"
                        onChange={(v) => setDefaults({
                          ...defaults, account_map: { ...defaults.account_map, [sa.ref]: v },
                          ...(stmtAccounts.length === 1 ? { account_id: v } : {}),
                        })} />
                    </div>
                  ))}
                </div>
              </Field>
            )}
            {(stmtAccounts.length === 0 || (b.doc_meta?.unassigned_rows ?? 0) > 0) && (
              <Field label={stmtAccounts.length ? `Rows not under any account above (${b.doc_meta?.unassigned_rows})` : 'Account / institution'}>
                <AccountSelect value={defaults.account_id} empty="— none (use the file's account column) —"
                  onChange={(v) => setDefaults({ ...defaults, account_id: v })} />
              </Field>
            )}
            <Field label="Category when unmatched">
              <CategorySelect value={defaults.category_id} emptyLabel="Uncategorized (AI suggests later)"
                onChange={(v) => setDefaults({ ...defaults, category_id: v })} />
            </Field>
            <Field label="Household member">
              <select className="input" value={defaults.member_id ?? ''}
                onChange={(e) => setDefaults({ ...defaults, member_id: e.target.value ? Number(e.target.value) : null })}>
                <option value="">Shared</option>
                {(members.data ?? []).map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
              </select>
            </Field>
            <Field label="Notes">
              <input className="input" value={defaults.notes ?? ''} onChange={(e) => setDefaults({ ...defaults, notes: e.target.value || null })} />
            </Field>
            {(tags.data?.length ?? 0) > 0 && (
              <Field label="Tags">
                <div className="row" style={{ gap: 6 }}>
                  {tags.data!.map((t) => {
                    const on = defaults.tag_ids.includes(t.id)
                    return (
                      <button key={t.id} type="button" className={`tag chip${on ? ' on' : ''}`}
                        onClick={() => setDefaults({ ...defaults, tag_ids: on ? defaults.tag_ids.filter((x) => x !== t.id) : [...defaults.tag_ids, t.id] })}>
                        {t.name}
                      </button>
                    )
                  })}
                </div>
              </Field>
            )}
          </Card>
          <div className="stack-3">
            {isDoc && b.doc_meta && <DocMetaCard b={b} />}
            <Card style={{ gap: 'var(--space-2)' }}>
              <div className="card-kicker">Category suggestions</div>
              <div className="card-body">
                Categories in the file are matched to your hierarchy by name. Rows that stay uncategorized first get your
                learned merchant rules, then Gemini proposes a category; suggestions can be bulk-accepted on the
                Transactions screen.
              </div>
            </Card>
          </div>
        </div>
      )}

      {!busy && b.status === 'review' && step === 4 && <DuplicateStep b={b} onChanged={refresh} />}
      {!busy && ['review', 'committed', 'rolled_back'].includes(b.status) && step === 5 && (
        <ReviewStep b={b} noAccount={noAccount} onChooseAccounts={() => setStep(3)} />
      )}

      <ErrorNote error={prepare.error || commit.error || rollback.error || discard.error || chooseSheet.error || reread.error} />
      {!busy && b.status !== 'failed' && (
        <footer className="wizard-foot">
          {!locked && (
            <Button onClick={() => (step <= 2 ? navigate('/import') : setStep(step - 1))}>Back</Button>
          )}
          {step === 2 && !locked && (
            <Button variant="primary" disabled={!mappingOk} onClick={() => setStep(3)}>Continue</Button>
          )}
          {step === 3 && !locked && (
            <Button variant="primary" disabled={!mappingOk || prepare.isPending} onClick={() => prepare.mutate()}>
              {b.status === 'review' ? 'Re-check rows' : 'Continue'}
            </Button>
          )}
          {step === 4 && b.status === 'review' && (
            <Button variant="primary" onClick={() => setStep(5)}>{pending ? 'Skip remaining, continue' : 'Continue'}</Button>
          )}
          {step === 5 && b.status === 'review' && (
            <Button variant="primary" disabled={commit.isPending || toInsert + toLink === 0} onClick={() => commit.mutate()}>
              {toInsert || !toLink ? `Commit ${toInsert} transactions` : `Link ${toLink} duplicates`}
            </Button>
          )}
          {b.status === 'committed' && (
            <>
              <Button variant="primary" onClick={() => navigate(`/transactions?batch=${b.id}`)}>View transactions</Button>
              <Button onClick={() => navigate('/import')}>Import another</Button>
              <Button variant="ghost" disabled={rollback.isPending}
                onClick={() => confirm('Remove every transaction added by this import?') && rollback.mutate()}>Roll back</Button>
            </>
          )}
          {!locked && isDoc && (
            <Button variant="ghost" style={{ marginLeft: 'auto' }} disabled={reread.isPending}
              onClick={() => confirm('Read the document again with Gemini? Mapping and duplicate decisions are reset.') && reread.mutate()}>
              Re-read document
            </Button>
          )}
          {!locked && (
            <Button variant="ghost" style={isDoc ? undefined : { marginLeft: 'auto' }} disabled={discard.isPending}
              onClick={() => confirm('Discard this import? Nothing has been added to the ledger yet.') && discard.mutate()}>
              Discard import
            </Button>
          )}
        </footer>
      )}
    </section>
  )
}

function stmtLabel(sa: StatementAccount) {
  return [sa.name, sa.last4 ? `···${sa.last4}` : null].filter(Boolean).join(' ') || sa.ref
}

function AccountSelect({ value, empty, onChange }: { value: number | null; empty: string; onChange: (v: number | null) => void }) {
  const accounts = useAccounts()
  return (
    <select className="input" value={value ?? ''} onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}>
      <option value="">{empty}</option>
      {(accounts.data ?? []).map((a) => (
        <option key={a.id} value={a.id}>{a.name}{a.institution_name ? ` · ${a.institution_name}` : ''}{a.mask ? ` ···${a.mask}` : ''}</option>
      ))}
    </select>
  )
}

function DocRows({ b }: { b: BatchDetail }) {
  const rows = useQuery({ queryKey: ['import', b.id, 'rows', 'all'], queryFn: () => get<ImportRow[]>(`/imports/${b.id}/rows`, { limit: 1000 }) })
  const stmtAccounts = b.doc_meta?.accounts ?? []
  const multi = stmtAccounts.length > 1
  const label = (ref: string | undefined) => {
    const sa = stmtAccounts.find((a) => a.ref === ref)
    return sa ? stmtLabel(sa) : ref || '—'
  }
  return (
    <Card style={{ gap: 'var(--space-3)' }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div><div className="card-kicker">Document · extracted by Gemini</div><div className="card-title">{b.filename}</div></div>
        <span className="tag tag-accent">{b.row_count} rows extracted</span>
      </div>
      <table className="table" style={{ fontSize: 13 }}>
        <thead><tr><th>Date</th><th>Posted</th><th>Description</th>{multi && <th>Account</th>}<th style={{ textAlign: 'right' }}>Amount</th><th>Conf.</th></tr></thead>
        <tbody>
          {(rows.data ?? []).map((r) => {
            const conf = Number(r.raw.Confidence ?? 1)
            return (
              <tr key={r.id} className={conf < 0.8 ? 'low-conf' : ''}>
                <td className="nowrap">{r.raw.Date}</td><td className="nowrap text-muted">{r.raw.Posted}</td>
                <td>{r.raw.Description}</td>{multi && <td className="nowrap text-muted">{label(r.raw.Account)}</td>}
                <td className="num">{money(r.raw.Amount)}</td>
                <td className="small">{Math.round(conf * 100)}%</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {b.attachment_id && (
        <a className="small" href={`/api/attachments/${b.attachment_id}/content`} target="_blank" rel="noreferrer">Open source document</a>
      )}
    </Card>
  )
}

function DocMetaCard({ b }: { b: BatchDetail }) {
  const m = b.doc_meta!
  const r = m.reconciliation
  const multi = (m.accounts?.length ?? 0) > 1
  return (
    <Card style={{ gap: 'var(--space-2)', borderColor: 'var(--color-accent-400)' }}>
      <div className="card-kicker">Read from the document</div>
      <div className="facts">
        <span className="text-muted">Type</span><span>{m.document_type.replaceAll('_', ' ')}</span>
        <span className="text-muted">Institution</span><span>{m.institution ?? '—'}</span>
        {multi ? m.accounts!.map((sa, i) => (
          <Fragment key={sa.ref}>
            <span className="text-muted">{i === 0 ? 'Accounts' : ''}</span>
            <span>
              {stmtLabel(sa)} · {sa.rows} rows
              {sa.reconciliation && (sa.reconciliation.reconciles ? ' · reconciles'
                : ` · rows sum ${money(sa.reconciliation.sum_of_rows)} vs balance change ${money(sa.reconciliation.balance_change)}`)}
            </span>
          </Fragment>
        )) : (
          <><span className="text-muted">Account</span><span>{m.account_name ?? '—'}{m.account_last4 ? ` ···${m.account_last4}` : ''}</span></>
        )}
        <span className="text-muted">Period</span><span>{m.period_start ? `${fullDate(m.period_start)} – ${fullDate(m.period_end)}` : '—'}</span>
        <span className="text-muted">Sign convention</span><span>{m.sign_note}</span>
        {r && (<><span className="text-muted">Reconciliation</span>
          <span>{r.reconciles ? 'Rows match the balance change' : `Rows sum ${money(r.sum_of_rows)} vs balance change ${money(r.balance_change)}`}</span></>)}
      </div>
      {m.low_confidence_rows > 0 && <div className="small">{m.low_confidence_rows} rows need a look (highlighted).</div>}
      <div className="card-meta">{m.summary} Edit defaults on the next step; the document stays attached to the batch.</div>
    </Card>
  )
}

function DuplicateStep({ b, onChanged }: { b: BatchDetail; onChanged: () => void }) {
  const qc = useQueryClient()
  const pairs = useQuery({ queryKey: ['import', b.id, 'pairs'], queryFn: () => get<ImportPair[]>(`/imports/${b.id}/duplicates`) })
  const decide = useMutation({
    mutationFn: ({ rowId, decision }: { rowId: number; decision: string }) =>
      post(`/imports/${b.id}/rows/${rowId}/decision`, { decision }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['import', b.id, 'pairs'] })
      onChanged()
    },
  })
  const list = pairs.data ?? []
  const open = list.filter((p) => p.row.decision === 'pending')
  const cur = open[0]
  const exact = b.stats.exact_duplicates ?? 0
  const skipped = list.filter((p) => p.row.decision === 'skip_duplicate').length
  const kept = list.filter((p) => p.row.decision === 'keep').length

  if (pairs.isLoading) return null
  return (
    <div className="stack-3">
      {exact > 0 && <div className="small text-muted">{exact} exact duplicates of existing transactions were skipped automatically.</div>}
      {cur ? (
        <PairReview
          kicker={`Potential duplicate ${list.length - open.length + 1} of ${list.length}`}
          title={`${cur.existing.description} · ${shortDate(cur.row.txn_date)}`}
          confidence={pairConfidence(cur.score, cur.ai_probability)}
          left={{ kicker: `Incoming · ${b.filename}`, description: cur.row.description, amount: cur.row.amount,
            facts: [['Date', fullDate(cur.row.txn_date)], ['Account', cur.row.account_name ?? cur.row.account_hint ?? '—'],
              ['Row', String(cur.row.row_index + 1)]] }}
          right={{ kicker: 'Already in ledger', description: cur.existing.description, amount: cur.existing.amount,
            facts: briefFacts(cur.existing) }}
          reason={cur.ai_reason ? `${cur.ai_reason} (${cur.reasons.join(' · ')})` : cur.reasons.join(' · ')}
          note="“Keep both” is remembered; this pair won't be flagged again."
          actions={<>
            <Button variant="primary" disabled={decide.isPending}
              onClick={() => decide.mutate({ rowId: cur.row.id, decision: 'skip_duplicate' })}>Same transaction — skip incoming</Button>
            <Button disabled={decide.isPending} onClick={() => decide.mutate({ rowId: cur.row.id, decision: 'keep' })}>
              Different — keep both
            </Button>
          </>}
        />
      ) : (
        <Card style={{ maxWidth: 560, gap: 'var(--space-2)' }}>
          <div className="card-kicker">Duplicate review</div>
          <div className="card-title">{list.length ? `All ${list.length} candidates decided` : 'No possible duplicates to review'}</div>
          {list.length > 0 && <div>{skipped} skipped as duplicates, {kept} kept as separate and remembered.</div>}
          {list.length > 0 && (
            <Button variant="ghost" style={{ alignSelf: 'flex-start' }} disabled={decide.isPending}
              onClick={async () => {
                for (const p of list) await post(`/imports/${b.id}/rows/${p.row.id}/decision`, { decision: 'pending' })
                qc.invalidateQueries({ queryKey: ['import', b.id, 'pairs'] })
                onChanged()
              }}>Start over</Button>
          )}
        </Card>
      )}
      <ErrorNote error={decide.error || pairs.error} />
    </div>
  )
}

function ReviewStep({ b, noAccount, onChooseAccounts }: { b: BatchDetail; noAccount: number; onChooseAccounts: () => void }) {
  const preview = useQuery({
    queryKey: ['import', b.id, 'rows', 'insert'],
    queryFn: () => get<ImportRow[]>(`/imports/${b.id}/rows`, { decision: 'insert', limit: 6 }),
  })
  const invalid = useQuery({
    queryKey: ['import', b.id, 'rows', 'invalid'],
    queryFn: () => get<ImportRow[]>(`/imports/${b.id}/rows`, { decision: 'invalid', limit: 50 }),
  })
  const d = b.decisions
  const committed = b.status === 'committed'
  const inserted = committed ? b.stats.inserted : (d.insert ?? 0) + (d.keep ?? 0)
  return (
    <div className="grid-main-side">
      <Card style={{ gap: 'var(--space-3)' }}>
        <div className="card-kicker">{committed ? 'Committed' : b.status === 'rolled_back' ? 'Rolled back' : 'Ready to commit'}</div>
        <div className="card-title">{b.filename}</div>
        <div className="grid-3">
          <div><div className="stat-n">{inserted}</div><div className="text-muted small">{committed ? 'rows inserted' : 'rows to insert'}</div></div>
          <div><div className="stat-n">{d.skip_duplicate ?? 0}</div><div className="text-muted small">{committed ? 'duplicates linked to existing' : 'duplicates to link'}</div></div>
          <div><div className="stat-n">{d.keep ?? 0}</div><div className="text-muted small">kept as separate</div></div>
        </div>
        {(d.pending ?? 0) > 0 && <div className="callout">{d.pending} undecided possible duplicates will be skipped.</div>}
        {noAccount > 0 && (
          <div className="callout row" style={{ justifyContent: 'space-between' }}>
            <span>{noAccount} row{noAccount === 1 ? ' has' : 's have'} no account, which also weakens duplicate matching.</span>
            <Button onClick={onChooseAccounts}>Choose accounts</Button>
          </div>
        )}
        {!committed && (
          <table className="table" style={{ fontSize: 13 }}>
            <thead><tr><th>Date</th><th>Description</th><th>Account</th><th>Category</th><th style={{ textAlign: 'right' }}>Amount</th></tr></thead>
            <tbody>
              {(preview.data ?? []).map((r) => (
                <tr key={r.id}>
                  <td className="nowrap">{shortDate(r.txn_date)}</td><td>{r.description}</td>
                  <td className="text-muted">{r.account_name ?? r.account_hint ?? '—'}</td>
                  <td className="text-muted">{r.category_name ?? r.category_hint ?? '—'}</td>
                  <td className="num">{money(r.amount, true)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="card-body">
          The source file is stored in Postgres with the batch (sha-256 deduplicated) so every transaction links back to its
          origin. Duplicate rows aren't inserted again; the file is added as another source on the matching transaction,
          along with any notes or statement details on the row. The import can be rolled back as a unit.
        </div>
        {committed && <span className="tag tag-accent" style={{ alignSelf: 'flex-start' }}>Committed · batch #{b.id}
          {b.stats.categorized_by_rule ? ` · ${b.stats.categorized_by_rule} categorized by rule` : ''}</span>}
        {b.status === 'rolled_back' && <span className="tag tag-neutral" style={{ alignSelf: 'flex-start' }}>
          Rolled back · {b.stats.rolled_back} transactions removed</span>}
      </Card>
      <Card style={{ gap: 'var(--space-2)' }}>
        <div className="card-kicker">Rows that can't be imported</div>
        <div className="card-title">{d.invalid ?? 0} invalid</div>
        {(invalid.data ?? []).map((r) => (
          <div key={r.id} className="small" style={{ borderBottom: '1px solid var(--color-divider)', paddingBottom: 4 }}>
            Row {r.row_index + 1}: {r.errors.join('; ')}
            <div className="text-muted">{Object.values(r.raw).filter(Boolean).slice(0, 4).join(' · ')}</div>
          </div>
        ))}
        {(d.invalid ?? 0) > 0 && <div className="card-meta">Fix the mapping or date format (Back) and re-check, or skip them.</div>}
      </Card>
    </div>
  )
}
