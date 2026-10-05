import { useState } from 'react'
import type { BreakdownRow, CategoryType, EntityMonth } from './api'
import { iso, monthLabel, parseIso } from './format'
import { accountPath, categoryPath, groupPath } from './links'
import { oneOf, useUrl } from './urlState'

export type DetailRange = '12m' | '24m' | '5y' | 'all'
const BACK: Record<DetailRange, number> = { '12m': 11, '24m': 23, '5y': 59, all: 0 }
const RANGES = Object.keys(BACK) as DetailRange[]

/** Server-side sort columns of /transactions and the direction each starts in. */
export const TXN_SORT = { date: 'desc', description: 'asc', category: 'asc', account: 'asc', amount: 'desc' } as const

/** Range + picked-month state shared by the detail pages, kept in the URL (`range`, `month`); `start` is undefined
 * for all time. Changing either resets the transaction list's page. */
export function useDetailRange(initial: DetailRange) {
  const [params, set] = useUrl()
  const range = oneOf(params, 'range', RANGES, initial)
  const month = params.get('month')
  const [today] = useState(() => new Date())
  const start = range === 'all' ? undefined : iso(new Date(today.getFullYear(), today.getMonth() - BACK[range], 1))
  const setRange = (r: DetailRange) => set({ range: r === initial ? null : r, month: null, page: null })
  const pick = (m: string) => set({ month: month === m ? null : m, page: null })
  return { range, setRange, start, month, pick, clearMonth: () => set({ month: null, page: null }) }
}

/** "January 2020 – September 2026" style label for a detail range. */
export function rangeLabel(start: string, end: string) {
  return `${monthLabel(parseIso(start))} – ${monthLabel(parseIso(end))}`
}

/** The natural measure for a category type: net spend for expenses, net income for income, outflow for transfers. */
export function measure(type: CategoryType) {
  return (m: { spent: number; received: number; net: number }) => (type === 'income' ? m.net : type === 'expense' ? -m.net : m.spent)
}

/** Range total/average, trailing-12 total and the latest (current) month of a monthly series. */
export function summarize(monthly: EntityMonth[], value: (m: EntityMonth) => number) {
  const total = monthly.reduce((a, m) => a + value(m), 0)
  const cur = monthly[monthly.length - 1]
  const last12 = monthly.slice(-12).reduce((a, m) => a + value(m), 0)
  return { total, avg: total / Math.max(1, monthly.length), current: cur ? value(cur) : 0, last12 }
}

export const breakdownPath = {
  category: (r: BreakdownRow) => (typeof r.id === 'number' ? categoryPath(r.id) : null),
  group: (r: BreakdownRow) => (typeof r.id === 'number' ? groupPath(r.id) : null),
  account: (r: BreakdownRow) => (typeof r.id === 'number' ? accountPath(r.id) : null),
}
