import type { ReactNode } from 'react'
import { money } from '../format'
import { Card } from './ui'

export type Side = {
  kicker: string
  description: string | null
  amount: string | null
  facts: [string, string][]
}

export function PairReview({
  kicker, title, left, right, confidence, reason, actions, note,
}: {
  kicker: string; title: string; left: Side; right: Side; confidence: string; reason: string
  actions: ReactNode; note?: string
}) {
  return (
    <div className="stack-3" style={{ maxWidth: 920 }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div>
          <div className="card-kicker">{kicker}</div>
          <h3 style={{ margin: 0 }}>{title}</h3>
        </div>
        <span className="tag tag-accent">{confidence}</span>
      </div>
      <div className="grid-2">
        {[left, right].map((s, i) => (
          <Card key={i} style={{ gap: 'var(--space-2)' }}>
            <div className="card-kicker">{s.kicker}</div>
            <div className="card-title">{s.description ?? '—'}</div>
            <div className="big-amount" style={{ fontSize: 28 }}>{money(s.amount)}</div>
            <div className="facts">
              {s.facts.map(([k, v]) => (
                <span key={k} style={{ display: 'contents' }}>
                  <span className="text-muted">{k}</span><span>{v}</span>
                </span>
              ))}
            </div>
          </Card>
        ))}
      </div>
      <div className="callout">{reason}</div>
      <div className="row">
        {actions}
        {note && <span className="text-muted small" style={{ marginLeft: 'auto' }}>{note}</span>}
      </div>
    </div>
  )
}
