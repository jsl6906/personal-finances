export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn
}

type Params = Record<string, string | number | boolean | null | undefined | (string | number)[]>

function qs(params?: Params): string {
  if (!params) return ''
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    if (Array.isArray(v)) v.forEach((x) => sp.append(k, String(x)))
    else sp.append(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export async function api<T>(method: string, path: string, body?: unknown, params?: Params): Promise<T> {
  const res = await fetch(`/api${path}${qs(params)}`, {
    method,
    credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (res.status === 401 && !path.startsWith('/auth/')) onUnauthorized?.()
  if (!res.ok) {
    let detail = res.statusText
    try {
      const j = await res.json()
      detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const get = <T>(path: string, params?: Params) => api<T>('GET', path, undefined, params)
export const post = <T>(path: string, body?: unknown) => api<T>('POST', path, body ?? {})
export const put = <T>(path: string, body: unknown) => api<T>('PUT', path, body)
export const patch = <T>(path: string, body: unknown) => api<T>('PATCH', path, body)
export const del = (path: string) => api<void>('DELETE', path)

// ---- types mirrored from backend/src/ledger/schemas.py ----
export type CategoryType = 'expense' | 'income' | 'transfer'

export interface Institution { id: number; name: string; website: string | null; notes: string | null }
export interface Account {
  id: number; name: string; institution_id: number | null; institution_name: string | null
  account_type: string; mask: string | null; is_hidden: boolean; is_closed: boolean; notes: string | null
}
export interface Member { id: number; name: string; initials: string; email: string | null }
export interface CategoryGroup { id: number; name: string; type: CategoryType; sort_order: number; hide_from_reports: boolean }
export interface Category {
  id: number; name: string; group_id: number; group_name: string; type: CategoryType
  hide_from_reports: boolean; is_active: boolean; description: string | null
}
export interface Tag { id: number; name: string; color: string | null }

export interface Transaction {
  id: number; txn_date: string; posted_date: string | null; description: string; original_description: string | null
  merchant: string | null; merchant_name: string | null; merchant_source: 'user' | null; amount: string; account_id: number | null; account_name: string | null
  institution_name: string | null; category_id: number | null; category_name: string | null
  category_group_id: number | null; category_group_name: string | null; category_type: CategoryType | null; category_source: string | null
  suggested_category_id: number | null; suggested_category_name: string | null
  suggestion_confidence: string | null; suggestion_reason: string | null
  member_id: number | null; member_initials: string | null; notes: string | null; check_number: string | null
  budget_spread_months: number | null; source_type: string; external_id: string | null; import_batch_id: number | null
  transfer_match_id: number | null; has_statement: boolean
  tags: Tag[]; created_at: string; updated_at: string
}
export interface TransactionPage { items: Transaction[]; total: number; total_in: string; total_out: string }

export interface TxnSource {
  id: number; role: 'created' | 'matched'; origin: string | null; source_type: string | null
  import_batch_id: number | null; attachment_id: number | null; filename: string | null; mime_type: string | null
  txn_date: string | null; description: string | null; amount: string | null; match_score: string | null; created_at: string
}
export interface TxnNote {
  id: number; body: string; source: 'user' | 'import'; import_batch_id: number | null; attachment_id: number | null
  filename: string | null; created_at: string; updated_at: string
}

export interface Job {
  id: number; type: string; status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  payload: Record<string, unknown>; result: Record<string, unknown> | null; progress: string
  message: string | null; error: string | null; created_at: string; started_at: string | null; finished_at: string | null
}

export interface Summary {
  count: number; spent: string; income: string; uncategorized: number; uncategorized_all: number
  last_transaction: string | null; accounts: number
}

export const ACCOUNT_TYPES = [
  'checking', 'savings', 'credit_card', 'loan', 'mortgage', 'investment', 'retirement', 'cash', 'property', 'vehicle', 'other',
]

export async function upload<T>(path: string, file: File | null, fields: Record<string, string> = {}): Promise<T> {
  const fd = new FormData()
  if (file) fd.append('file', file)
  for (const [k, v] of Object.entries(fields)) fd.append(k, v)
  const res = await fetch(`/api${path}`, { method: 'POST', body: fd, credentials: 'same-origin' })
  if (res.status === 401) onUnauthorized?.()
  if (!res.ok) {
    let detail = res.statusText
    try {
      const j = await res.json()
      detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail)
  }
  return res.json() as Promise<T>
}

// ---- imports ----
export type ImportField =
  | 'txn_date' | 'posted_date' | 'description' | 'original_description' | 'amount' | 'debit' | 'credit'
  | 'category' | 'account' | 'notes' | 'check_number' | 'external_id' | 'ignore'

export const IMPORT_FIELDS: { value: ImportField; label: string }[] = [
  { value: 'txn_date', label: 'Transaction date' },
  { value: 'posted_date', label: 'Posted date' },
  { value: 'description', label: 'Description' },
  { value: 'original_description', label: 'Full description' },
  { value: 'amount', label: 'Amount (signed)' },
  { value: 'debit', label: 'Debit / money out' },
  { value: 'credit', label: 'Credit / money in' },
  { value: 'category', label: 'Category' },
  { value: 'account', label: 'Account' },
  { value: 'notes', label: 'Notes' },
  { value: 'check_number', label: 'Check number' },
  { value: 'external_id', label: 'Transaction ID' },
  { value: 'ignore', label: '— ignore —' },
]

export interface ImportOptions { date_format: string; dayfirst: boolean; invert_sign: boolean }
export interface ImportDefaults {
  account_id: number | null; account_map: Record<string, number | null>; category_id: number | null
  member_id: number | null; notes: string | null; tag_ids: number[]
}

export interface BatchSummary {
  id: number; filename: string | null; source_type: 'spreadsheet' | 'document'; origin: string
  status: 'extracting' | 'mapping' | 'preparing' | 'review' | 'committed' | 'rolled_back' | 'failed'
  row_count: number; stats: Record<string, number>; error: string | null; job_id: number | null
  created_at: string; committed_at: string | null
}

export interface Reconciliation { sum_of_rows: string; balance_change: string; reconciles: boolean }
export interface StatementAccount {
  ref: string; last4: string | null; name: string | null; account_type: string | null
  opening_balance: number | null; closing_balance: number | null; rows: number; reconciliation: Reconciliation | null
}

export interface DocMeta {
  document_type: string; institution: string | null; account_name: string | null; account_last4: string | null
  account_type: string | null; period_start: string | null; period_end: string | null
  opening_balance: number | null; closing_balance: number | null; sign_note: string; summary: string
  low_confidence_rows: number
  reconciliation: Reconciliation | null
  accounts?: StatementAccount[]; unassigned_rows?: number
}

export interface BatchDetail extends BatchSummary {
  attachment_id: number | null; sheet_name: string | null; sheets: string[]
  columns: { name: string; samples: string[] }[]; mapping: Record<string, ImportField>; mapping_source: string | null
  options: Partial<ImportOptions>; defaults: Partial<ImportDefaults>; doc_meta: DocMeta | null
  template_name: string | null; decisions: Record<string, number>; preview: Record<string, string>[]
}

export interface ImportRow {
  id: number; row_index: number; raw: Record<string, string>; txn_date: string | null; posted_date: string | null
  description: string | null; amount: string | null; account_id: number | null; account_name: string | null
  category_id: number | null; category_name: string | null; account_hint: string | null; category_hint: string | null
  notes: string | null; confidence: string | null; errors: string[]; decision: string; transaction_id: number | null
}

export interface TxnBrief {
  id: number; txn_date: string; description: string; amount: string; account_name: string | null
  category_name: string | null; notes: string | null; source_type: string; created_at: string
}

export interface ImportPair {
  id: number; status: string; score: string; reasons: string[]; ai_probability: string | null; ai_reason: string | null
  row: ImportRow; existing: TxnBrief
}

export interface TxnPair {
  id: number; status: string; score: string; reasons: string[]; ai_probability: string | null; ai_reason: string | null
  a: TxnBrief; b: TxnBrief
}

// ---- backfill ----
export interface BackfillSettings {
  provider: 'drive' | 'local'; folder_id: string | null; folder_name: string | null; local_path: string
  paused: boolean; auto_approve_bills: boolean; last_error: string | null
  last_scan: { found: number; new: number; unsupported: number; at: string } | null
}
export interface BackfillSummary {
  total: number; processed: number; by_status: Record<string, number>
  by_kind: Record<string, { total: number; done: number; flagged: number; skipped: number; queued: number }>
  transactions_added: number; duplicates_skipped: number; earliest: string | null; latest: string | null
}
export interface BackfillOverview {
  settings: BackfillSettings; summary: BackfillSummary; google_service_account: string | null
  inbox_configured: boolean; running: boolean; active_job: { id: number; type: string; message: string | null } | null
}
export interface BackfillFileRow {
  id: number; path: string; name: string; status: 'pending' | 'classified' | 'done' | 'review' | 'skipped' | 'failed'
  kind: string | null; period_start: string | null; message: string | null; error: string | null
  classification: { institution: string | null; account_last4: string | null; confidence: number; reason: string } | null
  attachment_id: number | null; import_batch_id: number | null; statement_id: number | null; processed_at: string | null
}

// ---- sources ----
export interface SourceResult {
  accounts?: number; fetched?: number; new?: number; inserted?: number; skipped_duplicates?: number
  needs_review?: number; batch_id?: number; categories_filled?: number; balances?: number; holdings?: number
  warnings?: string[]
}
export interface DataSource {
  id: number; kind: 'tiller' | 'simplefin'; name: string; enabled: boolean
  config: { sheet_id?: string; sheet_title?: string; lookback_days?: number; transactions_sheet?: string; balances_sheet?: string | null }
  connected: boolean; linked_accounts: number; last_sync_at: string | null
  last_status: 'ok' | 'failed' | 'needs_review' | null; last_error: string | null; last_result: SourceResult
}
export interface SourcesInfo { google_service_account: string | null; sources: DataSource[] }
export interface BalanceRow {
  account_id: number; account: string; account_type: string; institution: string | null; as_of: string
  balance: number; available: number | null; balance_30d_ago: number | null; source: string
}
export interface Balances { accounts: BalanceRow[]; assets: number; liabilities: number; net_worth: number }
export type BalanceTrend = Record<string, { as_of: string; balance: number }[]>
export interface HoldingRow {
  account_id: number; account: string; as_of: string; symbol: string | null; description: string | null
  shares: number | null; market_value: number | null; cost_basis: number | null; currency: string | null; source: string
}

// ---- alerts ----
export type AlertKind = 'budget_overspend' | 'out_of_norm' | 'large_transaction' | 'weekly_digest'
export interface AlertRule { kind: AlertKind; enabled: boolean; params: { pace?: boolean; threshold?: number }; updated_at: string }
export interface AlertRecipient { id: number; email: string; name: string | null; enabled: boolean; created_at: string }
export interface AlertEvent {
  id: number; kind: AlertKind; title: string; body: string; link: string | null
  status: 'pending' | 'sent' | 'failed' | 'skipped'; recipients: string[]; error: string | null
  created_at: string; sent_at: string | null
}
export interface AlertStatus {
  smtp_configured: boolean; smtp_host: string | null; smtp_from: string | null; app_base_url: string | null
  digest_weekday: string; recipients: number
}
export interface DigestPreview { subject: string; title: string; text: string; html: string; summary: string | null }

// ---- chat ----
export interface ChatSession { id: number; title: string; created_at: string; updated_at: string }
export interface ChatQuery {
  sql: string; purpose: string | null; columns?: string[]; rows?: unknown[][]; row_count?: number
  truncated?: boolean; error?: string
}
export interface ChatMessage {
  id: number; session_id: number; role: 'user' | 'assistant' | 'error'; content: string
  attachment_id: number | null; attachment_name: string | null; attachment_kind: 'document' | 'spreadsheet' | null
  queries: ChatQuery[]; created_at: string
}

// ---- analytics ----
export interface CashflowMonth { month: string; income: number; expenses: number; net: number; count: number }
export interface CategoryRow {
  category_id: number | null; category: string; group_id: number | null; group: string; spent: number; income: number; count: number
}
export interface Trend { months: string[]; series: { id: number | null; name: string; total: number; values: number[] }[] }
/** Filter for /analytics/contributors; in ids/exclude, 0 means Uncategorized. */
export type ContribParams = {
  start: string; end: string; basis?: 'reports' | 'all'; kind?: 'expense' | 'income'; level?: 'group' | 'category'
  ids?: number[]; exclude?: number[]; account_id?: number; merchant?: string
}
export interface Contributors {
  count: number; out: number; in: number
  merchants: { key: string; name: string; count: number; out: number; in: number }[]
  transactions: { id: number; date: string; description: string; amount: number; category: string | null }[]
}
export interface Merchant { merchant: string; example: string; spent: number; count: number; last_date: string }
export interface Anomaly {
  id: number; kind: string; period: string; title: string; detail: string; ai_note: string | null; amount: number
  baseline: number | null; score: number; status: string; transaction_id: number | null; category_id: number | null
  category_name: string | null; series_id: number | null; transaction_description: string | null
  transaction_date: string | null; created_at: string
}
export const ANOMALY_LABEL: Record<string, string> = {
  category_spike: 'Category spike', large_for_merchant: 'Large for merchant', large_transaction: 'Large transaction',
  new_merchant: 'New merchant', bill_increase: 'Bill increase', unmatched_transfer: 'Unmatched transfer',
}

// ---- detail pages ----
export interface EntityStats {
  count: number; first_date: string | null; last_date: string | null; spent: number; received: number
  count_out: number; count_in: number; avg_out: number | null; median_out: number | null; max_out: number | null
  count_12m: number; spent_12m: number; received_12m: number
}
export interface EntityMonth { month: string; spent: number; received: number; net: number; count: number }
export interface EntityYear { year: number; spent: number; received: number; net: number; count: number }
export interface BreakdownRow {
  id: number | string | null; name: string; spent: number; received: number; net: number; count: number; last_date: string | null
}
export interface Charge { id: number; date: string; amount: number; description: string; account: string | null }
export interface Cadence { label: string; days: number; next_date: string; typical_amount: number | null; lapsed: boolean }
export interface BudgetInfo { id: number; amount: number; period_type: PeriodType; monthly: number }
interface EntityDetail { start: string; end: string; stats: EntityStats; monthly: EntityMonth[]; yearly: EntityYear[] }
interface CategoryLinks {
  bill_series: { id: number; name: string }[]; spread_rules: { id: number; name: string; months: number }[]; merchant_rules: number
}
export interface MerchantDetail extends EntityDetail {
  key: string; name: string; display_name: string | null; aliases: string[]; overridden: number
  descriptions: { description: string; count: number; last_date: string }[]
  latest_description: string; charges: Charge[]; cadence: Cadence | null; categories: BreakdownRow[]; accounts: BreakdownRow[]
  rule: { category_id: number; category_name: string; source: string; hits: number } | null
}
export interface AccountDetail extends EntityDetail {
  account: {
    id: number; name: string; institution_name: string | null; account_type: string; mask: string | null
    is_hidden: boolean; is_closed: boolean; notes: string | null; sources: string[]
  }
  balance: { as_of: string; balance: number; available: number | null; source: string } | null
  balance_history: { as_of: string; balance: number }[]
  holdings: { symbol: string | null; description: string | null; shares: number | null; market_value: number | null; cost_basis: number | null; as_of: string }[]
  categories: BreakdownRow[]; merchants: BreakdownRow[]
}
export interface CategoryDetail extends EntityDetail, CategoryLinks {
  category: {
    id: number; name: string; type: CategoryType; group_id: number; group_name: string; description: string | null
    hide_from_reports: boolean; is_active: boolean
  }
  budget: BudgetInfo | null; merchants: BreakdownRow[]; accounts: BreakdownRow[]
}
export interface GroupDetail extends EntityDetail, CategoryLinks {
  group: { id: number; name: string; type: CategoryType; hide_from_reports: boolean }
  budget: BudgetInfo | null
  categories: {
    id: number; name: string; is_active: boolean; budget: BudgetInfo | null; spent: number; received: number; net: number
    count: number; last_date: string | null
  }[]
  trend: { months: string[]; series: { id: number | null; name: string; values: number[]; total: number }[] }
  merchants: BreakdownRow[]; accounts: BreakdownRow[]
}
export interface MerchantHit { key: string; name: string; count: number; last_date: string }
export interface TxnBriefDetail {
  id: number; txn_date: string; description: string; amount: number; account_id: number | null; account_name: string | null
}
export interface TxnContext {
  merchant: string; merchant_name: string; stats: EntityStats; charges: Charge[]; cadence: Cadence | null
  comparison: { count: number; median: number; min: number; max: number; diff_pct: number | null; rank_pct: number } | null
  transfer_match: TxnBriefDetail | null; same_day: TxnBriefDetail[]
}

// ---- budgets ----
export type PeriodType = 'month' | 'quarter' | 'year'
export interface Budget {
  id: number; name: string; category_id: number | null; group_id: number | null; period_type: PeriodType
  amount: string; notes: string | null
}
export interface BudgetRow {
  budget_id: number; category_id: number | null; group_id: number | null; name: string; group: string | null
  kind: string; period_type: PeriodType; base_amount: number; budget: number; actual: number; left: number
  pct: number; projected: number; spread_amount: number; notes: string | null; status: 'ok' | 'pace' | 'over'
}
export interface BudgetStatus {
  period: { type: PeriodType; start: string; end: string; label: string; elapsed: number }
  rows: BudgetRow[]
  total: { spent: number; budget: number; left: number; over_count: number; count: number }
  unbudgeted: { category_id: number; name: string; group: string; actual: number }[]
}
export interface SpreadRule {
  id: number; name: string; category_id: number | null; category_name: string | null; merchant_pattern: string | null
  min_amount: string | null; months: number; is_active: boolean; matches_12m: number; total_12m: string
}
export interface BudgetSuggestion { category_id: number; name: string; group: string; monthly_average: number; suggested: number }

// ---- statements ----
export interface Usage { id?: number; metric: string; value: string; unit: string | null; is_primary: boolean }
export interface LinkCandidate {
  transaction_id: number; date: string; amount: string; description: string; score: number; reason: string
}
export interface StatementSuggestion {
  series_id?: number | null; series_reason?: string; new_series_name?: string | null
  new_series_category_id?: number | null; service_type?: string | null; candidates?: LinkCandidate[]
  transaction_ids?: number[]; preset_transaction_ids?: number[]
}
export interface Statement {
  id: number; attachment_id: number; filename: string; series_id: number | null; series_name: string | null
  status: 'processing' | 'suggested' | 'approved' | 'dismissed' | 'failed'; document_type: string | null
  vendor: string | null; account_ref: string | null; statement_date: string | null; period_start: string | null
  period_end: string | null; due_date: string | null; amount_due: string | null; summary: string | null
  suggestion: StatementSuggestion; error: string | null; job_id: number | null; usage: Usage[]
  transactions: TxnBrief[]; created_at: string; approved_at: string | null
}
export interface Series {
  id: number; name: string; vendor: string | null; service_type: string | null; category_id: number | null
  category_name: string | null; primary_metric: string | null; unit: string | null; notes: string | null
  is_active: boolean; tag_id: number | null; statement_count: number; latest_period_end: string | null
}
export interface SeriesPoint {
  statement_id: number; filename: string; attachment_id: number; period_start: string | null; period_end: string | null
  statement_date: string | null; amount_due: string | null; usage_value: string | null; usage_unit: string | null
  cost_per_unit: string | null; transactions: TxnBrief[]
}
