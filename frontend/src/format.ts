const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const LONG_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
  'November', 'December']

export function money(value: string | number | null | undefined, plus = false): string {
  if (value === null || value === undefined || value === '') return '—'
  const n = typeof value === 'number' ? value : Number(value)
  const s = Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return (n < 0 ? '−' : plus && n > 0 ? '+' : '') + '$' + s
}

export function moneyRound(value: string | number): string {
  const n = typeof value === 'number' ? value : Number(value)
  return (n < 0 ? '−' : '') + '$' + Math.round(Math.abs(n)).toLocaleString('en-US')
}

export function iso(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export function parseIso(s: string): Date {
  const [y, m, d] = s.split('-').map(Number)
  return new Date(y, m - 1, d)
}

export function shortDate(s: string | null): string {
  if (!s) return '—'
  const d = parseIso(s.slice(0, 10))
  const year = d.getFullYear() === new Date().getFullYear() ? '' : ` '${String(d.getFullYear()).slice(2)}`
  return `${String(d.getDate()).padStart(2, '0')} ${MONTHS[d.getMonth()]}${year}`
}

export function fullDate(s: string | null): string {
  if (!s) return '—'
  // Timestamps are UTC; plain dates are calendar dates and must not shift by timezone.
  const d = s.length > 10 ? new Date(s) : parseIso(s)
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`
}

export function monthLabel(d: Date): string {
  return `${LONG_MONTHS[d.getMonth()]} ${d.getFullYear()}`
}

/** Last day (ISO) of the month containing the ISO date `m`. */
export function monthEnd(m: string): string {
  const d = parseIso(m.slice(0, 10))
  return iso(new Date(d.getFullYear(), d.getMonth() + 1, 0))
}

export function monthName(d: Date): string {
  return LONG_MONTHS[d.getMonth()]
}

export type PeriodKey = 'this_month' | 'last_month' | 'last_90' | 'this_year' | 'last_year' | 'all' | 'custom'

export const PERIODS: { key: PeriodKey; label: string }[] = [
  { key: 'this_month', label: 'This month' },
  { key: 'last_month', label: 'Last month' },
  { key: 'last_90', label: 'Last 90 days' },
  { key: 'this_year', label: 'This year' },
  { key: 'last_year', label: 'Last year' },
  { key: 'all', label: 'All time' },
  { key: 'custom', label: 'Custom range' },
]

export function periodRange(key: PeriodKey, today = new Date()): { start?: string; end?: string } {
  const y = today.getFullYear()
  const m = today.getMonth()
  switch (key) {
    case 'this_month':
      return { start: iso(new Date(y, m, 1)), end: iso(new Date(y, m + 1, 0)) }
    case 'last_month':
      return { start: iso(new Date(y, m - 1, 1)), end: iso(new Date(y, m, 0)) }
    case 'last_90':
      return { start: iso(new Date(y, m, today.getDate() - 90)), end: iso(today) }
    case 'this_year':
      return { start: iso(new Date(y, 0, 1)), end: iso(new Date(y, 11, 31)) }
    case 'last_year':
      return { start: iso(new Date(y - 1, 0, 1)), end: iso(new Date(y - 1, 11, 31)) }
    default:
      return {}
  }
}
