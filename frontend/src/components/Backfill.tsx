import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, post, put, type BackfillFileRow, type BackfillOverview } from '../api'
import { Button, Card, ErrorNote, ProgressBar, Seg } from './ui'

const KIND_LABEL: Record<string, string> = {
  bank_statement: 'Bank statement',
  credit_card_statement: 'Card statement',
  loan_statement: 'Loan statement',
  investment_statement: 'Investment statement',
  utility_bill: 'Utility bill',
  other_bill: 'Bill',
  spreadsheet: 'Spreadsheet',
  receipt: 'Receipt',
  tax_document: 'Tax document',
  other: 'Other',
}
const GROUPS: [string, string[]][] = [
  ['Bank statements', ['bank_statement', 'loan_statement', 'investment_statement']],
  ['Card statements', ['credit_card_statement']],
  ['Bills', ['utility_bill', 'other_bill']],
  ['Spreadsheets', ['spreadsheet']],
]
const STATUS: Record<BackfillFileRow['status'], [string, string]> = {
  pending: ['Queued', 'tag-neutral'],
  classified: ['Queued', 'tag-neutral'],
  done: ['Parsed', 'tag-accent'],
  review: ['Needs review', 'tag-outline'],
  skipped: ['Skipped', 'tag-neutral'],
  failed: ['Failed', 'tag-outline'],
}
const KINDS = ['bank_statement', 'credit_card_statement', 'loan_statement', 'utility_bill', 'other_bill', 'spreadsheet']

