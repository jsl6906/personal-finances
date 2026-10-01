import type { ReactNode } from 'react'
import { compactMoney as compact, monthTick, seriesColor, stackColor } from '../chartUtils'
import { tipHandlers, useTip, type TipSpec } from '../tip'

/** Paired income/expense bars per month with a net marker, as in the dashboard prototype. */
export function CashflowChart({ data, height = 220, onPick, tip }: {
  data: { month: string; income: number; expenses: number; net: number }[]; height?: number; onPick?: (month: string) => void
  tip?: (month: string, part?: 'income' | 'expenses') => TipSpec | null
}) {
  const t = useTip()
  const hover = (m: string, part?: 'income' | 'expenses') => (tip ? tipHandlers(t, () => tip(m, part)) : {})
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
            {!tip && <title>{`${d.month.slice(0, 7)} · income ${compact(d.income)} · expenses ${compact(d.expenses)} · net ${compact(d.net)}`}</title>}
            <rect x={x - slot / 2} y={0} width={slot} height={base} fill="transparent" {...hover(d.month)} />
            <rect x={x - bar - 1} y={y(d.income)} width={bar} height={base - y(d.income)} fill="var(--color-accent-300)" {...hover(d.month, 'income')} />
            <rect x={x + 1} y={y(d.expenses)} width={bar} height={base - y(d.expenses)} fill="var(--color-accent-700)" {...hover(d.month, 'expenses')} />
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

export const NET_UP = 'var(--color-accent-500)'
export const NET_DOWN = '#c98a8a'
const signed = (v: number) => (v > 0 ? '+' : v < 0 ? '−' : '') + compact(Math.abs(v))

function niceTicks(lo: number, hi: number, count = 5) {
  const raw = (hi - lo || 1) / count
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag
  const out: number[] = []
  for (let i = Math.floor(lo / step); i <= Math.ceil(hi / step); i++) out.push(i * step)
  return out
}

/** Axis label for month i, or null to skip: years for long ranges, otherwise every nth month. */
function monthLabeler(months: string[]) {
  const yearly = months.length > 36
  const yearStep = Math.ceil(months.length / 12 / 12)
  const every = Math.ceil(months.length / 14)
  return (i: number) => {
    const m = months[i]
    if (yearly) return m.slice(5, 7) === '01' && Number(m.slice(0, 4)) % yearStep === 0 ? m.slice(0, 4) : null
    if (i % every) return null
    return monthTick(m) + (i > 0 && m.slice(5, 7) !== '01' && months[i - every]?.slice(0, 4) !== m.slice(0, 4) ? ` ${m.slice(2, 4)}` : '')
  }
}

const trailingAvg = (vals: number[], n = 12) =>
  vals.map((_, i) => { const s = vals.slice(Math.max(0, i - n + 1), i + 1); return s.reduce((a, v) => a + v, 0) / s.length })

/** Monthly stacked bars (one segment per series) with a trailing 12-month average of the total. */
export function StackedBars({ months, series, onPick, highlight, height = 240, tip }: {
  months: string[]; series: { name: string; values: number[] }[]; onPick?: (month: string) => void
  highlight?: string | null; height?: number
  tip?: (i: number, si: number | null, ctx: { total: number; avg: number }) => TipSpec | null
}) {
  const t = useTip()
  const w = 640
  const top = 8
  const base = height - 20
  const left = 44
  const totals = months.map((_, i) => series.reduce((a, s) => a + Math.max(0, s.values[i] ?? 0), 0))
  const avg = trailingAvg(totals)
  const ticks = niceTicks(0, Math.max(1, ...totals, ...avg), 4)
  const hi = ticks[ticks.length - 1]
  const y = (v: number) => base - (v / hi) * (base - top)
  const slot = (w - left) / Math.max(1, months.length)
  const bar = Math.max(1, Math.min(22, slot - 2))
  const cx = (i: number) => left + i * slot + slot / 2
  const label = monthLabeler(months)
  const hover = (i: number, si: number | null) =>
    tip ? tipHandlers(t, () => tip(i, si, { total: totals[i], avg: avg[i] })) : {}
  return (
    <svg viewBox={`0 0 ${w} ${height}`} width="100%" style={{ display: 'block' }}>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={left - 4} y1={y(t)} x2={w} y2={y(t)} stroke="var(--color-divider)" strokeDasharray={t === 0 ? undefined : '2 4'} />
          <text x="0" y={y(t) + 3} fontSize="10" fill="var(--color-neutral-600)" fontFamily="Barlow">{compact(t)}</text>
        </g>
      ))}
      {months.map((m, i) => {
        const tick = label(i)
        const pos = (s: { values: number[] }) => Math.max(0, s.values[i] ?? 0)
        const segs = series.map((s, si) => {
          const below = series.slice(0, si).reduce((a, p) => a + pos(p), 0)
          return { name: s.name, si, y0: y(below), y1: y(below + pos(s)), v: pos(s) }
        })
        const title = [`${m.slice(0, 7)} · total ${compact(totals[i])} · 12-mo avg ${compact(avg[i])}`,
          ...series.filter((s) => s.values[i]).map((s) => `${s.name}: ${compact(s.values[i])}`)].join('\n')
        return (
          <g key={m} style={{ cursor: onPick ? 'pointer' : undefined }} onClick={() => onPick?.(m)}>
            {!tip && <title>{title}</title>}
            <rect x={cx(i) - slot / 2} y={0} width={slot} height={base} {...hover(i, null)}
              fill={highlight === m ? 'color-mix(in srgb, var(--color-accent) 14%, transparent)' : 'transparent'} />
            {segs.map((sg) => sg.v > 0 && (
              <rect key={sg.name} x={cx(i) - bar / 2} y={sg.y1} width={bar} height={sg.y0 - sg.y1} fill={stackColor(sg.name, sg.si)}
                {...hover(i, sg.si)} />
            ))}
            {tick && (
              <text x={cx(i)} y={height - 4} fontSize="10" textAnchor="middle" fill="var(--color-neutral-600)" fontFamily="Barlow">{tick}</text>
            )}
          </g>
        )
      })}
      {avg.length > 1 && (
        <polyline fill="none" stroke="var(--color-text)" strokeWidth="1.5" strokeDasharray="4 3" pointerEvents="none"
          points={avg.map((v, i) => `${cx(i)},${y(v)}`).join(' ')} />
      )}
    </svg>
  )
}

