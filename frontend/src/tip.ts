import { createContext, useContext, type MouseEvent } from 'react'
import type { ContribParams } from './api'

/** What a chart shows when hovering an aggregate; `contrib` loads the transactions behind it. */
export interface TipSpec {
  title: string
  lines?: string[]
  contrib?: ContribParams
  show?: 'merchants' | 'transactions'
}

export interface TipApi {
  show: (spec: TipSpec, e: { clientX: number; clientY: number }) => void
  hide: () => void
}

export const TipContext = createContext<TipApi>({ show: () => {}, hide: () => {} })
export const useTip = () => useContext(TipContext)

/** Hover handlers for an SVG/HTML element; `make` runs lazily so charts don't build specs for every bar on render. */
export function tipHandlers(tip: TipApi, make: () => TipSpec | null) {
  return {
    onMouseMove: (e: MouseEvent) => { const s = make(); if (s) tip.show(s, e); else tip.hide() },
    onMouseLeave: () => tip.hide(),
  }
}
