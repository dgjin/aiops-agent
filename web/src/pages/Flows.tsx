/** 流程列表：状态 / 阶段 / 服务过滤 + 明细表（设计方案 7.3）。
 *
 * 内容管理：点击表头按任意列升 / 降序（默认开始时间降序），全部为前端内存排序。
 */

import { Fragment, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { api, describeError } from '../lib/api'
import { cn, fmtDateTime, fmtDuration } from '../lib/format'
import { compareValues, nextSort, type SortState } from '../lib/sort'
import { EmptyState } from '../components/EmptyState'
import { FlowStageStrip } from '../components/FlowStageStrip'
import { Pagination, usePagination } from '../components/Pagination'
import { SortableTh } from '../components/SortableTh'
import { STAGE_META, StageBadge, stageRank } from '../components/StageBadge'
import type { FlowItem } from '../lib/types'

const STAGE_OPTIONS = [
  'DETECTED',
  'TRIAGING',
  'FIXING',
  'TESTING',
  'WAIT_APPROVAL',
  'NOTIFYING',
  'CANARY',
  'ROLLING_OUT',
  'DONE',
  'SHADOWED',
  'ESCALATED',
  'FAILED',
  'CANCELLED',
]

const INPUT_CLS =
  'rounded-lg border border-line bg-panel px-3 py-1.5 text-sm text-ink focus:border-accent/50 focus:outline-none'

/** 可排序列（操作列为链接图标，不参与排序）。 */
type ColumnKey = 'wf_id' | 'service' | 'alert_id' | 'stage' | 'start_time' | 'duration'

/** 默认排序：开始时间降序（最新流程在前）。 */
const DEFAULT_SORT: SortState<ColumnKey> = { key: 'start_time', dir: 'desc' }

/** 时间戳（缺失按 0，排序兜底稳定）。 */
const tsOf = (value: string | null | undefined): number => (value ? Date.parse(value) : 0)

/** 列排序取值（阶段按推进顺序，服务 / 告警取告警快照）。 */
function sortValue(item: FlowItem, key: ColumnKey): string | number {
  switch (key) {
    case 'wf_id':
      return item.wf_id
    case 'service':
      return item.alert?.service ?? ''
    case 'alert_id':
      return item.alert?.alert_id ?? ''
    case 'stage':
      return stageRank(item.stage)
    case 'start_time':
      return tsOf(item.start_time)
    case 'duration':
      return item.duration_seconds ?? 0
  }
}

/** 列排序比较器（同类型数值直接比较，其余按中文排序；结果按方向取反）。 */
function compareByColumn(a: FlowItem, b: FlowItem, sort: SortState<ColumnKey>): number {
  return compareValues(sortValue(a, sort.key), sortValue(b, sort.key), sort.dir)
}

export function Flows() {
  // 支持从看板环图跳转（/flows?stage=DONE）：以 URL 参数初始化筛选
  const [params] = useSearchParams()
  const [status, setStatus] = useState('all')
  const [stage, setStage] = useState(params.get('stage') ?? '')
  const [service, setService] = useState(params.get('service') ?? '')
  // 单行手风琴：点击行在其下端展开简要流程图（再点收起；列表刷新不重置）
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [sort, setSort] = useState<SortState<ColumnKey>>(DEFAULT_SORT)

  const { data, isError, error } = useQuery({
    queryKey: ['flows', status, stage, service],
    queryFn: () => api.flows({ status, stage: stage || undefined, service: service || undefined }),
    refetchInterval: 5000,
  })

  /** 点击表头：同列翻转升降；换列按直觉默认方向（开始时间降序，其余升序）。 */
  const toggleSort = (key: ColumnKey) => {
    setSort((prev) => nextSort(prev, key, key === 'start_time' ? 'desc' : 'asc'))
  }

  const items = data?.items ?? []
  const sorted = [...items].sort((a, b) => compareByColumn(a, b, sort))

  // ---- 分页：过滤条件 / 排序变化回到第 1 页（总数变化时越界页码自动夹取）----
  const pg = usePagination(sorted.length, 20)
  const { setPage } = pg
  useEffect(() => {
    setPage(1)
  }, [status, stage, service, sort, setPage])
  const view = sorted.slice(pg.start, pg.start + pg.pageSize)

  return (
    <div>
      <h1 className="text-lg font-medium">流程列表</h1>
      <p className="mt-1 text-xs text-muted">全部工作流（含终态），支持状态 / 阶段 / 服务过滤</p>

      <div className="mt-5 flex flex-wrap items-center gap-3">
        <select value={status} onChange={(e) => setStatus(e.target.value)} className={INPUT_CLS}>
          <option value="all">全部状态</option>
          <option value="open">运行中</option>
          <option value="closed">已结束</option>
        </select>
        <select value={stage} onChange={(e) => setStage(e.target.value)} className={INPUT_CLS}>
          <option value="">全部阶段</option>
          {STAGE_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {STAGE_META[s]?.label ?? s}
            </option>
          ))}
        </select>
        <input
          value={service}
          onChange={(e) => setService(e.target.value)}
          placeholder="按服务名过滤"
          className={INPUT_CLS}
        />
        {data && <span className="ml-auto text-xs text-idle">共 {data.items.length} 条</span>}
      </div>

      {isError && (
        <div className="mt-4">
          <EmptyState title="无法加载流程列表" hint={describeError(error)} />
        </div>
      )}

      {data &&
        (data.items.length ? (
          <>
          <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-panel">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <SortableTh label="工作流" colKey="wf_id" sort={sort} onToggle={toggleSort} padding="px-4 py-2.5" />
                  <SortableTh label="服务" colKey="service" sort={sort} onToggle={toggleSort} padding="px-4 py-2.5" />
                  <SortableTh label="告警" colKey="alert_id" sort={sort} onToggle={toggleSort} padding="px-4 py-2.5" />
                  <SortableTh label="阶段" colKey="stage" sort={sort} onToggle={toggleSort} padding="px-4 py-2.5" />
                  <SortableTh
                    label="开始时间"
                    colKey="start_time"
                    sort={sort}
                    onToggle={toggleSort}
                    padding="px-4 py-2.5"
                  />
                  <SortableTh label="耗时" colKey="duration" sort={sort} onToggle={toggleSort} padding="px-4 py-2.5" />
                  <th className="px-4 py-2.5 font-medium" />
                </tr>
              </thead>
              <tbody>
                {view.map((item) => {
                  const expanded = expandedId === item.wf_id
                  return (
                  <Fragment key={item.wf_id}>
                  <tr
                    className={cn(
                      'cursor-pointer border-b border-line/60 hover:bg-elevated/60',
                      expanded ? 'bg-elevated/40' : undefined,
                    )}
                    onClick={() => setExpandedId(expanded ? null : item.wf_id)}
                  >
                    <td className="px-4 py-2.5 font-mono text-xs">{item.wf_id}</td>
                    <td className="px-4 py-2.5">{item.alert?.service ?? '—'}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">
                      {item.alert?.alert_id ?? '—'}
                    </td>
                    <td className="px-4 py-2.5">
                      <StageBadge stage={item.stage} />
                    </td>
                    <td className="px-4 py-2.5 text-xs text-muted">{fmtDateTime(item.start_time)}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">
                      {item.exec_status === 'RUNNING' ? '运行中' : fmtDuration(item.duration_seconds)}
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center justify-end gap-3">
                        <ChevronRight
                          className={cn('size-3.5 text-idle transition-transform', expanded && 'rotate-90 text-accent')}
                        />
                        <Link
                          to={`/flows/${encodeURIComponent(item.wf_id)}`}
                          className="text-xs text-accent hover:underline"
                          onClick={(e) => e.stopPropagation()}
                        >
                          详情
                        </Link>
                      </div>
                    </td>
                  </tr>
                  {expanded && (
                    <tr className="border-b border-line/60 last:border-0">
                      <td colSpan={7} className="animate-row-reveal bg-canvas/50 px-4 pb-4 pt-2">
                        <div className="rounded-lg border border-line bg-panel">
                          <FlowStageStrip item={item} className="px-4 py-3" />
                        </div>
                      </td>
                    </tr>
                  )}
                  </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
          <Pagination pg={pg} />
          </>
        ) : (
          <div className="mt-4">
            <EmptyState title="没有匹配的流程" hint="调整过滤条件，或等待新告警接入" />
          </div>
        ))}
    </div>
  )
}