/** Net income by month: a running-total waterfall, or monthly bars around zero with a trailing 12-month average. */
export function NetChart({ data, mode, onPick, height = 260, tip }: {
  data: { month: string; net: number }[]; mode: 'waterfall' | 'monthly'; onPick?: (month: string) => void; height?: number
  tip?: (month: string, ctx: { net: number; running: number; avg?: number }) => TipSpec | null
}) {
  const t = useTip()
  const w = 640
  const top = 8
  const base = height - 20
  const left = 44
  const totals = data.reduce<number[]>((acc, d) => [...acc, (acc[acc.length - 1] ?? 0) + d.net], [])
  const bars = data.map((d, i) => {
    const total = totals[i]
    return mode === 'waterfall' ? { ...d, from: total - d.net, to: total, total } : { ...d, from: 0, to: d.net, total }
  })
  const avg = mode === 'monthly' ? trailingAvg(data.map((d) => d.net)) : []
  const ticks = niceTicks(Math.min(0, ...bars.map((b) => Math.min(b.from, b.to))), Math.max(0, ...bars.map((b) => Math.max(b.from, b.to))))
  const lo = ticks[0]
  const hi = ticks[ticks.length - 1]
  const y = (v: number) => base - ((v - lo) / (hi - lo || 1)) * (base - top)
  const slot = (w - left) / Math.max(1, data.length)
  const bar = Math.max(1, Math.min(22, slot - 2))
  const cx = (i: number) => left + i * slot + slot / 2
  const label = monthLabeler(data.map((d) => d.month))
  return (
    <svg viewBox={`0 0 ${w} ${height}`} width="100%" style={{ display: 'block' }}>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={left - 4} y1={y(t)} x2={w} y2={y(t)} stroke="var(--color-divider)"
            strokeDasharray={t === 0 ? undefined : '2 4'} />
          <text x="0" y={y(t) + 3} fontSize="10" fill="var(--color-neutral-600)" fontFamily="Barlow">{signed(t)}</text>
        </g>
      ))}
      {bars.map((b, i) => {
        const tick = label(i)
        const y1 = Math.min(y(b.from), y(b.to))
        return (
          <g key={b.month} style={{ cursor: onPick ? 'pointer' : undefined }} onClick={() => onPick?.(b.month)}
            {...(tip ? tipHandlers(t, () => tip(b.month, { net: b.net, running: b.total, avg: avg[i] })) : {})}>
            {!tip && <title>{`${b.month.slice(0, 7)} · net ${signed(b.net)}${mode === 'waterfall' ? ` · running ${signed(b.total)}` : ` · 12-mo avg ${signed(avg[i])}`}`}</title>}
            <rect x={cx(i) - slot / 2} y={0} width={slot} height={base} fill="transparent" />
            <rect x={cx(i) - bar / 2} y={y1} width={bar} height={Math.max(1, Math.abs(y(b.from) - y(b.to)))}
              fill={b.net >= 0 ? NET_UP : NET_DOWN} />
            {mode === 'waterfall' && i < bars.length - 1 && slot >= 6 && (
              <line x1={cx(i) + bar / 2} y1={y(b.to)} x2={cx(i + 1) - bar / 2} y2={y(b.to)} stroke="var(--color-neutral-400)" />
            )}
            {tick && (
              <text x={cx(i)} y={height - 4} fontSize="10" textAnchor="middle" fill="var(--color-neutral-600)" fontFamily="Barlow">{tick}</text>
            )}
          </g>
        )
      })}
      {mode === 'monthly' && avg.length > 1 && (
        <polyline fill="none" stroke="var(--color-text)" strokeWidth="1.5" pointerEvents="none"
          points={avg.map((v, i) => `${cx(i)},${y(v)}`).join(' ')} />
      )}
    </svg>
  )
}

