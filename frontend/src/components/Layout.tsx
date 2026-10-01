import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { post } from '../api'
import { useMembers } from '../hooks'
import { Icon } from './ui'

const NAV = [
  { to: '/', icon: 'dashboard', label: 'Dashboard' },
  { to: '/reports', icon: 'reports', label: 'Reports' },
  { to: '/transactions', icon: 'transactions', label: 'Transactions' },
  { to: '/import', icon: 'import', label: 'Import' },
  { to: '/bills', icon: 'bills', label: 'Bills & statements' },
  { to: '/budgets', icon: 'budgets', label: 'Budgets' },
  { to: '/chat', icon: 'chat', label: 'Ask the ledger' },
  { to: '/alerts', icon: 'alerts', label: 'Alerts' },
  { to: '/sources', icon: 'sources', label: 'Sources & backfill' },
]

export function Layout({ onLogout }: { onLogout: () => void }) {
  const members = useMembers()
  const qc = useQueryClient()
  const { pathname } = useLocation()
  const [navOpen, setNavOpen] = useState(false)
  useEffect(() => { window.scrollTo(0, 0) }, [pathname])
  const logout = async () => {
    await post('/auth/logout')
    qc.clear()
    onLogout()
  }
  return (
    <div className={`shell${navOpen ? ' nav-open' : ''}`}>
      <header className="topbar">
        <button className="btn btn-icon" aria-label="Menu" aria-expanded={navOpen} onClick={() => setNavOpen((o) => !o)}>
          <Icon name={navOpen ? 'close' : 'menu'} size={20} />
        </button>
        <span className="brand-name">Ledger</span>
      </header>
      <div className="nav-backdrop" onClick={() => setNavOpen(false)} />
      <aside className="sidebar" onClick={(e) => { if ((e.target as HTMLElement).closest('a')) setNavOpen(false) }}>
        <div className="brand">
          <span className="brand-name">Ledger</span>
          <span className="brand-sub">Household</span>
        </div>
        <nav className="side-nav">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === '/'} className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>
              <Icon name={n.icon} />
              <span className="label">{n.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="side-foot">
          <NavLink to="/settings" className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`} style={{ padding: '6px 0' }}>
            <Icon name="settings" />
            <span className="label">Settings</span>
          </NavLink>
          <div className="row" style={{ gap: 6 }}>
            {(members.data ?? []).map((m) => (
              <span key={m.id} className="avatar" title={m.name}>{m.initials}</span>
            ))}
            <button className="btn btn-ghost small" onClick={logout} style={{ marginLeft: 'auto' }}>Sign out</button>
          </div>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}
