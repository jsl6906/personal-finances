import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { useSearchParams } from 'react-router-dom'

export type Dir = 'asc' | 'desc'
type Value = string | number | boolean | null | undefined
export type Patch = Record<string, Value>
export type SetUrl = (patch: Patch, opts?: { replace?: boolean; reset?: boolean }) => void

/** Search params plus a setter that merges `patch` into the URL as one history entry (null/''/false delete a key).
 * `reset` starts from empty params; `replace` swaps the current entry instead of pushing. No-op changes are skipped. */
export function useUrl(): [URLSearchParams, SetUrl] {
  const [params, setParams] = useSearchParams()
  const set = useCallback<SetUrl>((patch, opts) => {
    // Read the live URL, not the render-time params, so delayed callers (debounced search) can't drop newer changes.
    const cur = new URLSearchParams(window.location.search)
    const next = opts?.reset ? new URLSearchParams() : new URLSearchParams(cur)
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === undefined || v === '' || v === false) next.delete(k)
      else next.set(k, v === true ? '1' : String(v))
    }
    if (next.toString() !== cur.toString()) setParams(next, { replace: opts?.replace })
  }, [setParams])
  return [params, set]
}

export function numParam(p: URLSearchParams, key: string): number | null {
  const v = p.get(key)
  return v !== null && v !== '' && Number.isFinite(Number(v)) ? Number(v) : null
}

export function oneOf<T extends string>(p: URLSearchParams, key: string, values: readonly T[], dflt: T): T {
  const v = p.get(key) as T | null
  return v !== null && values.includes(v) ? v : dflt
}

/** Text input bound to a URL param: the box updates immediately, the URL after a pause. The first change pushes a
 * history entry and further typing replaces it. Returns [text, setText, committed URL value]. */
export function useUrlText(key: string, resetKeys: string[] = [], ms = 300) {
  const [params, set] = useUrl()
  const url = params.get(key) ?? ''
  const [text, setText] = useState(url)
  const synced = useRef(url)
  const resets = resetKeys.join(',')
  useEffect(() => {
    if (url !== synced.current) {
      synced.current = url
      setText(url)
    }
  }, [url])
  useEffect(() => {
    if (text === synced.current) return
    const id = setTimeout(() => {
      const replace = synced.current !== ''
      synced.current = text
      const patch: Patch = { [key]: text }
      for (const k of resets.split(',').filter(Boolean)) patch[k] = null
      set(patch, { replace })
    }, ms)
    return () => clearTimeout(id)
  }, [text, key, ms, set, resets])
  return [text, setText, url] as const
}

/** Local state that resets to `initial()` whenever `key` changes, e.g. a row selection tied to the URL's filters. */
export function useKeyedState<T>(key: string, initial: () => T): [T, Dispatch<SetStateAction<T>>] {
  const [state, setState] = useState(initial)
  const [prev, setPrev] = useState(key)
  if (prev !== key) {
    setPrev(key)
    setState(initial())
  }
  return [state, setState]
}

export type Sort<K extends string> = { key: K | null; dir: Dir; toggle: (k: K) => void }

function parseSort<K extends string>(raw: string | null, cols: Record<K, Dir>): { key: K; dir: Dir } | null {
  if (!raw) return null
  const i = raw.lastIndexOf('_')
  const key = raw.slice(0, i) as K
  const dir = raw.slice(i + 1)
  return Object.hasOwn(cols, key) && (dir === 'asc' || dir === 'desc') ? { key, dir } : null
}

/** Column sort kept in the URL as `<param>=<column>_<asc|desc>`. `cols` gives each column's first direction. With no
 * default the table has a natural order, and a third click on a column returns to it. */
export function useUrlSort<K extends string>(cols: Record<K, Dir>, dflt: NoInfer<`${K}_${Dir}`> | null = null, param = 'sort',
  resetKeys: string[] = []): Sort<K> {
  const [params, set] = useUrl()
  const cur = parseSort(params.get(param), cols) ?? parseSort(dflt, cols)
  const toggle = (k: K) => {
    const first = cols[k]
    const other: Dir = first === 'asc' ? 'desc' : 'asc'
    let next: string | null = cur?.key !== k ? `${k}_${first}` : cur.dir === first ? `${k}_${other}` : dflt ? `${k}_${first}` : null
    if (next === dflt) next = null
    const patch: Patch = { [param]: next }
    for (const r of resetKeys) patch[r] = null
    set(patch)
  }
  return { key: cur?.key ?? null, dir: cur?.dir ?? 'asc', toggle }
}

type SortVal = string | number | null | undefined
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })

/** Stable client-side sort; blanks always sort last. Unsorted (`key` null) keeps the given order. */
export function sortRows<T, K extends string>(rows: readonly T[], s: { key: K | null; dir: Dir },
  by: Record<K, (row: T) => SortVal>): T[] {
  if (!s.key) return [...rows]
  const get = by[s.key]
  const sign = s.dir === 'asc' ? 1 : -1
  const blank = (v: SortVal) => v === null || v === undefined || v === '' || (typeof v === 'number' && Number.isNaN(v))
  return rows
    .map((row, i) => ({ row, i, v: get(row) }))
    .sort((a, b) => {
      const ab = blank(a.v)
      const bb = blank(b.v)
      if (ab || bb) return ab && bb ? a.i - b.i : ab ? 1 : -1
      const c = typeof a.v === 'number' && typeof b.v === 'number' ? a.v - b.v : collator.compare(String(a.v), String(b.v))
      return c * sign || a.i - b.i
    })
    .map((e) => e.row)
}
