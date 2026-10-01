type Point = { label: string; value: number | null; title?: string }

/** Minimal blueprint-style line chart (usage over statement periods), matching the design prototype. */
export function LineChart({ points, height = 180, width = 640, fit = false, format = (v: number) => v.toLocaleString() }: {
  points: Point[]; height?: number; width?: number; fit?: boolean; format?: (v: number) => string
}) {
  const vals = points.map((p) => p.value).filter((v): v is number => v !== null)
  if (vals.length === 0) return <div className="text-muted small">No usage values yet.</div>
  const w = width
  const base = height - 20
  const hi = Math.max(...vals)
  const lo = fit ? Math.min(...vals) : 0
  const pad = fit ? (hi - lo) * 0.1 || Math.abs(hi) * 0.05 || 1 : 0
  const max = fit ? hi + pad : hi * 1.15 || 1
  const min = fit ? lo - pad : 0
  const avg = vals.reduce((a, b) => a + b, 0) / vals.length
  const step = points.length > 1 ? (w - 40) / (points.length - 1) : 0
  const x = (i: number) => 20 + i * step
  const y = (v: number) => base - ((v - min) / (max - min)) * (base - 20)
  const dots = points.map((p, i) => ({ ...p, x: x(i), y: p.value === null ? null : y(p.value) }))
  const line = dots.filter((d) => d.y !== null).map((d) => `${d.x},${d.y}`).join(' ')
  const every = Math.ceil(points.length / 12)
  return (
    <svg viewBox={`0 0 ${w} ${height}`} width="100%" style={{ display: 'block' }}>
      <line x1="0" y1={base} x2={w} y2={base} stroke="var(--color-divider)" />
      <line x1="0" y1={y(avg)} x2={w} y2={y(avg)} stroke="var(--color-accent-500)" strokeDasharray="3 4" />
      <text x={w - 4} y={y(avg) - 4} fontSize="10" textAnchor="end" fill="var(--color-neutral-600)" fontFamily="Barlow">
        avg {format(avg)}
      </text>
      <polyline points={line} fill="none" stroke="var(--color-accent-700)" strokeWidth="1.5" />
      {dots.map((d, i) => (
        <g key={i}>
          {d.y !== null && (
            <circle cx={d.x} cy={d.y} r="3" fill="var(--color-bg)" stroke="var(--color-accent-700)" strokeWidth="1.5">
              <title>{d.title ?? `${d.label}: ${format(d.value!)}`}</title>
            </circle>
          )}
          {i % every === 0 && (
            <text x={d.x} y={height - 4} fontSize="10" textAnchor="middle" fill="var(--color-neutral-600)" fontFamily="Barlow">
              {d.label}
            </text>
          )}
        </g>
      ))}
    </svg>
  )
}