export function BackfillCard() {
  const qc = useQueryClient()
  const [flaggedOnly, setFlaggedOnly] = useState(false)
  const ov = useQuery({
    queryKey: ['backfill'],
    queryFn: () => get<BackfillOverview>('/backfill'),
    refetchInterval: (q) => (q.state.data && (q.state.data.active_job || !q.state.data.settings.paused) ? 3000 : false),
  })
  const files = useQuery({
    queryKey: ['backfill', 'files', flaggedOnly],
    queryFn: () => get<BackfillFileRow[]>('/backfill/files', { limit: 40, status: flaggedOnly ? ['review', 'failed'] : undefined }),
    refetchInterval: ov.data?.active_job ? 3000 : false,
  })
  const refresh = () => qc.invalidateQueries({ queryKey: ['backfill'] })
  const act = useMutation({ mutationFn: (path: string) => post(path), onSuccess: refresh })
  const fileAction = useMutation({
    mutationFn: ({ id, action, kind }: { id: number; action: string; kind?: string }) => post(`/backfill/files/${id}`, { action, kind }),
    onSuccess: refresh,
  })

  const d = ov.data
  const s = d?.summary
  const configured = !!d && (d.settings.provider === 'local' ? d.settings.folder_name !== null : !!d.settings.folder_id)
  const flagged = (s?.by_status.review ?? 0) + (s?.by_status.failed ?? 0)
  const years = s?.earliest ? `${s.earliest.slice(0, 4)} – ${(s.latest ?? s.earliest).slice(0, 4)}` : ''
  const running = !!d && !d.settings.paused

  return (
    <Card style={{ gap: 'var(--space-3)' }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div>
          <div className="card-kicker">Backfill queue{d?.settings.folder_name ? ` · ${d.settings.folder_name}` : ''}</div>
          <div className="card-title">
            {s && s.total ? `${s.processed.toLocaleString()} of ${s.total.toLocaleString()} files processed${years ? ` · ${years}` : ''}` : 'Historical archive'}
          </div>
        </div>
        <div className="row">
          <Button onClick={() => act.mutate('/backfill/scan')} disabled={!configured || act.isPending || d?.active_job?.type === 'backfill_scan'}>
            {d?.active_job?.type === 'backfill_scan' ? 'Scanning…' : 'Scan folder'}
          </Button>
          {running
            ? <Button onClick={() => act.mutate('/backfill/pause')}>Pause</Button>
            : <Button variant="primary" onClick={() => act.mutate('/backfill/start')} disabled={!s?.total}>Start</Button>}
          <Button variant="ghost" onClick={() => setFlaggedOnly(!flaggedOnly)} disabled={!flagged && !flaggedOnly}>
            {flaggedOnly ? 'Show all' : `Review ${flagged} flagged`}
          </Button>
        </div>
      </div>
      <ErrorNote error={ov.error || act.error || fileAction.error} />
      {d?.settings.last_error && <div className="callout error">{d.settings.last_error}</div>}
      {d && <BackfillSettingsForm overview={d} open={!configured} />}
      {s && s.total > 0 && (
        <>
          <ProgressBar fraction={s.processed / s.total} />
          {running && d?.active_job?.message && <div className="small text-muted">Working: {d.active_job.message}</div>}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: 'var(--space-3)', fontSize: 13 }}>
            {GROUPS.map(([label, kinds]) => {
              const total = kinds.reduce((n, k) => n + (s.by_kind[k]?.total ?? 0), 0)
              const done = kinds.reduce((n, k) => n + (s.by_kind[k]?.done ?? 0), 0)
              return <Stat key={label} label={label} value={`${done.toLocaleString()} / ${total.toLocaleString()}`} />
            })}
            <Stat label="Waiting to classify" value={(s.by_status.pending ?? 0).toLocaleString()} />
            <Stat label="Transactions added" value={s.transactions_added.toLocaleString()} />
            <Stat label="Duplicates auto-resolved" value={s.duplicates_skipped.toLocaleString()} />
          </div>
        </>
      )}
      {(files.data?.length ?? 0) > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table className="table">
            <thead><tr><th>File</th><th>Detected</th><th>Result</th><th>Status</th><th /></tr></thead>
            <tbody>
              {files.data!.map((f) => (
                <tr key={f.id}>
                  <td title={f.path}>{f.name}</td>
                  <td className="text-muted small">
                    {f.kind ? KIND_LABEL[f.kind] ?? f.kind : '—'}
                    {f.classification?.institution ? ` · ${f.classification.institution}` : ''}
                    {f.classification?.account_last4 ? ` ···${f.classification.account_last4}` : ''}
                  </td>
                  <td className="small">{f.error ?? f.message ?? '—'}</td>
                  <td><span className={`tag ${STATUS[f.status][1]}`}>{STATUS[f.status][0]}</span></td>
                  <td className="nowrap">
                    <FileActions f={f} onAction={(action, kind) => fileAction.mutate({ id: f.id, action, kind })} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div><div className="text-muted" style={{ fontSize: 11 }}>{label}</div><div>{value}</div></div>
}

function FileActions({ f, onAction }: { f: BackfillFileRow; onAction: (action: string, kind?: string) => void }) {
  const [kind, setKind] = useState('')
  return (
    <div className="row" style={{ gap: 4, flexWrap: 'nowrap' }}>
      {f.import_batch_id && f.status !== 'skipped' && <Link className="btn btn-ghost small" to={`/import/${f.import_batch_id}`}>Open import</Link>}
      {f.statement_id && <Link className="btn btn-ghost small" to="/bills">Open bill</Link>}
      {f.attachment_id && <a className="btn btn-ghost small" href={`/api/attachments/${f.attachment_id}/content`} target="_blank" rel="noreferrer">View</a>}
      {(f.status === 'failed' || f.status === 'review') && (
        <>
          {f.status === 'review' && f.attachment_id && !f.import_batch_id && !f.statement_id && (
            <select className="input compact" value={kind} title="Treat as…"
              onChange={(e) => { setKind(e.target.value); if (e.target.value) onAction('set_kind', e.target.value) }}>
              <option value="">Treat as…</option>
              {KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
            </select>
          )}
          {f.status === 'failed' && <button className="btn btn-ghost small" onClick={() => onAction('retry')}>Retry</button>}
          <button className="btn btn-ghost small" onClick={() => onAction('skip')}>Skip</button>
        </>
      )}
    </div>
  )
}

function BackfillSettingsForm({ overview, open: initiallyOpen }: { overview: BackfillOverview; open: boolean }) {
  const qc = useQueryClient()
  const cfg = overview.settings
  const [open, setOpen] = useState(initiallyOpen)
  const [provider, setProvider] = useState<'drive' | 'local'>(cfg.provider)
  const [folder, setFolder] = useState(cfg.provider === 'local' ? cfg.local_path : cfg.folder_id ?? '')
  const [approve, setApprove] = useState(cfg.auto_approve_bills)
  const save = useMutation({
    mutationFn: () => put('/backfill/settings', { provider, folder, auto_approve_bills: approve }),
    onSuccess: () => { setOpen(false); qc.invalidateQueries({ queryKey: ['backfill'] }) },
  })
  if (!open) return <button className="btn btn-ghost small" style={{ alignSelf: 'start' }} onClick={() => setOpen(true)}>Settings</button>
  return (
    <div className="stack" style={{ borderTop: '1px solid var(--color-divider)', paddingTop: 'var(--space-2)', maxWidth: 640 }}>
      <Seg name="bf-provider" value={provider} onChange={setProvider}
        options={[{ value: 'drive', label: 'Google Drive folder' }, { value: 'local', label: 'Local inbox folder' }]} />
      {provider === 'drive' ? (
        <div className="small">
          {overview.google_service_account
            ? <>Share the archive folder (Viewer) with <code style={{ userSelect: 'all' }}>{overview.google_service_account}</code>, then paste its URL. Subfolders are included.</>
            : <>Configure the Google service account first (see the Tiller card).</>}
        </div>
      ) : (
        <div className="small">
          {overview.inbox_configured
            ? <>A path inside the mounted inbox volume; leave empty for the whole inbox.</>
            : <>Set <code>INBOX_DIR</code> (e.g. <code>/inbox</code>, mounted from the host) to use a local folder.</>}
        </div>
      )}
      <input className="input" value={folder} onChange={(e) => setFolder(e.target.value)}
        placeholder={provider === 'drive' ? 'https://drive.google.com/drive/folders/…' : 'statements/2012'} />
      <label className="row small">
        <input type="checkbox" checked={approve} onChange={(e) => setApprove(e.target.checked)} />
        File bills automatically (otherwise each waits for approval in Bills &amp; statements)
      </label>
      <ErrorNote error={save.error} />
      <div className="row">
        <Button variant="primary" onClick={() => save.mutate()} disabled={save.isPending || (provider === 'drive' && !folder.trim())}>
          {save.isPending ? 'Checking folder…' : 'Save'}
        </Button>
        {!initiallyOpen && <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>}
      </div>
    </div>
  )
}
