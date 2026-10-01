import { groupCategories, useCategories } from '../hooks'

type Props = {
  value: number | null
  onChange: (id: number | null) => void
  emptyLabel?: string
  className?: string
  id?: string
}

export function CategorySelect({ value, onChange, emptyLabel = 'Uncategorized', className = 'input', id }: Props) {
  const { data } = useCategories()
  return (
    <select id={id} className={className} value={value ?? ''}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}>
      <option value="">{emptyLabel}</option>
      {groupCategories(data, value).map(([group, cats]) => (
        <optgroup key={group} label={group}>
          {cats.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </optgroup>
      ))}
    </select>
  )
}
