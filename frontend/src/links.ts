export const txnPath = (id: number) => `/transactions/${id}`
export const merchantPath = (key: string) => `/merchants?key=${encodeURIComponent(key)}`
export const accountPath = (id: number) => `/accounts/${id}`
export const categoryPath = (id: number) => `/categories/${id}`
export const groupPath = (id: number) => `/groups/${id}`
export const merchantKey = (t: { merchant: string | null; description: string }) => t.merchant ?? t.description.toLowerCase()
