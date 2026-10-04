export const txnPath = (id: number) => `/transactions/${id}`
export const merchantPath = (key: string) => `/merchants?key=${encodeURIComponent(key)}`
export const accountPath = (id: number) => `/accounts/${id}`
export const categoryPath = (id: number) => `/categories/${id}`
export const groupPath = (id: number) => `/groups/${id}`
export function txnsPath(f: { category?: number; categories?: number[]; start?: string; end?: string; label?: string }): string {
  const sp = new URLSearchParams()
  if (f.category) sp.set('category', String(f.category))
  if (f.categories?.length) sp.set('categories', f.categories.join(','))
  if (f.start) sp.set('start', f.start)
  if (f.end) sp.set('end', f.end)
  if (f.label) sp.set('label', f.label)
  return `/transactions?${sp}`
}
export const merchantKey = (t: { merchant: string | null; description: string }) => t.merchant ?? t.description.toLowerCase()
