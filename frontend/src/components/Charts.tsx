import { compactMoney as compact, monthTick, seriesColor } from '../chartUtils'

/** Paired income/expense bars per month with a net marker, as in the dashboard prototype. */
export function CashflowChart({ data, height = 220, onPick }: {
  data: { month: string; income: number; expenses: number; net: number }[]; height?: number; onPick?: (month: string) => void
}) {
  const w = 640
  const base = height - 20
  const max = Math.max(1, ...data.flatMap((d) => [d.income, d.expenses])) * 1.1
  const slot = (w - 40) / Math.max(1, data.length)
  const bar = Math.max(1, Math.min(18, slot / 2 - 3))
  const every = Math.ceil(data.length / 18)
  const y = (v: number) => base - (v / max) * (base - 14)
  const grid = [max / 2, max].map((v) => Math.round(v / 100) * 100)
  return (
    <svg viewBox={`0 0 ${w} ${height}`} width="100%" style={{ display: 'block' }}>
      <line x1="0" y1={base} x2={w} y2={base} stroke="var(--color-divider)" />
      {grid.map((g) => (
        <g key={g}>
          <line x1="36" y1={y(g)} x2={w} y2={y(g)} stroke="var(--color-divider)" strokeDasharray="2 4" />
          <text x="0" y={y(g) + 3} fontSize="10" fill="var(--color-neutral-600)" fontFamily="Barlow">{compact(g)}</text>
        </g>
      ))}
      {data.map((d, i) => {
        const x = 40 + i * slot + slot / 2
        return (
          <g key={d.month} style={{ cursor: onPick ? 'pointer' : undefined }} onClick={() => onPick?.(d.month)}>
            <title>{`${d.month.slice(0, 7)} · income ${compact(d.income)} · expenses ${compact(d.expenses)} · net ${compact(d.net)}`}</title>
            <rect x={x - bar - 1} y={y(d.income)} width={bar} height={base - y(d.income)} fill="var(--color-accent-300)" />
            <rect x={x + 1} y={y(d.expenses)} width={bar} height={base - y(d.expenses)} fill="var(--color-accent-700)" />
            {i % every === 0 && (
              <text x={x} y={height - 4} fontSize="10" textAnchor="middle" fill="var(--color-neutral-600)" fontFamily="Barlow">
                {monthTick(d.month)}
              </text>
            )}
          </g>
        )
      })}
    </svg>
  )
}

export function Legend({ items }: { items: { label: string; color: string }[] }) {
  return (
    <div className="row small muted-2" style={{ gap: 'var(--space-3)' }}>
      {items.map((it) => (
        <span key={it.label} className="row" style={{ gap: 5, flexWrap: 'nowrap' }}>
          <span style={{ width: 10, height: 10, background: it.color, display: 'inline-block' }} />{it.label}
        </span>
      ))}
    </div>
  )
}

/** Several monthly series as lines. */
export function TrendChart({ months, series, height = 220 }: {
  months: string[]; series: { name: string; values: number[] }[]; height?: number
}) {
  const w = 640
  const base = height - 20
  const max = Math.max(1, ...series.flatMap((s) => s.values)) * 1.1
  const step = months.length > 1 ? (w - 60) / (months.length - 1) : 0
  const x = (i: number) => 44 + i * step
  const y = (v: number) => base - (v / max) * (base - 14)
  const every = Math.ceil(months.length / 12)
  return (
    <svg viewBox={`0 0 ${w} ${height}`} width="100%" style={{ display: 'block' }}>
      <line x1="0" y1={base} x2={w} y2={base} stroke="var(--color-divider)" />
      {[max / 2, max].map((g) => (
        <g key={g}>
          <line x1="40" y1={y(g)} x2={w} y2={y(g)} stroke="var(--color-divider)" strokeDasharray="2 4" />
          <text x="0" y={y(g) + 3} fontSize="10" fill="var(--color-neutral-600)" fontFamily="Barlow">{compact(g)}</text>
        </g>
      ))}
      {series.map((s, si) => (
        <polyline key={s.name} fill="none" stroke={seriesColor(si)} strokeWidth="1.5"
          points={s.values.map((v, i) => `${x(i)},${y(v)}`).join(' ')}>
          <title>{s.name}</title>
        </polyline>
      ))}
      {months.map((m, i) => i % every === 0 && (
        <text key={m} x={x(i)} y={height - 4} fontSize="10" textAnchor="middle" fill="var(--color-neutral-600)" fontFamily="Barlow">
          {monthTick(m)}
        </text>
      ))}
    </svg>
  )
}

