import { useState } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'
import { ACCOUNT_TYPES } from '../api'
import { CrudTable } from '../components/CrudTable'
import { SystemPanel } from '../components/SystemPanel'
import { Seg } from '../components/ui'
import { useAccounts, useCategories, useGroups, useInstitutions, useMembers, useTags } from '../hooks'

type Tab = 'accounts' | 'institutions' | 'categories' | 'groups' | 'members' | 'tags' | 'system'
type Rows = UseQueryResult<({ id: number } & Record<string, unknown>)[]>

const TYPE_OPTIONS = ['expense', 'income', 'transfer'].map((t) => ({ value: t, label: t }))

export function Settings() {
  const [tab, setTab] = useState<Tab>('accounts')
  const accounts = useAccounts()
  const institutions = useInstitutions()
  const categories = useCategories()
  const groups = useGroups()
  const members = useMembers()
  const tags = useTags()

  const instOptions = (institutions.data ?? []).map((i) => ({ value: i.id, label: i.name }))
  const groupOptions = (groups.data ?? []).map((g) => ({ value: g.id, label: g.name }))

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <h2>Settings</h2>
          <div className="text-muted subtitle">Reference data: accounts, institutions, category hierarchy, household and tags</div>
        </div>
      </header>
      <Seg name="tab" value={tab} onChange={setTab} options={[
        { value: 'accounts', label: 'Accounts' }, { value: 'institutions', label: 'Institutions' },
        { value: 'categories', label: 'Categories' }, { value: 'groups', label: 'Category groups' },
        { value: 'members', label: 'Household' }, { value: 'tags', label: 'Tags' }, { value: 'system', label: 'System' },
      ]} />

      {tab === 'accounts' && (
        <CrudTable title="Accounts" kicker="Reference" path="/accounts" queryKey="accounts" query={accounts as unknown as Rows}
          columns={[
            { key: 'name', label: 'Name', kind: 'text', required: true },
            { key: 'institution_id', label: 'Institution', kind: 'select', options: instOptions },
            { key: 'account_type', label: 'Type', kind: 'select', required: true,
              options: ACCOUNT_TYPES.map((t) => ({ value: t, label: t.replace('_', ' ') })) },
            { key: 'mask', label: 'Last 4', kind: 'text', width: 90 },
            { key: 'is_hidden', label: 'Hidden', kind: 'bool' },
            { key: 'is_closed', label: 'Closed', kind: 'bool' },
          ]}
          defaults={{ name: '', institution_id: null, account_type: 'checking', mask: null, is_hidden: false, is_closed: false, notes: null }}
          display={(row, key) => (key === 'institution_id' ? String(row.institution_name ?? '—') : undefined)}
        />
      )}
      {tab === 'institutions' && (
        <CrudTable title="Institutions" kicker="Reference" path="/institutions" queryKey="institutions"
          query={institutions as unknown as Rows}
          columns={[
            { key: 'name', label: 'Name', kind: 'text', required: true },
            { key: 'website', label: 'Website', kind: 'text' },
            { key: 'notes', label: 'Notes', kind: 'text' },
          ]}
          defaults={{ name: '', website: null, notes: null }}
        />
      )}
      {tab === 'categories' && (
        <CrudTable title="Categories" kicker="Hierarchy · group › category" path="/categories" queryKey="categories"
          query={categories as unknown as Rows}
          columns={[
            { key: 'group_id', label: 'Group', kind: 'select', required: true, options: groupOptions },
            { key: 'name', label: 'Category', kind: 'text', required: true },
            { key: 'type', label: 'Type', kind: 'select', required: true, options: TYPE_OPTIONS },
            { key: 'hide_from_reports', label: 'Hide from reports', kind: 'bool' },
            { key: 'is_active', label: 'Active', kind: 'bool' },
          ]}
          defaults={{ name: '', group_id: null, type: 'expense', hide_from_reports: false, is_active: true, description: null }}
          display={(row, key) => (key === 'group_id' ? String(row.group_name) : undefined)}
        />
      )}
      {tab === 'groups' && (
        <CrudTable title="Category groups" kicker="Hierarchy" path="/category-groups" queryKey="category-groups"
          query={groups as unknown as Rows}
          columns={[
            { key: 'name', label: 'Group', kind: 'text', required: true },
            { key: 'type', label: 'Type', kind: 'select', required: true, options: TYPE_OPTIONS },
            { key: 'sort_order', label: 'Sort', kind: 'number', width: 90 },
            { key: 'hide_from_reports', label: 'Hide from reports', kind: 'bool' },
          ]}
          defaults={{ name: '', type: 'expense', sort_order: 0, hide_from_reports: false }}
        />
      )}
      {tab === 'members' && (
        <CrudTable title="Household members" kicker="People" path="/members" queryKey="members" query={members as unknown as Rows}
          columns={[
            { key: 'name', label: 'Name', kind: 'text', required: true },
            { key: 'initials', label: 'Initials', kind: 'text', required: true, width: 90 },
            { key: 'email', label: 'Email', kind: 'text' },
          ]}
          defaults={{ name: '', initials: '', email: null }}
        />
      )}
      {tab === 'tags' && (
        <CrudTable title="Tags" kicker="Labels" path="/tags" queryKey="tags" query={tags as unknown as Rows}
          columns={[
            { key: 'name', label: 'Name', kind: 'text', required: true },
            { key: 'color', label: 'Color', kind: 'color', width: 180 },
          ]}
          defaults={{ name: '', color: null }}
        />
      )}
      {tab === 'system' && <SystemPanel />}
    </section>
  )
}
