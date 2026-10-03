import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ANOMALY_LABEL, post, type Anomaly } from '../api'
import { money } from '../format'
import { categoryPath, txnPath } from '../links'
import { Button } from './ui'

export function AnomalyList({ items, limit }: { items: Anomaly[]; limit?: number }) {
  const qc = useQueryClient()
  const act = useMutation({
    mutationFn: ({ id, action }: { id: number; action: string }) => post(`/anomalies/${id}/${action}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['anomalies'] }),
  })
  const shown = limit ? items.slice(0, limit) : items
  return (
    <div className="stack-3">
      {shown.map((a) => {
        const link = a.transaction_id
          ? txnPath(a.transaction_id)
          : a.category_id ? categoryPath(a.category_id) : a.series_id ? `/bills?series=${a.series_id}` : '/transactions'
        return (
          <div key={a.id} style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: '2px 12px', paddingBottom: 'var(--space-3)',
            borderBottom: '1px solid var(--color-divider)' }}>
            <div style={{ fontSize: 14, fontWeight: 500 }}>{a.title}</div>
            <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 600, fontSize: 16, textAlign: 'right' }}>{money(a.amount)}</div>
            <div className="small muted-2" style={{ gridColumn: '1 / -1' }}>{a.detail}</div>
            {a.ai_note && <div className="small" style={{ gridColumn: '1 / -1', color: 'var(--color-accent-800)' }}>{a.ai_note}</div>}
            <div className="row" style={{ gridColumn: '1 / -1', gap: 6, marginTop: 4 }}>
              <span className="tag tag-accent">{ANOMALY_LABEL[a.kind] ?? a.kind}</span>
              <Link to={link} className="btn btn-ghost" style={{ fontSize: 12, padding: '2px 6px' }}>Review</Link>
              {a.status === 'withdrawn' ? (
                <span className="tag tag-neutral" title="A later check found this no longer holds">Withdrawn</span>
              ) : a.status === 'open' ? (
                <>
                  <Button variant="ghost" style={{ fontSize: 12, padding: '2px 6px' }} disabled={act.isPending}
                    onClick={() => act.mutate({ id: a.id, action: 'review' })}>Mark reviewed</Button>
                  <Button variant="ghost" style={{ fontSize: 12, padding: '2px 6px' }} disabled={act.isPending}
                    onClick={() => act.mutate({ id: a.id, action: 'dismiss' })}>Dismiss</Button>
                </>
              ) : (
                <Button variant="ghost" style={{ fontSize: 12, padding: '2px 6px' }} onClick={() => act.mutate({ id: a.id, action: 'reopen' })}>
                  Reopen
                </Button>
              )}
            </div>
          </div>
        )
      })}
      {items.length === 0 && <div className="small text-muted">Nothing out of the ordinary.</div>}
    </div>
  )
}