/** Monthly bars for one measure, with an optional budget line and a highlighted month. */
export function MonthlyBars({ data, budget, highlight, onPick, height = 200, width = 960 }: {
  data: { month: string; value: number }[]; budget?: number | null; highlight?: string | null
  onPick?: (month: string) => void; height?: number; width?: number
}) {
  const w = width
  const base = height - 20
  const max = Math.max(1, budget ?? 0, ...data.map((d) => d.value)) * 1.1
  const slot = (w - 40) / Math.max(1, data.length)
  const bar = Math.max(1, Math.min(28, slot - 4))
  const every = Math.ceil(data.length / 18)
  const y = (v: number) => base - (Math.max(0, v) / max) * (base - 14)
  return (
    <svg viewBox={`0 0 ${w} ${height}`} width="100%" style={{ display: 'block' }}>
      <line x1="0" y1={base} x2={w} y2={base} stroke="var(--color-divider)" />
      {[max / 2, max].map((g) => (
        <g key={g}>
          <line x1="36" y1={y(g)} x2={w} y2={y(g)} stroke="var(--color-divider)" strokeDasharray="2 4" />
          <text x="0" y={y(g) + 3} fontSize="10" fill="var(--color-neutral-600)" fontFamily="Barlow">{compact(g)}</text>
        </g>
      ))}
      {data.map((d, i) => {
        const x = 40 + i * slot + slot / 2
        const on = highlight === d.month
        const prev = i >= every ? data[i - every].month : null
        const tick = monthTick(d.month) + (d.month.slice(5, 7) !== '01' && prev?.slice(0, 4) !== d.month.slice(0, 4) ? ` ${d.month.slice(2, 4)}` : '')
        return (
          <g key={d.month} style={{ cursor: onPick ? 'pointer' : undefined }} onClick={() => onPick?.(d.month)}>
            <title>{`${d.month.slice(0, 7)} · ${compact(d.value)}`}</title>
            <rect x={x - slot / 2} y={0} width={slot} height={base} fill="transparent" />
            <rect x={x - bar / 2} y={y(d.value)} width={bar} height={base - y(d.value)}
              fill={on ? 'var(--color-accent-900)' : budget && d.value > budget ? 'var(--color-accent-700)' : 'var(--color-accent-400)'} />
            {i % every === 0 && (
              <text x={x} y={height - 4} fontSize="10" textAnchor="middle" fill="var(--color-neutral-600)" fontFamily="Barlow">
                {tick}
              </text>
            )}
          </g>
        )
      })}
      {budget ? (
        <g>
          <line x1="36" y1={y(budget)} x2={w} y2={y(budget)} stroke="var(--color-text)" strokeDasharray="4 3" />
          <text x={w - 4} y={y(budget) - 4} fontSize="10" textAnchor="end" fill="var(--color-text)" fontFamily="Barlow">
            budget {compact(budget)}/mo
          </text>
        </g>
      ) : null}
    </svg>
  )
}

const DAY = 86_400_000
const toMs = (s: string) => { const [y, m, d] = s.slice(0, 10).split('-').map(Number); return Date.UTC(y, m - 1, d) }

