import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, post, type Person } from '../api'
import { Button, ErrorNote, Field } from './ui'

/** Email household members a question about one or more transactions; replies come back by email. */
export function AskPanel({ ids, onSent, onClose }: { ids: number[]; onSent: (msg: string) => void; onClose: () => void }) {
  const qc = useQueryClient()
  const people = useQuery({ queryKey: ['alerts', 'people'], queryFn: () => get<Person[]>('/alerts/people') })
  const [to, setTo] = useState<string[]>([])
  const [replyTo, setReplyTo] = useState('')
  const [subject, setSubject] = useState('')
  const [message, setMessage] = useState('')
  const label = (p: Person) => p.name ?? p.email
  const send = useMutation({
    mutationFn: () => post<{ sent_to: string[] }>('/alerts/questions', {
      transaction_ids: ids, to, message, subject: subject.trim() || null, reply_to: replyTo || null,
    }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['transactions', 'notes'] })
      qc.invalidateQueries({ queryKey: ['alerts', 'events'] })
      const names = r.sent_to.map((e) => label(people.data?.find((p) => p.email === e) ?? { email: e, name: null }))
      onSent(`Question sent to ${names.join(', ')}. It was also saved as a note on ${ids.length === 1 ? 'the transaction' : `all ${ids.length} transactions`}.`)
    },
  })
  const list = people.data ?? []

  return (
    <div className="callout stack" style={{ color: 'inherit' }}>
      <div className="row" style={{ flexWrap: 'nowrap', alignItems: 'flex-start' }}>
        <strong style={{ flex: 1 }}>Ask about {ids.length === 1 ? 'this transaction' : `${ids.length} transactions`}</strong>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">×</button>
      </div>
      {people.isSuccess && list.length === 0 ? (
        <div className="small">
          No one to ask yet. Add an email to a household member in <Link to="/settings?tab=members">Settings</Link> or
          add an <Link to="/alerts">alert recipient</Link>.
        </div>
      ) : (
        <>
          <Field label="Send to">
            <div className="row" style={{ gap: 6 }}>
              {list.map((p) => {
                const on = to.includes(p.email)
                return (
                  <button key={p.email} type="button" className={`tag chip${on ? ' on' : ''}`} title={p.email}
                    onClick={() => setTo(on ? to.filter((e) => e !== p.email) : [...to, p.email])}>
                    {label(p)}
                  </button>
                )
              })}
            </div>
          </Field>
          <Field label="Question">
            <textarea className="input" style={{ minHeight: 72 }} maxLength={4000} value={message} autoFocus
              placeholder="What was this for?" onChange={(e) => setMessage(e.target.value)} />
          </Field>
          <div className="grid-form">
            <Field label="Subject (optional)">
              <input className="input" maxLength={200} value={subject} onChange={(e) => setSubject(e.target.value)} />
            </Field>
            <Field label="Replies go to">
              <select className="input" value={replyTo} onChange={(e) => setReplyTo(e.target.value)}>
                <option value="">The app’s email address</option>
                {list.map((p) => <option key={p.email} value={p.email}>{label(p)}</option>)}
              </select>
            </Field>
          </div>
          <ErrorNote error={people.error || send.error} />
          <div className="row">
            <Button variant="primary" disabled={!to.length || !message.trim() || send.isPending} onClick={() => send.mutate()}>
              {send.isPending ? 'Sending…' : 'Send question'}
            </Button>
            <Button variant="ghost" onClick={onClose}>Cancel</Button>
          </div>
        </>
      )}
    </div>
  )
}
