import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation } from 'react-router-dom'
import { get, type ContribParams, type Contributors } from '../api'
import { money, shortDate } from '../format'
import { TipContext, type TipApi, type TipSpec } from '../tip'

type TipState = { spec: TipSpec; x: number; y: number; path: string }

function createStore() {
  let state: TipState | null = null
  const subs = new Set<() => void>()
  return {
    get: () => state,
    set: (s: TipState | null) => { state = s; subs.forEach((f) => f()) },
    subscribe: (f: () => void) => { subs.add(f); return () => { subs.delete(f) } },
  }
}
type Store = ReturnType<typeof createStore>

export function TipProvider({ children }: { children: ReactNode }) {
  const { pathname } = useLocation()
  const [store] = useState(createStore)
  const path = useRef(pathname)
  useEffect(() => { path.current = pathname }, [pathname])
  const api = useMemo<TipApi>(() => ({
    show: (spec, e) => store.set({ spec, x: e.clientX, y: e.clientY, path: path.current }),
    hide: () => store.set(null),
  }), [store])
  useEffect(() => {
    const hide = () => store.set(null)
    window.addEventListener('scroll', hide, true)
    window.addEventListener('mousedown', hide)
    return () => {
      window.removeEventListener('scroll', hide, true)
      window.removeEventListener('mousedown', hide)
    }
  }, [store])
  return (
    <TipContext.Provider value={api}>
      {children}
      <TipLayer store={store} pathname={pathname} />
    </TipContext.Provider>
  )
}

function TipLayer({ store, pathname }: { store: Store; pathname: string }) {
  const t = useSyncExternalStore(store.subscribe, store.get)
  // A click that navigates unmounts the hovered element without a mouseleave.
  if (!t || t.path !== pathname) return null
  const right = t.x > window.innerWidth / 2
  const below = t.y < window.innerHeight / 2
  const style = {
    ...(right ? { right: window.innerWidth - t.x + 14 } : { left: t.x + 14 }),
    ...(below ? { top: t.y + 14 } : { bottom: window.innerHeight - t.y + 14 }),
  }
  return (
    <div className="tip" style={style} role="tooltip">
      <div className="tip-title">{t.spec.title}</div>
      {t.spec.lines?.map((l) => <div key={l} className="muted-2">{l}</div>)}
      {t.spec.contrib && <Contrib key={JSON.stringify(t.spec.contrib)} params={t.spec.contrib} show={t.spec.show ?? 'merchants'} />}
    </div>
  )
}

function Contrib({ params, show }: { params: ContribParams; show: 'merchants' | 'transactions' }) {
  // Only fetch once the pointer rests on an aggregate, not while sweeping across a chart.
  const [ready, setReady] = useState(false)
  useEffect(() => { const h = setTimeout(() => setReady(true), 250); return () => clearTimeout(h) }, [])
  const q = useQuery({
    queryKey: ['contributors', params],
    queryFn: () => get<Contributors>('/analytics/contributors', params),
    enabled: ready,
    staleTime: 300_000,
  })
  const d = q.data
  if (!d) return <div className="tip-body muted-2">{q.error ? 'Could not load transactions.' : 'Loading top transactions…'}</div>
  if (d.count === 0) return <div className="tip-body muted-2">No transactions.</div>
  const shown = show === 'merchants' ? d.merchants.reduce((a, m) => a + m.count, 0) : d.transactions.length
  return (
    <div className="tip-body">
      {show === 'merchants' ? (
        <table>
          <thead><tr><th>Merchant</th><th className="num">#</th><th className="num">Out</th><th className="num">In</th></tr></thead>
          <tbody>
            {d.merchants.map((m) => (
              <tr key={m.key}>
                <td className="tip-name">{m.name}</td><td className="num">{m.count}</td>
                <td className="num">{m.out ? money(-m.out) : ''}</td><td className="num">{m.in ? money(m.in) : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <table>
          <tbody>
            {d.transactions.map((x) => (
              <tr key={x.id}>
                <td className="nowrap muted-2">{shortDate(x.date)} {x.date.slice(2, 4)}</td>
                <td className="tip-name">{x.description}</td><td className="num">{money(x.amount)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="muted-2">
        {d.count.toLocaleString()} transaction{d.count === 1 ? '' : 's'}
        {d.out ? ` · out ${money(d.out)}` : ''}{d.in ? ` · in ${money(d.in)}` : ''}
        {d.count > shown ? ` · top ${show === 'merchants' ? d.merchants.length + ' merchants' : shown} shown` : ''}
      </div>
    </div>
  )
}
