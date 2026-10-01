const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const PALETTE = ['var(--color-accent-700)', 'var(--color-accent-400)', 'var(--color-neutral-700)', 'var(--color-accent-900)',
  'var(--color-neutral-400)', 'var(--color-accent-500)', 'var(--color-neutral-900)', 'var(--color-accent-300)']

export const monthTick = (iso: string) => MONTHS[Number(iso.slice(5, 7)) - 1] + (iso.slice(5, 7) === '01' ? ` ${iso.slice(2, 4)}` : '')
export const seriesColor = (i: number) => PALETTE[i % PALETTE.length]
// Muted hues so adjacent stack segments stay distinguishable; "Other" is always light gray.
const STACK = ['#416180', '#94bce3', '#c9a26b', '#6f9a8d', '#b77b7b', '#8a7fa8', '#5d5d60', '#a7b98a']
export const stackColor = (name: string, i: number) => (name === 'Other' ? 'var(--color-neutral-300)' : STACK[i % STACK.length])
export const compactMoney = (v: number) =>
  (Math.abs(v) >= 1000 ? `$${(v / 1000).toFixed(Math.abs(v) >= 10000 ? 0 : 1)}k` : `$${Math.round(v)}`)
