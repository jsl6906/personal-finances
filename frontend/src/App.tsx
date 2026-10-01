import { lazy, Suspense, useEffect, useState } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { get, setUnauthorizedHandler } from './api'
import { Layout } from './components/Layout'
import { AccountPage } from './pages/AccountPage'
import { Alerts } from './pages/Alerts'
import { Bills } from './pages/Bills'
import { Budgets } from './pages/Budgets'
import { CategoryPage } from './pages/CategoryPage'
import { Dashboard } from './pages/Dashboard'
import { Duplicates } from './pages/Duplicates'
import { GroupPage } from './pages/GroupPage'
import { ImportHome } from './pages/Import'
import { ImportWizard } from './pages/ImportWizard'
import { Login } from './pages/Login'
import { MerchantPage } from './pages/MerchantPage'
import { Placeholder } from './pages/Placeholder'
import { Reports } from './pages/Reports'
import { Settings } from './pages/Settings'
import { Sources } from './pages/Sources'
import { TransactionPage } from './pages/TransactionPage'
import { Transactions } from './pages/Transactions'

const Chat = lazy(() => import('./pages/Chat').then((m) => ({ default: m.Chat })))

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null)

  useEffect(() => {
    setUnauthorizedHandler(() => setAuthed(false))
    get<{ authenticated: boolean }>('/auth/me')
      .then((r) => setAuthed(r.authenticated))
      .catch(() => setAuthed(false))
  }, [])

  if (authed === null) return null
  if (!authed) return <Login onLogin={() => setAuthed(true)} />

  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout onLogout={() => setAuthed(false)} />}>
          <Route index element={<Dashboard />} />
          <Route path="reports" element={<Reports />} />
          <Route path="transactions" element={<Transactions />} />
          <Route path="transactions/:id" element={<TransactionPage />} />
          <Route path="merchants" element={<MerchantPage />} />
          <Route path="accounts/:id" element={<AccountPage />} />
          <Route path="categories/:id" element={<CategoryPage />} />
          <Route path="groups/:id" element={<GroupPage />} />
          <Route path="settings" element={<Settings />} />
          <Route path="import" element={<ImportHome />} />
          <Route path="import/:id" element={<ImportWizard />} />
          <Route path="duplicates" element={<Duplicates />} />
          <Route path="bills" element={<Bills />} />
          <Route path="budgets" element={<Budgets />} />
          <Route path="chat" element={<Suspense fallback={null}><Chat /></Suspense>} />
          <Route path="alerts" element={<Alerts />} />
          <Route path="sources" element={<Sources />} />
          <Route path="*" element={<Placeholder title="Not found" phase={0} description="" />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
