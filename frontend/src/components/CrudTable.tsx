import { useState, type ReactNode } from 'react'
import { useMutation, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import { del, post, put } from '../api'
import { Button, Card, ErrorNote, Swatch } from './ui'

export type Column = {
  key: string
  label: string
  kind: 'text' | 'number' | 'bool' | 'select' | 'color'
  options?: { value: string | number; label: string }[]
  required?: boolean
  width?: number
}

type Row = { id: number } & Record<string, unknown>

type Props = {
  title: string
  kicker: string
  path: string
  queryKey: string
  query: UseQueryResult<Row[]>
  columns: Column[]
  defaults: Record<string, unknown>
  display?: (row: Row, key: string) => string | undefined
}

export function Editor({ col, value, onChange }: { col: Column; value: unknown; onChange: (v: unknown) => void }) {
  if (col.kind === 'bool') {
    return <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
  }
  if (col.kind === 'color') {
    return (
      <span className="row" style={{ gap: 8 }}>
        <input type="color" className="color-input" value={typeof value === 'string' ? value : '#7a8a99'}
          onChange={(e) => onChange(e.target.value)} />
        {Boolean(value) && <button type="button" className="link-btn small" onClick={() => onChange(null)}>Clear</button>}
      </span>
    )
  }
  if (col.kind === 'select') {
    return (
      <select className="input" value={value === null || value === undefined ? '' : String(value)}
        onChange={(e) => {
          const raw = e.target.value
          const opt = col.options?.find((o) => String(o.value) === raw)
          onChange(raw === '' ? null : opt ? opt.value : raw)
        }}>
        {!col.required && <option value="">—</option>}
        {col.options?.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    )
  }
  return (
    <input className="input" type={col.kind === 'number' ? 'number' : 'text'} value={value === null || value === undefined ? '' : String(value)}
      onChange={(e) => onChange(col.kind === 'number' ? Number(e.target.value) : e.target.value || null)} />
  )
}

export function CrudTable({ title, kicker, path, queryKey, query, columns, defaults, display }: Props) {
  const qc = useQueryClient()
  const [editId, setEditId] = useState<number | 'new' | null>(null)
  const [draft, setDraft] = useState<Record<string, unknown>>({})

  const done = () => {
    qc.invalidateQueries({ queryKey: [queryKey] })
    setEditId(null)
  }
  const save = useMutation({
    mutationFn: () => {
      const body = Object.fromEntries(Object.keys(defaults).map((k) => [k, draft[k] ?? defaults[k]]))
      return editId === 'new' ? post(path, body) : put(`${path}/${editId}`, body)
    },
    onSuccess: done,
  })
  const remove = useMutation({ mutationFn: (id: number) => del(`${path}/${id}`), onSuccess: done })

  const startEdit = (row: Row | null) => {
    save.reset()
    remove.reset()
    setEditId(row ? row.id : 'new')
    setDraft(row ? { ...row } : { ...defaults })
  }
  const valid = columns.every((c) => !c.required || (draft[c.key] !== null && draft[c.key] !== undefined && draft[c.key] !== ''))

  const show = (row: Row, col: Column): ReactNode => {
    const custom = display?.(row, col.key)
    if (custom !== undefined) return custom
    const v = row[col.key]
    if (col.kind === 'bool') return v ? 'Yes' : ''
    if (col.kind === 'color') {
      return v ? <span className="row" style={{ gap: 8 }}><Swatch color={String(v)} />{String(v)}</span> : '—'
    }
    if (col.kind === 'select') return col.options?.find((o) => o.value === v)?.label ?? (v ? String(v) : '—')
    return v === null || v === undefined ? '' : String(v)
  }

  const editRow = (key: string | number) => (
    <tr key={key} className="selected">
      {columns.map((c) => (
        <td key={c.key} style={c.width ? { width: c.width } : undefined}>
          <Editor col={c} value={draft[c.key]} onChange={(v) => setDraft((d) => ({ ...d, [c.key]: v }))} />
        </td>
      ))}
      <td className="nowrap">
        <Button variant="ghost" onClick={() => save.mutate()} disabled={!valid || save.isPending}>Save</Button>
        <Button variant="ghost" onClick={() => setEditId(null)}>Cancel</Button>
      </td>
    </tr>
  )

  return (
    <Card className="table-card" style={{ overflow: 'visible' }}>
      <div className="row" style={{ justifyContent: 'space-between', padding: 'var(--space-3) var(--space-3) 0' }}>
        <div>
          <div className="card-kicker">{kicker}</div>
          <div className="card-title">{title}</div>
        </div>
        <Button onClick={() => startEdit(null)} disabled={editId === 'new'}>Add</Button>
      </div>
      <div style={{ padding: '0 var(--space-3)' }}><ErrorNote error={save.error || remove.error || query.error} /></div>
      <div style={{ overflowX: 'auto' }}>
      <table className="table">
        <thead>
          <tr>{columns.map((c) => <th key={c.key} style={c.width ? { width: c.width } : undefined}>{c.label}</th>)}<th /></tr>
        </thead>
        <tbody>
          {editId === 'new' && editRow('new')}
          {(query.data ?? []).map((row) =>
            editId === row.id ? (
              editRow(row.id)
            ) : (
              <tr key={row.id}>
                {columns.map((c) => <td key={c.key} className={c.kind === 'bool' ? 'text-muted' : undefined}>{show(row, c)}</td>)}
                <td className="nowrap" style={{ textAlign: 'right' }}>
                  <Button variant="ghost" onClick={() => startEdit(row)}>Edit</Button>
                  <Button variant="ghost" onClick={() => confirm('Delete this record?') && remove.mutate(row.id)}>Delete</Button>
                </td>
              </tr>
            ),
          )}
        </tbody>
      </table>
      </div>
    </Card>
  )
}
