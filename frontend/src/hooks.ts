import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { get, type Account, type Anomaly, type BalanceTrend, type Category, type CategoryGroup, type Institution, type Job, type Member, type MerchantHit, type Tag } from './api'

const STALE = 60_000

export const useAccounts = () => useQuery({ queryKey: ['accounts'], queryFn: () => get<Account[]>('/accounts'), staleTime: STALE })
export const useInstitutions = () =>
  useQuery({ queryKey: ['institutions'], queryFn: () => get<Institution[]>('/institutions'), staleTime: STALE })
export const useCategories = () =>
  useQuery({ queryKey: ['categories'], queryFn: () => get<Category[]>('/categories'), staleTime: STALE })
export const useGroups = () =>
  useQuery({ queryKey: ['category-groups'], queryFn: () => get<CategoryGroup[]>('/category-groups'), staleTime: STALE })
export const useMembers = () => useQuery({ queryKey: ['members'], queryFn: () => get<Member[]>('/members'), staleTime: STALE })
export const useTags = () => useQuery({ queryKey: ['tags'], queryFn: () => get<Tag[]>('/tags'), staleTime: STALE })
export const useBalanceTrend = () =>
  useQuery({ queryKey: ['balances', 'trend'], queryFn: () => get<BalanceTrend>('/balances/trend', { days: 730 }), staleTime: STALE })

export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return v
}

export function useMerchantSearch(q: string) {
  const debounced = useDebounced(q.trim(), 250)
  return useQuery({
    queryKey: ['merchants', 'search', debounced],
    queryFn: () => get<MerchantHit[]>('/merchants/search', { q: debounced, limit: 12 }),
    enabled: debounced.length >= 2,
    staleTime: 30_000,
  })
}

export function useAnomalies(status = 'open') {
  return useQuery({ queryKey: ['anomalies', status], queryFn: () => get<Anomaly[]>('/anomalies', { status, limit: 500 }) })
}

export function useJob(id: number | null) {
  return useQuery({
    queryKey: ['job', id],
    queryFn: () => get<Job>(`/jobs/${id}`),
    enabled: id !== null,
    refetchInterval: (q) => (q.state.data && ['succeeded', 'failed', 'cancelled'].includes(q.state.data.status) ? false : 1500),
  })
}

/** Categories grouped for <optgroup> rendering, active only unless `includeId` is inactive. */
export function groupCategories(cats: Category[] | undefined, includeId?: number | null) {
  const groups = new Map<string, Category[]>()
  for (const c of cats ?? []) {
    if (!c.is_active && c.id !== includeId) continue
    if (!groups.has(c.group_name)) groups.set(c.group_name, [])
    groups.get(c.group_name)!.push(c)
  }
  return [...groups.entries()]
}
