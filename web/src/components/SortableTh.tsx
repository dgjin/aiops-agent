/** 可排序表头（各列表页共用）：点击切换升 / 降序；当前排序列箭头常显，其余悬停提示可点。 */

import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import { cn } from '../lib/format'
import type { SortState } from '../lib/sort'

export function SortableTh<K extends string>({
  label,
  colKey,
  sort,
  onToggle,
  padding = 'px-3 py-2',
  className,
}: {
  label: string
  colKey: K
  /** 当前排序状态（键放宽为 string，允许页面使用「默认排序」等伪键） */
  sort: SortState
  onToggle: (key: K) => void
  /** 单元格内边距（各表行高不同：流程类表格多用 px-4 py-2.5） */
  padding?: string
  className?: string
}) {
  const active = sort.key === colKey
  const Icon = active ? (sort.dir === 'asc' ? ArrowUp : ArrowDown) : ArrowUpDown
  return (
    <th
      className={cn('whitespace-nowrap font-medium', padding, className)}
      aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : undefined}
    >
      <button
        type="button"
        onClick={() => onToggle(colKey)}
        title={active ? (sort.dir === 'asc' ? '点击改为降序' : '点击改为升序') : '点击按此列排序'}
        className={cn(
          'group inline-flex items-center gap-1 transition-colors hover:text-ink',
          active && 'text-ink',
        )}
      >
        {label}
        <Icon
          size={11}
          className={cn(
            'shrink-0',
            active
              ? 'text-accent'
              : 'text-idle opacity-0 transition-opacity group-hover:opacity-100',
          )}
        />
      </button>
    </th>
  )
}
