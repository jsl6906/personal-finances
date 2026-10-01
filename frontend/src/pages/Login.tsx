import { useState, type FormEvent } from 'react'
import { post } from '../api'
import { Button, Card, ErrorNote, Field } from '../components/ui'

export function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await post('/auth/login', { password })
      onLogin()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form onSubmit={submit}>
        <Card className="login-card">
          <div className="brand" style={{ padding: 0 }}>
            <span className="brand-name">Ledger</span>
            <span className="brand-sub">Household</span>
          </div>
          <Field label="Password">
            <input className="input" type="password" autoFocus autoComplete="current-password" value={password}
              onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <ErrorNote error={error} />
          <Button variant="primary" type="submit" disabled={busy || !password}>Sign in</Button>
        </Card>
      </form>
    </div>
  )
}
