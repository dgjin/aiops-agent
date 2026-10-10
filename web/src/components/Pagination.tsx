/** 列表分页条（需求基线 / 转人工待办 / 流程列表 / 审计回看 / 系统清单共用）。
 *
 * - usePagination：页码状态 + 越界自动夹取（过滤后条目变少无需手动修正；
 *   过滤条件变化时由调用方 setPage(1) 回到第 1 页）；
 * - Pagination：当前范围计数 + 页码窗口（首尾 + 当前页 ±1，省略号折叠）+ 每页条数；
 *   过滤后总数 ≤ 最小页长（10）时不渲染——条目少于一页时分页无意义。
 */

import { useState } from 'react'
import { cn } from '../lib/format'

export const PAGE_SIZE_OPTIONS = [10, 20, 50] as const

export function usePagination(total: number, initialSize: number = 20) {
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<number>(initialSize)
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const current = Math.min(page, pageCount)
  const start = (current - 1) * pageSize
  const end = Math.min(start + pageSize, total)
  /** 切换每页条数并回到第 1 页（避免停留在无意义的页码上）。 */
  const changeSize = (size: number) => {
    setPageSize(size)
    setPage(1)
  }
  return { total, pageSize, pageCount, current, start, end, setPage, changeSize }
}

export type PaginationState = ReturnType<typeof usePagination>

/** 页码窗口：页数少时全部展示；多时保留首尾与当前页前后一页。 */
function pageWindow(current: number, pageCount: number): (number | '…')[] {
  if (pageCount <= 7) return Array.from({ length: pageCount }, (_, index) => index + 1)
  const wanted = [...new Set([1, pageCount, current - 1, current, current + 1])]
    .filter((page) => page >= 1 && page <= pageCount)
    .sort((a, b) => a - b)
  const out: (number | '…')[] = []
  let prev = 0
  for (const page of wanted) {
    if (page - prev > 1) out.push('…')
    out.push(page)
    prev = page
  }
  return out
}

const BTN_BASE =
  'inline-flex h-7 min-w-7 items-center justify-center rounded-md border px-1.5 text-xs transition-colors disabled:pointer-events-none disabled:opacity-40'
const BTN_IDLE = 'border-line text-muted hover:bg-elevated hover:text-ink'

export function Pagination({ pg }: { pg: PaginationState }) {
  const { total, pageSize, pageCount, current, start, end, setPage, changeSize } = pg
  if (total <= PAGE_SIZE_OPTIONS[0]) return null
  return (
    <div className="mt-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-xs">
      <span className="text-idle">
        第 <span className="font-mono text-ink">{start + 1}</span>–
        <span className="font-mono text-ink">{end}</span> 条 · 共{' '}
        <span className="font-mono text-ink">{total}</span> 条
      </span>
      <div className="flex flex-wrap items-center gap-1">
        <button
          type="button"
          disabled={current <= 1}
          onClick={() => setPage(current - 1)}
          className={cn(BTN_BASE, BTN_IDLE)}
        >
          ‹ 上一页
        </button>
        {pageWindow(current, pageCount).map((item, index) =>
          item === '…' ? (
            <span key={`gap-${index}`} className="px-1 text-idle">
              …
            </span>
          ) : (
            <button
              key={item}
              type="button"
              onClick={() => setPage(item)}
              className={cn(
                BTN_BASE,
                item === current
                  ? 'border-accent/50 bg-accent/15 font-medium text-accent'
                  : BTN_IDLE,
              )}
            >
              {item}
            </button>
          ),
        )}
        <button
          type="button"
          disabled={current >= pageCount}
          onClick={() => setPage(current + 1)}
          className={cn(BTN_BASE, BTN_IDLE)}
        >
          下一页 ›
        </button>
        <label className="ml-2 flex items-center gap-1.5 text-idle">
          每页
          <select
            value={pageSize}
            onChange={(event) => changeSize(Number(event.target.value))}
            className="rounded-md border border-line bg-panel px-1.5 py-1 text-xs text-ink focus:border-accent/50 focus:outline-none"
          >
            {PAGE_SIZE_OPTIONS.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
          条
        </label>
      </div>
    </div>
  )
}
