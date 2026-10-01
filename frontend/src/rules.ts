import type { CategoryRule, RuleMatch, RuleSpec } from './api'

export type RuleDraft = {
  match_type: RuleMatch
  pattern: string
  category_id: number | null
  account_id: number | null
  amount_min: string
  amount_max: string
  priority: number
  is_active: boolean
  note: string
}

export const MATCH_LABELS: Record<RuleMatch, string> = {
  merchant: 'Merchant is',
  contains: 'Description contains',
  regex: 'Description matches regex',
}

export const SOURCE_LABELS: Record<string, string> = { user: 'You', learned: 'Learned (legacy)', ai: 'Accepted AI suggestion' }

export function ruleDraft(r?: Partial<CategoryRule>): RuleDraft {
  return {
    match_type: r?.match_type ?? 'merchant',
    pattern: r?.pattern ?? '',
    category_id: r?.category_id ?? null,
    account_id: r?.account_id ?? null,
    amount_min: r?.amount_min ?? '',
    amount_max: r?.amount_max ?? '',
    priority: r?.priority ?? 100,
    is_active: r?.is_active ?? true,
    note: r?.note ?? '',
  }
}

export function ruleBody(d: RuleDraft) {
  return {
    match_type: d.match_type,
    pattern: d.pattern.trim(),
    category_id: d.category_id,
    account_id: d.account_id,
    amount_min: d.amount_min === '' ? null : Number(d.amount_min).toFixed(2),
    amount_max: d.amount_max === '' ? null : Number(d.amount_max).toFixed(2),
    priority: d.priority,
    is_active: d.is_active,
    note: d.note.trim() || null,
  }
}

/** The part of a draft the preview endpoint needs, or null while it's incomplete. */
export function specOf(d: RuleDraft): RuleSpec | null {
  const b = ruleBody(d)
  if (!b.category_id || b.pattern.length < (d.match_type === 'merchant' ? 1 : 2)) return null
  if ([b.amount_min, b.amount_max].some((v) => v !== null && Number.isNaN(Number(v)))) return null
  return { ...b, category_id: b.category_id }
}
