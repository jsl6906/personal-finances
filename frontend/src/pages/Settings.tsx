import type { UseQueryResult } from '@tanstack/react-query'
import { ACCOUNT_TYPES } from '../api'
import { CrudTable } from '../components/CrudTable'
import { HierarchyEditor } from '../components/HierarchyEditor'
import { SystemPanel } from '../components/SystemPanel'
import { Seg } from '../components/ui'
import { useAccounts, useCategories, useGroups, useInstitutions, useMembers, useTags } from '../hooks'
import { accountPath, categoryPath, groupPath } from '../links'
import { oneOf, useUrl } from '../urlState'

type Tab = 'accounts' | 'categories' | 'members' | 'tags' | 'system'
const TABS: Tab[] = ['accounts', 'categories', 'members', 'tags', 'system']
type Row = { id: number } & Record<string, unknown>
type Rows = UseQueryResult<Row[]>

const TYPE_OPTIONS = ['expense', 'income', 'transfer'].map((t) => ({ value: t, label: t }))

export function Settings() {
  const [params, set] = useUrl()
  const tab = oneOf(params, 'tab', TABS, 'accounts')
  const setTab = (t: Tab) => set({ tab: t === 'accounts' ? null : t }, { reset: true })
  const accounts = useAccounts()
  const institutions = useInstitutions()
  const categories = useCategories()
  const groups = useGroups()
  const members = useMembers()
  const tags = useTags()

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <h2>Settings</h2>
          <div className="text-muted subtitle">Reference data: institutions and their accounts, category groups and their categories, household and tags</div>
        </div>
      </header>
      <Seg name="tab" value={tab} onChange={setTab} options={[
        { value: 'accounts', label: 'Accounts' }, { value: 'categories', label: 'Categories' },
        { value: 'members', label: 'Household' }, { value: 'tags', label: 'Tags' }, { value: 'system', label: 'System' },
      ]} />

      {tab === 'accounts' && (
        <HierarchyEditor
          parentKey="institution_id"
          parents={(institutions.data ?? []) as unknown as Row[]}
          items={(accounts.data ?? []) as unknown as Row[]}
          orphanLabel="No institution"
          error={institutions.error || accounts.error}
          parent={{
            label: 'Institution', path: '/institutions', queryKey: 'institutions',
            columns: [
              { key: 'name', label: 'Name', kind: 'text', required: true },
              { key: 'website', label: 'Website', kind: 'text' },
              { key: 'notes', label: 'Notes', kind: 'text' },
            ],
            defaults: { name: '', website: null, notes: null },
          }}
          child={{
            label: 'Account', path: '/accounts', queryKey: 'accounts', detail: accountPath,
            columns: [
              { key: 'name', label: 'Account', kind: 'text', required: true },
              { key: 'account_type', label: 'Type', kind: 'select', required: true,
                options: ACCOUNT_TYPES.map((t) => ({ value: t, label: t.replace('_', ' ') })) },
              { key: 'mask', label: 'Last 4', kind: 'text', width: 80 },
              { key: 'is_hidden', label: 'Hidden', kind: 'bool' },
              { key: 'is_closed', label: 'Closed', kind: 'bool' },
            ],
            defaults: { name: '', institution_id: null, account_type: 'checking', mask: null, is_hidden: false, is_closed: false, notes: null },
          }}
          dim={{ label: 'Show closed & hidden', noun: 'closed or hidden', test: (r) => Boolean(r.is_closed || r.is_hidden) }}
          summary={(kids) => `${kids.filter((k) => !k.is_closed && !k.is_hidden).length} open`}
        />
      )}
      {tab === 'categories' && (
        <HierarchyEditor
          parentKey="group_id"
          parents={(groups.data ?? []) as unknown as Row[]}
          items={(categories.data ?? []) as unknown as Row[]}
          error={groups.error || categories.error}
          parent={{
            label: 'Group', path: '/category-groups', queryKey: 'category-groups', detail: groupPath,
            columns: [
              { key: 'name', label: 'Group', kind: 'text', required: true },
              { key: 'type', label: 'Type', kind: 'select', required: true, options: TYPE_OPTIONS },
              { key: 'sort_order', label: 'Sort', kind: 'number', width: 90 },
              { key: 'hide_from_reports', label: 'Hidden from reports', kind: 'bool' },
            ],
            defaults: { name: '', type: 'expense', sort_order: 0, hide_from_reports: false },
          }}
          child={{
            label: 'Category', path: '/categories', queryKey: 'categories', detail: categoryPath,
            columns: [
              { key: 'name', label: 'Category', kind: 'text', required: true },
              { key: 'type', label: 'Type', kind: 'select', required: true, options: TYPE_OPTIONS },
              { key: 'description', label: 'Description', kind: 'text' },
              { key: 'hide_from_reports', label: 'Hidden from reports', kind: 'bool' },
              { key: 'is_active', label: 'Active', kind: 'bool' },
            ],
            defaults: { name: '', group_id: null, type: 'expense', hide_from_reports: false, is_active: true, description: null },
            badges: (r) => [...(r.hide_from_reports ? ['Hidden from reports'] : []), ...(r.is_active ? [] : ['Inactive'])],
          }}
          dim={{ label: 'Show inactive', noun: 'inactive', test: (r) => !r.is_active }}
          summary={(kids) => `${kids.filter((k) => k.is_active).length} active`}
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
