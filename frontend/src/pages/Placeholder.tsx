import { Card } from '../components/ui'

export function Placeholder({ title, phase, description }: { title: string; phase: number; description: string }) {
  return (
    <section className="page">
      <header className="page-header">
        <div>
          <h2>{title}</h2>
          <div className="text-muted subtitle">{description}</div>
        </div>
      </header>
      <Card style={{ maxWidth: 560 }}>
        <div className="card-kicker">Build phase {phase}</div>
        <div className="card-title">Not built yet</div>
        <p className="card-body">This screen is part of a later implementation phase.</p>
      </Card>
    </section>
  )
}
