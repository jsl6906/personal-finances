import { useState } from 'react'
import type { BreakdownRow, CategoryType, EntityMonth } from './api'
import { iso, monthLabel, parseIso } from './format'
import { accountPath, categoryPath, groupPath } from './links'

export type DetailRange = '12m' | '24m' | '5y' | 'all'
const BACK: Record<DetailRange, number> = { '12m': 11, '24m': 23, '5y': 59, all: 0 }

/** Range + picked-month state shared by the detail pages; `start` is undefined for all time. */
export function useDetailRange(initial: DetailRange) {
  const [range, setRangeRaw] = useState<DetailRange>(initial)
  const [month, setMonth] = useState<string | null>(null)
  const [today] = useState(() => new Date())
  const start = range === 'all' ? undefined : iso(new Date(today.getFullYear(), today.getMonth() - BACK[range], 1))
  const setRange = (r: DetailRange) => { setRangeRaw(r); setMonth(null) }
  const pick = (m: string) => setMonth(month === m ? null : m)
  return { range, setRange, start, month, pick, clearMonth: () => setMonth(null) }
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
