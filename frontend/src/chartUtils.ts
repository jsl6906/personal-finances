const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const PALETTE = ['var(--color-accent-700)', 'var(--color-accent-400)', 'var(--color-neutral-700)', 'var(--color-accent-900)',
  'var(--color-neutral-400)', 'var(--color-accent-500)', 'var(--color-neutral-900)', 'var(--color-accent-300)']

export const monthTick = (iso: string) => MONTHS[Number(iso.slice(5, 7)) - 1] + (iso.slice(5, 7) === '01' ? ` ${iso.slice(2, 4)}` : '')
export const seriesColor = (i: number) => PALETTE[i % PALETTE.length]
export const compactMoney = (v: number) =>
  (Math.abs(v) >= 1000 ? `$${(v / 1000).toFixed(Math.abs(v) >= 10000 ? 0 : 1)}k` : `$${Math.round(v)}`)
