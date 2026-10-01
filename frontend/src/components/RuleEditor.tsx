import { useId, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { post, put, type CategoryRule, type RuleMatch, type RulePreview } from '../api'
import { money, shortDate } from '../format'
import { useAccounts, useDebounced, useMerchantSearch } from '../hooks'
import { txnPath } from '../links'
import { MATCH_LABELS, ruleBody, specOf, type RuleDraft } from '../rules'
import { CategorySelect } from './CategorySelect'
import { Button, ErrorNote, Field } from './ui'

function MerchantKeyInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const listId = useId()
  const hits = useMerchantSearch(value)
  return (
    <>
      <input className="input" list={listId} value={value} placeholder="merchant key, e.g. costco" onChange={(e) => onChange(e.target.value)} />
      <datalist id={listId}>
        {(hits.data ?? []).map((m) => <option key={m.key} value={m.key}>{m.name} · {m.count} transactions</option>)}
      </datalist>
    </>
  )
}

type Props = {
  initial: RuleDraft
  /** Existing rule being edited (PUT); otherwise a new rule is created. */
  ruleId?: number
  /** Prompt mode: only match, category and "this account"; identical rules are updated instead of rejected. */
  compact?: boolean
  /** In compact mode: the account the "only this account" checkbox refers to. */
  seedAccount?: { id: number; name: string } | null
  intro?: ReactNode
  saveLabel?: string
  cancelLabel?: string
  onDone: (msg: string) => void
  onCancel: () => void
}