/** Individual charges on a time axis (absolute amounts); one can be highlighted, e.g. the transaction being viewed. */
export function ChargesChart({ charges, highlightId, median, onPick, height = 200, width = 960 }: {
  charges: { id: number; date: string; amount: number; description: string }[]; highlightId?: number | null
  median?: number | null; onPick?: (id: number) => void; height?: number; width?: number
}) {
  if (charges.length === 0) return <div className="text-muted small">No transactions.</div>
  const w = width
  const base = height - 20
  const left = 40
  let t0 = toMs(charges[0].date)
  let t1 = toMs(charges[charges.length - 1].date)
  if (t1 - t0 < 30 * DAY) { t0 -= 15 * DAY; t1 += 15 * DAY }
  const max = Math.max(1, ...charges.map((c) => Math.abs(c.amount))) * 1.1
  const x = (s: string) => left + ((toMs(s) - t0) / (t1 - t0)) * (w - left - 10)
  const y = (v: number) => base - (Math.abs(v) / max) * (base - 14)
  const y0 = new Date(t0).getUTCFullYear()
  const y1 = new Date(t1).getUTCFullYear()
  const spanDays = (t1 - t0) / DAY
  const ticks: { at: string; label: string }[] = []
  if (spanDays > 540) {
    const step = Math.ceil((y1 - y0 + 1) / 12)
    for (let yr = y0 + 1; yr <= y1; yr += step) ticks.push({ at: `${yr}-01-01`, label: String(yr) })
  } else {
    const d0 = new Date(t0)
    for (let i = 1; ; i++) {
      const d = new Date(Date.UTC(d0.getUTCFullYear(), d0.getUTCMonth() + i, 1))
      if (d.getTime() > t1) break
      const at = d.toISOString().slice(0, 10)
      if (spanDays < 200 || i % 2 === 0) ticks.push({ at, label: monthTick(at) })
    }
  }
  const hi = charges.find((c) => c.id === highlightId)
  const stem = charges.length <= 120
  return (
    <svg viewBox={`0 0 ${w} ${height}`} width="100%" style={{ display: 'block' }}>
      <line x1="0" y1={base} x2={w} y2={base} stroke="var(--color-divider)" />
      {[max / 2, max].map((g) => (
        <g key={g}>
          <line x1="36" y1={y(g)} x2={w} y2={y(g)} stroke="var(--color-divider)" strokeDasharray="2 4" />
          <text x="0" y={y(g) + 3} fontSize="10" fill="var(--color-neutral-600)" fontFamily="Barlow">{compact(g)}</text>
        </g>
      ))}
      {ticks.map((t) => (
        <text key={t.at} x={x(t.at)} y={height - 4} fontSize="10" textAnchor="middle" fill="var(--color-neutral-600)" fontFamily="Barlow">
          {t.label}
        </text>
      ))}
      {median ? (
        <g>
          <line x1="36" y1={y(median)} x2={w} y2={y(median)} stroke="var(--color-accent-500)" strokeDasharray="3 4" />
          <text x={hi && x(hi.date) > w * 0.8 ? left + 4 : w - 4} y={y(median) - 4} fontSize="10"
            textAnchor={hi && x(hi.date) > w * 0.8 ? 'start' : 'end'} fill="var(--color-neutral-600)" fontFamily="Barlow">
            typical {compact(median)}
          </text>
        </g>
      ) : null}
      {charges.map((c) => c.id === highlightId ? null : (
        <g key={c.id} style={{ cursor: onPick ? 'pointer' : undefined }} onClick={() => onPick?.(c.id)}>
          <title>{`${c.date} · ${c.description} · ${c.amount > 0 ? '+' : ''}${compact(Math.abs(c.amount))}`}</title>
          {stem && <line x1={x(c.date)} y1={base} x2={x(c.date)} y2={y(c.amount)} stroke="var(--color-accent-300)" />}
          <circle cx={x(c.date)} cy={y(c.amount)} r="3" fill={c.amount > 0 ? 'var(--color-bg)' : 'var(--color-accent-600)'}
            stroke="var(--color-accent-700)" strokeWidth="1" />
        </g>
      ))}
      {hi && (
        <g>
          <title>{`${hi.date} · ${hi.description} · ${compact(Math.abs(hi.amount))} (this transaction)`}</title>
          <line x1={x(hi.date)} y1={base} x2={x(hi.date)} y2={y(hi.amount)} stroke="var(--color-accent-900)" strokeWidth="2" />
          <circle cx={x(hi.date)} cy={y(hi.amount)} r="6" fill="var(--color-accent-900)" stroke="var(--color-bg)" strokeWidth="2" />
          <text x={Math.min(w - 30, Math.max(left + 30, x(hi.date)))} y={Math.max(12, y(hi.amount) - 10)} fontSize="11"
            textAnchor="middle" fontWeight="600" fill="var(--color-accent-900)" fontFamily="Barlow">
            {compact(Math.abs(hi.amount))}
          </text>
        </g>
      )}
    </svg>
  )
}
