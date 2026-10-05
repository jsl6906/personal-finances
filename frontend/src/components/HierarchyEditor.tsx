import { Fragment, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { del, post, put } from '../api'
import { useUrl, useUrlText } from '../urlState'
import { Editor, type Column } from './CrudTable'
import { TableCard } from './Detail'
import { Button, ErrorNote, Field } from './ui'

type Row = { id: number } & Record<string, unknown>

export type Level = {
  /** Singular noun used in buttons ("Account", "Group"). */
  label: string
  path: string
  queryKey: string
  /** Editable fields. The first column is treated as the name; bool columns collapse into one "flags" cell. */
  columns: Column[]
  defaults: Record<string, unknown>
  detail?: (id: number) => string
  /** Tags shown per row; defaults to the labels of truthy bool columns. */
  badges?: (row: Row) => string[]
}

type Props = {
  parent: Level
  child: Level
  /** Field on child rows holding the parent id. */
  parentKey: string
  parents: Row[]
  items: Row[]
  /** Heading for children whose parent is null; omitted = parent required. */
  orphanLabel?: string
  /** Children matching `test` are greyed out and hidden until the toggle is on; `noun` names them in summaries. */
  dim?: { label: string; noun: string; test: (row: Row) => boolean }
  summary?: (children: Row[]) => string
  error?: unknown
}

type Edit = { level: 'parent' | 'child'; id: number | 'new' }

const ORPHAN = -1

function cellText(row: Row, col: Column): string {
  const v = row[col.key]
  if (v === null || v === undefined || v === '') return ''
  if (col.kind === 'select') return col.options?.find((o) => o.value === v)?.label ?? String(v)
  return String(v)
}

export function HierarchyEditor({ parent, child, parentKey, parents, items, orphanLabel, dim, summary, error }: Props) {
  const qc = useQueryClient()
  const [edit, setEdit] = useState<Edit | null>(null)
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set())
  const [params, set] = useUrl()
  const showDim = params.get('all') === '1'
  const setShowDim = (v: boolean) => set({ all: v })
  const [filter, setFilter] = useUrlText('q')

  const childParentCol: Column = {
    key: parentKey, label: parent.label, kind: 'select', required: !orphanLabel,
    options: parents.map((p) => ({ value: p.id, label: String(p[parent.columns[0].key]) })),
  }
  const childCols = [childParentCol, ...child.columns]
  const nameCol = child.columns[0]
  const textCols = child.columns.slice(1).filter((c) => c.kind !== 'bool')
  const flagCols = child.columns.filter((c) => c.kind === 'bool')
  const parentFlagCols = parent.columns.filter((c) => c.kind === 'bool')
  const colSpan = textCols.length + 3

  const done = () => {
    qc.invalidateQueries({ queryKey: [parent.queryKey] })
    qc.invalidateQueries({ queryKey: [child.queryKey] })
    setEdit(null)
  }
  const save = useMutation({
    mutationFn: () => {
      const lvl = edit!.level === 'parent' ? parent : child
      const keys = edit!.level === 'parent' ? Object.keys(parent.defaults) : [parentKey, ...Object.keys(child.defaults)]
      const body = Object.fromEntries(keys.map((k) => [k, draft[k] ?? lvl.defaults[k] ?? null]))
      return edit!.id === 'new' ? post(lvl.path, body) : put(`${lvl.path}/${edit!.id}`, body)
    },
    onSuccess: done,
  })
  const remove = useMutation({
    mutationFn: ({ level, id }: Edit) => del(`${level === 'parent' ? parent.path : child.path}/${id}`),
    onSuccess: done,
  })
  const start = (level: 'parent' | 'child', row: Row | null, parentId?: number | null) => {
    save.reset()
    remove.reset()
    setEdit({ level, id: row ? row.id : 'new' })
    const lvl = level === 'parent' ? parent : child
    setDraft(row ? { ...row } : { ...lvl.defaults, ...(level === 'child' ? { [parentKey]: parentId ?? null } : {}) })
  }
  const editing = (level: 'parent' | 'child', id: number | 'new') => edit?.level === level && edit.id === id

  const q = filter.trim().toLowerCase()
  const matches = (row: Row, cols: Column[]) => !q || cols.some((c) => cellText(row, c).toLowerCase().includes(q))

  const byParent = useMemo(() => {
    const m = new Map<number, Row[]>()
    for (const c of items) {
      const pid = (c[parentKey] as number | null) ?? ORPHAN
      if (!m.has(pid)) m.set(pid, [])
      m.get(pid)!.push(c)
    }
    return m
  }, [items, parentKey])

  const dimCount = dim ? items.filter(dim.test).length : 0
  const sections: { id: number; row: Row | null; name: string }[] = parents.map((p) => ({ id: p.id, row: p, name: String(p[parent.columns[0].key]) }))
  const addingOrphan = editing('child', 'new') && (draft[parentKey] ?? null) === null
  if (orphanLabel && (byParent.has(ORPHAN) || addingOrphan)) sections.push({ id: ORPHAN, row: null, name: orphanLabel })

  const toggle = (id: number) => setCollapsed((s) => {
    const n = new Set(s)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    return n
  })

  const formRow = (key: string, cols: Column[], required: Column[]) => {
    const valid = required.every((c) => draft[c.key] !== null && draft[c.key] !== undefined && draft[c.key] !== '')
    return (
      <tr key={key} className="selected">
        <td colSpan={colSpan}>
          <form className="row tree-form" onSubmit={(e) => { e.preventDefault(); if (valid) save.mutate() }}>
            {cols.map((c) => (
              <Field key={c.key} label={c.label}>
                <Editor col={c} value={draft[c.key]} onChange={(v) => setDraft((d) => ({ ...d, [c.key]: v }))} />
              </Field>
            ))}
            <div className="row" style={{ flexWrap: 'nowrap', alignSelf: 'flex-end' }}>
              <Button variant="primary" type="submit" disabled={!valid || save.isPending}>Save</Button>
              <Button variant="ghost" type="button" onClick={() => setEdit(null)}>Cancel</Button>
            </div>
          </form>
        </td>
      </tr>
    )
  }

  const flags = (row: Row, lvl: Level, cols: Column[]): ReactNode =>
    (lvl.badges ? lvl.badges(row) : cols.filter((c) => row[c.key]).map((c) => c.label)).map((label) => (
      <span key={label} className="tag tag-neutral">{label}</span>
    ))

  const childRow = (k: Row) => {
    if (editing('child', k.id)) return formRow(`c-${k.id}`, childCols, childCols.filter((c) => c.required))
    const name = cellText(k, nameCol)
    return (
      <tr key={k.id} className={dim?.test(k) ? 'tree-dim' : undefined}>
        <td className="tree-child">{child.detail ? <Link to={child.detail(k.id)}>{name}</Link> : name}</td>
        {textCols.map((c) => <td key={c.key} className={c.kind === 'number' ? 'num' : undefined}>{cellText(k, c)}</td>)}
        <td><span className="row" style={{ gap: 4, flexWrap: 'nowrap' }}>{flags(k, child, flagCols)}</span></td>
        <td className="nowrap" style={{ textAlign: 'right' }}>
          <Button variant="ghost" onClick={() => start('child', k)}>Edit</Button>
          <Button variant="ghost" onClick={() => confirm(`Delete ${child.label.toLowerCase()} "${name}"?`) && remove.mutate({ level: 'child', id: k.id })}>Delete</Button>
        </td>
      </tr>
    )
  }

  const head = (
    <div className="row" style={{ padding: 'var(--space-2) var(--space-3)', justifyContent: 'space-between' }}>
      <div className="row">
        <input className="input compact" placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {dim && dimCount > 0 && (
          <label className="row small" style={{ gap: 6, cursor: 'pointer' }}>
            <input type="checkbox" checked={showDim} onChange={(e) => setShowDim(e.target.checked)} />
            {dim.label} ({dimCount})
          </label>
        )}
        <button type="button" className="link-btn small" onClick={() => setCollapsed(new Set(sections.map((s) => s.id)))}>Collapse all</button>
        <button type="button" className="link-btn small" onClick={() => setCollapsed(new Set())}>Expand all</button>
      </div>
      <Button onClick={() => start('parent', null)} disabled={editing('parent', 'new')}>Add {parent.label.toLowerCase()}</Button>
    </div>
  )

  return (
    <TableCard head={head}>
      <div style={{ padding: '0 var(--space-3)' }}><ErrorNote error={save.error || remove.error || error} /></div>
      <table className="table dense tree">
        <thead>
          <tr>
            <th>{parent.label} › {nameCol.label}</th>
            {textCols.map((c) => <th key={c.key} style={c.width ? { width: c.width } : undefined}>{c.label}</th>)}
            <th />
            <th />
          </tr>
        </thead>
        <tbody>
          {editing('parent', 'new') && formRow('new-parent', parent.columns, parent.columns.filter((c) => c.required))}
          {sections.map((sec) => {
            const kids = byParent.get(sec.id) ?? []
            const visible = kids.filter((k) => (showDim || !dim || !dim.test(k)) && matches(k, childCols))
            const hiddenDim = dim ? kids.filter(dim.test).length : 0
            const selfMatch = sec.row ? matches(sec.row, parent.columns) : false
            if (q && !selfMatch && visible.length === 0) return null
            const isCollapsed = collapsed.has(sec.id) && !q
            if (sec.row && editing('parent', sec.id)) {
              return (
                <Fragment key={sec.id}>
                  {formRow(`p-${sec.id}`, parent.columns, parent.columns.filter((c) => c.required))}
                  {!isCollapsed && visible.map((k) => childRow(k))}
                </Fragment>
              )
            }
            return (
              <Fragment key={sec.id}>
                <tr className="tree-parent">
                  <td colSpan={textCols.length + 2}>
                    <div className="row" style={{ flexWrap: 'nowrap', gap: 8 }}>
                      <button type="button" className="link-btn tree-toggle" onClick={() => toggle(sec.id)} aria-label={isCollapsed ? 'Expand' : 'Collapse'}>
                        {isCollapsed ? '▸' : '▾'}
                      </button>
                      <span className="tree-name">
                        {sec.row && parent.detail ? <Link to={parent.detail(sec.id)}>{sec.name}</Link> : sec.name}
                      </span>
                      {sec.row && parent.columns.slice(1).filter((c) => c.kind !== 'bool').map((c) => {
                        const t = cellText(sec.row!, c)
                        return t ? <span key={c.key} className="small text-muted nowrap">{c.kind === 'number' ? `${c.label} ${t}` : t}</span> : null
                      })}
                      {sec.row && flags(sec.row, parent, parentFlagCols)}
                      <span className="small text-muted nowrap">
                        {summary ? summary(kids) : `${kids.length} ${kids.length === 1 ? child.label.toLowerCase() : `${child.label.toLowerCase()}s`}`}
                        {!showDim && hiddenDim > 0 && ` · ${hiddenDim} ${dim!.noun}`}
                      </span>
                    </div>
                  </td>
                  <td className="nowrap" style={{ textAlign: 'right' }}>
                    <Button variant="ghost" onClick={() => start('child', null, sec.id === ORPHAN ? null : sec.id)}
                      disabled={editing('child', 'new')}>+ {child.label}</Button>
                    {sec.row && (
                      <>
                        <Button variant="ghost" onClick={() => start('parent', sec.row)}>Edit</Button>
                        <Button variant="ghost" disabled={kids.length > 0} title={kids.length ? `Move or delete its ${child.label.toLowerCase()}s first` : undefined}
                          onClick={() => confirm(`Delete ${parent.label.toLowerCase()} "${sec.name}"?`) && remove.mutate({ level: 'parent', id: sec.id })}>Delete</Button>
                      </>
                    )}
                  </td>
                </tr>
                {editing('child', 'new') && ((draft[parentKey] as number | null) ?? ORPHAN) === sec.id && formRow('new-child', childCols, childCols.filter((c) => c.required))}
                {!isCollapsed && visible.map((k) => childRow(k))}
              </Fragment>
            )
          })}
          {items.length === 0 && parents.length === 0 && (
            <tr><td colSpan={colSpan} className="text-muted">Nothing here yet.</td></tr>
          )}
        </tbody>
      </table>
    </TableCard>
  )
}