export function RuleEditor({ initial, ruleId, compact = false, seedAccount, intro, saveLabel, cancelLabel = 'Cancel', onDone, onCancel }: Props) {
  const qc = useQueryClient()
  const accounts = useAccounts()
  const [d, setD] = useState<RuleDraft>(initial)
  const [applyNow, setApplyNow] = useState(true)
  const [includeUser, setIncludeUser] = useState(false)
  const set = <K extends keyof RuleDraft>(k: K, v: RuleDraft[K]) => setD((x) => ({ ...x, [k]: v }))

  const spec = useDebounced(specOf(d), 350)
  const preview = useQuery({
    queryKey: ['rules', 'preview', spec],
    queryFn: () => post<RulePreview>('/rules/preview', spec),
    enabled: spec !== null,
    retry: false,
  })
  const p = preview.data
  const fixable = p ? p.uncategorized + p.auto : 0

  const save = useMutation({
    mutationFn: async () => {
      const body = ruleBody(d)
      const rule = ruleId
        ? await put<CategoryRule>(`/rules/${ruleId}`, body)
        : await post<CategoryRule>('/rules', { ...body, replace_existing: compact })
      let updated = 0
      if (applyNow && p && (fixable > 0 || (includeUser && p.user > 0))) {
        updated = (await post<{ updated: number }>(`/rules/${rule.id}/apply`, { include_user: includeUser })).updated
      }
      return { rule, updated }
    },
    onSuccess: ({ rule, updated }) => {
      for (const k of ['rules', 'transactions', 'summary', 'details', 'analytics']) qc.invalidateQueries({ queryKey: [k] })
      onDone(`Rule ${ruleId ? 'saved' : 'created'}: ${rule.description} → ${rule.category_name}` +
        (updated ? ` · ${updated.toLocaleString()} transaction${updated === 1 ? '' : 's'} recategorized` : ''))
    },
  })

  const patternInput = d.match_type === 'merchant'
    ? <MerchantKeyInput value={d.pattern} onChange={(v) => set('pattern', v)} />
    : <input className="input" value={d.pattern} placeholder={d.match_type === 'regex' ? 'e.g. ^(shell|exxon|bp)\\b' : 'e.g. netflix'}
        onChange={(e) => set('pattern', e.target.value)} />

  return (
    <div className="stack">
      {intro}
      <div className="grid-form">
        <Field label="Match">
          <select className="input" value={d.match_type} onChange={(e) => set('match_type', e.target.value as RuleMatch)}>
            {(Object.keys(MATCH_LABELS) as RuleMatch[]).map((m) => <option key={m} value={m}>{MATCH_LABELS[m]}</option>)}
          </select>
        </Field>
        <Field label={d.match_type === 'merchant' ? 'Merchant key' : d.match_type === 'regex' ? 'Regex (case-insensitive)' : 'Text (case-insensitive)'}>
          {patternInput}
        </Field>
      </div>
      <Field label="Set category to">
        <CategorySelect value={d.category_id} onChange={(v) => set('category_id', v)} emptyLabel="Choose…" />
      </Field>
      {compact ? (
        seedAccount && (
          <label className="row small">
            <input type="checkbox" checked={d.account_id === seedAccount.id}
              onChange={(e) => set('account_id', e.target.checked ? seedAccount.id : null)} />
            Only in {seedAccount.name}
          </label>
        )
      ) : (
        <>
          <div className="grid-form">
            <Field label="Account (optional)">
              <select className="input" value={d.account_id ?? ''} onChange={(e) => set('account_id', e.target.value ? Number(e.target.value) : null)}>
                <option value="">Any account</option>
                {(accounts.data ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            </Field>
            <Field label="Amount at least">
              <input className="input" type="number" min={0} step="0.01" value={d.amount_min} placeholder="any"
                onChange={(e) => set('amount_min', e.target.value)} />
            </Field>
            <Field label="Amount at most">
              <input className="input" type="number" min={0} step="0.01" value={d.amount_max} placeholder="any"
                onChange={(e) => set('amount_max', e.target.value)} />
            </Field>
          </div>
          <div className="grid-form">
            <Field label="Priority (lower runs first)">
              <input className="input" type="number" min={0} max={1000} value={d.priority}
                onChange={(e) => set('priority', Math.max(0, Math.min(1000, Number(e.target.value) || 0)))} />
            </Field>
            <Field label="Note">
              <input className="input" value={d.note} onChange={(e) => set('note', e.target.value)} />
            </Field>
          </div>
          <label className="row small">
            <input type="checkbox" checked={d.is_active} onChange={(e) => set('is_active', e.target.checked)} />
            Active
          </label>
        </>
      )}
      <div className="small muted-2">Amounts compare the absolute value, so a range works for charges and refunds alike.</div>

      <ErrorNote error={preview.error} />
      {p && spec && (
        <div className="stack small" style={{ borderLeft: '2px solid var(--color-divider)', paddingLeft: 8 }}>
          <div>
            <strong>Matches {p.matches.toLocaleString()} transaction{p.matches === 1 ? '' : 's'} today</strong>
            {p.matches > 0 && <> · {p.uncategorized} uncategorized · {p.auto} categorized automatically · {p.user} set by you · {p.same} already in this category</>}
          </div>
          {p.samples.length > 0 && (
            <div className="stack" style={{ gap: 2 }}>
              {p.samples.map((s) => (
                <div key={s.id} className="row muted-2" style={{ flexWrap: 'nowrap', gap: 8 }}>
                  <span className="nowrap">{shortDate(s.txn_date)}</span>
                  <Link to={txnPath(s.id)} style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.description}</Link>
                  <span className="num">{money(s.amount)}</span>
                  <span className="nowrap">{s.category_name ?? 'Uncategorized'}{s.category_source === 'user' ? ' (you)' : ''}</span>
                </div>
              ))}
              {p.uncategorized + p.auto + p.user > p.samples.length && <div className="muted-2">…and more</div>}
            </div>
          )}
          {fixable > 0 && (
            <label className="row">
              <input type="checkbox" checked={applyNow} onChange={(e) => setApplyNow(e.target.checked)} />
              Recategorize the {fixable.toLocaleString()} uncategorized / automatically categorized now
            </label>
          )}
          {p.user > 0 && (
            <label className="row">
              <input type="checkbox" checked={includeUser} disabled={fixable > 0 && !applyNow}
                onChange={(e) => { setIncludeUser(e.target.checked); if (e.target.checked) setApplyNow(true) }} />
              Also change the {p.user.toLocaleString()} you categorized by hand
            </label>
          )}
          {fixable === 0 && p.user === 0 && <div className="muted-2">Nothing to change today; the rule applies to future transactions.</div>}
        </div>
      )}

      <ErrorNote error={save.error} />
      <div className="row">
        <Button variant="primary" onClick={() => save.mutate()} disabled={!specOf(d) || save.isPending}>
          {save.isPending ? 'Saving…' : saveLabel ?? (ruleId ? 'Save rule' : 'Create rule')}
        </Button>
        <Button variant="ghost" onClick={onCancel}>{cancelLabel}</Button>
      </div>
    </div>
  )
}
