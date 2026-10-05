import { useState, type ReactNode, type CSSProperties, type InputHTMLAttributes } from 'react'
import type { Sort } from '../urlState'

export function Corners() {
  return (
    <>
      <i className="corner tl" />
      <i className="corner tr" />
      <i className="corner bl" />
      <i className="corner br" />
    </>
  )
}

type CardProps = { children: ReactNode; className?: string; style?: CSSProperties; as?: 'div' | 'aside' | 'section' }

export function Card({ children, className = '', style, as: Tag = 'div' }: CardProps) {
  return (
    <Tag className={`card blueprint ${className}`} style={style}>
      <Corners />
      {children}
    </Tag>
  )
}

/** Full-bleed table card; the scroll wrapper sits inside so the blueprint corners (at -6px) don't trigger scrollbars. */
export function TableCard({ children, head, foot }: { children: ReactNode; head?: ReactNode; foot?: ReactNode }) {
  return (
    <Card className="table-card">
      {head}
      <div className="table-scroll">{children}</div>
      {foot}
    </Card>
  )
}

export function Swatch({ color }: { color: string }) {
  return <span className="swatch" style={{ background: color }} />
}

type BtnProps = React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'ghost' }

export function Button({ variant = 'secondary', className = '', children, ...rest }: BtnProps) {
  const blueprint = variant === 'primary' ? ' blueprint' : ''
  return (
    <button className={`btn btn-${variant}${blueprint} ${className}`} {...rest}>
      {variant === 'primary' && <Corners />}
      {children}
    </button>
  )
}

export function Seg<T extends string>({
  name, value, options, onChange,
}: { name: string; value: T; options: { value: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="seg">
      {options.map((o) => (
        <label key={o.value} className="seg-opt">
          <input type="radio" name={name} checked={value === o.value} onChange={() => onChange(o.value)} />
          {o.label}
        </label>
      ))}
    </div>
  )
}

/** Clickable column header for a `useUrlSort` table. */
export function SortTh<K extends string>({ s, k, children, right, className, style, title }: {
  s: Sort<K>; k: NoInfer<K>; children: ReactNode; right?: boolean; className?: string; style?: CSSProperties; title?: string
}) {
  const active = s.key === k
  return (
    <th className={className} style={right ? { textAlign: 'right', ...style } : style}
      aria-sort={active ? (s.dir === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button type="button" className={`th-sort${active ? ' active' : ''}`} title={title} onClick={() => s.toggle(k)}>
        {children}<span className="th-sort-arrow">{active ? (s.dir === 'asc' ? '▲' : '▼') : '↕'}</span>
      </button>
    </th>
  )
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
    </div>
  )
}

type DateInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'type'> & {
  value: string
  onChange: (v: string) => void
}

/** Date input that keeps typed text locally and only commits once the year is plausible (or on blur), so partial
 * years like 0002-01-01 don't round-trip through slow state (e.g. the URL) and reset the field mid-typing. */
export function DateInput({ value, onChange, onBlur, ...rest }: DateInputProps) {
  const [draft, setDraft] = useState(value)
  const [prev, setPrev] = useState(value)
  if (prev !== value) {
    setPrev(value)
    setDraft(value)
  }
  return (
    <input {...rest} type="date" value={draft}
      onChange={(e) => {
        const v = e.target.value
        setDraft(v)
        if (v === '' || v >= '1900') onChange(v)
      }}
      onBlur={(e) => {
        if (draft !== value) onChange(draft)
        onBlur?.(e)
      }} />
  )
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null
  return <div className="callout error">{error instanceof Error ? error.message : String(error)}</div>
}

const ICONS: Record<string, string> = {
  dashboard: 'M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z',
  reports: 'M3 3v18h18M7 15l4-4 3 3 5-6',
  findings: 'M4 22V4M4 4h13l-2.5 4.5L17 13H4',
  transactions: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  rules: 'M22 3H2l8 9.46V19l4 2v-8.54L22 3z',
  import: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12',
  bills: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8',
  budgets: 'M12 2a10 10 0 1 0 0 20 10 10 0 1 0 0-20zM12 6a6 6 0 1 0 0 12 6 6 0 1 0 0-12zM12 10a2 2 0 1 0 0 4 2 2 0 1 0 0-4z',
  chat: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z',
  alerts: 'M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0',
  sources:
    'M12 2C7.58 2 4 3.79 4 6s3.58 4 8 4 8-1.79 8-4-3.58-4-8-4zM4 6v6c0 2.21 3.58 4 8 4s8-1.79 8-4V6M4 12v6c0 2.21 3.58 4 8 4s8-1.79 8-4v-6',
  settings: 'M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6',
  file: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6',
  menu: 'M3 6h18M3 12h18M3 18h18',
  close: 'M6 6l12 12M18 6L6 18',
}

export function Icon({ name, size = 16 }: { name: keyof typeof ICONS | string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
      strokeLinecap="round" strokeLinejoin="round">
      <path d={ICONS[name]} />
    </svg>
  )
}

export function ProgressBar({ fraction }: { fraction: number }) {
  return (
    <div className="progress">
      <div style={{ width: `${Math.max(0, Math.min(1, fraction)) * 100}%` }} />
    </div>
  )
}