export function Legend({ items }: { items: { label: string; color: string; dashed?: boolean }[] }) {
  return (
    <div className="row small muted-2" style={{ gap: 'var(--space-3)' }}>
      {items.map((it) => (
        <span key={it.label} className="row" style={{ gap: 5, flexWrap: 'nowrap' }}>
          {it.dashed
            ? <span style={{ width: 14, borderTop: `2px dashed ${it.color}`, display: 'inline-block' }} />
            : <span style={{ width: 10, height: 10, background: it.color, display: 'inline-block' }} />}
          {it.label}
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
export function MonthlyBars({ data, budget, highlight, onPick, height = 200, width = 960, tip }: {
  data: { month: string; value: number }[]; budget?: number | null; highlight?: string | null
  onPick?: (month: string) => void; height?: number; width?: number; tip?: (month: string, value: number) => TipSpec | null
}) {
  const t = useTip()
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
          <g key={d.month} style={{ cursor: onPick ? 'pointer' : undefined }} onClick={() => onPick?.(d.month)}
            {...(tip ? tipHandlers(t, () => tip(d.month, d.value)) : {})}>
            {!tip && <title>{`${d.month.slice(0, 7)} · ${compact(d.value)}`}</title>}
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

/** Small multiples: one bar chart per series on a shared month axis, each with its own scale and average line. */
export function SeriesHistory({ months, series, label, highlight, onPick, tip }: {
  months: string[]; series: { name: string; values: number[] }[]; label: (si: number, avg: number) => ReactNode
  highlight?: string | null; onPick?: (month: string) => void; tip?: (i: number, si: number, avg: number) => TipSpec | null
}) {
  const t = useTip()
  const w = 800
  const h = 64
  const top = 12
  const base = h - 2
  const left = 46
  const slot = (w - left) / Math.max(1, months.length)
  const bar = Math.max(1, Math.min(18, slot - 2))
  const cx = (i: number) => left + i * slot + slot / 2
  const tick = monthLabeler(months)
  const text = { fontSize: 10, fill: 'var(--color-neutral-600)', fontFamily: 'Barlow' }
  return (
    <div>
      {series.map((s, si) => {
        const max = Math.max(0, ...s.values)
        const avg = s.values.reduce((a, v) => a + v, 0) / Math.max(1, s.values.length)
        const hi = niceTicks(0, Math.max(1, max), 2).at(-1) ?? 1
        const y = (v: number) => base - (Math.max(0, v) / hi) * (base - top)
        const peak = s.values.indexOf(max)
        return (
          <div key={s.name} className="mh-row">
            <div className="mh-label">{label(si, avg)}</div>
            <svg viewBox={`0 0 ${w} ${h}`} width="100%" style={{ display: 'block' }}>
              <line x1={left - 4} y1={base} x2={w} y2={base} stroke="var(--color-divider)" />
              <line x1={left - 4} y1={y(hi)} x2={w} y2={y(hi)} stroke="var(--color-divider)" strokeDasharray="2 4" />
              {Math.abs(y(hi) - y(avg)) > 12 && <text x="0" y={y(hi) + 3} {...text}>{compact(hi)}</text>}
              {Math.abs(base - y(avg)) > 14 && <text x="0" y={base} {...text}>$0</text>}
              {s.values.map((v, i) => (
                <g key={months[i]} style={{ cursor: onPick ? 'pointer' : undefined }} onClick={() => onPick?.(months[i])}
                  {...(tip ? tipHandlers(t, () => tip(i, si, avg)) : {})}>
                  {!tip && <title>{`${months[i].slice(0, 7)} · ${s.name} · ${compact(v)}`}</title>}
                  <rect x={cx(i) - slot / 2} y={0} width={slot} height={base}
                    fill={highlight === months[i] ? 'color-mix(in srgb, var(--color-accent) 14%, transparent)' : 'transparent'} />
                  {v > 0 && <rect x={cx(i) - bar / 2} y={y(v)} width={bar} height={base - y(v)} fill="var(--color-accent-600)" />}
                </g>
              ))}
              {avg > 0 && (
                <g pointerEvents="none">
                  <line x1={left - 4} y1={y(avg)} x2={w} y2={y(avg)} stroke="var(--color-text)" strokeDasharray="4 3" />
                  <text x="0" y={y(avg) + 3} {...text} fill="var(--color-text)">{compact(avg)}</text>
                </g>
              )}
              {max > 0 && (
                <text x={cx(peak)} y={y(max) - 2} {...text} textAnchor="middle" fill="var(--color-text)" pointerEvents="none">
                  {compact(max)}
                </text>
              )}
            </svg>
          </div>
        )
      })}
      <div className="mh-row">
        <div className="hide-sm" />
        <svg viewBox={`0 0 ${w} 16`} width="100%" style={{ display: 'block' }}>
          {months.map((m, i) => {
            const l = tick(i)
            return l && <text key={m} x={cx(i)} y={12} {...text} textAnchor="middle">{l}</text>
          })}
        </svg>
      </div>
    </div>
  )
}

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
