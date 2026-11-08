/** 审计回看：终态流程审计 + 控制台操作审计（设计方案 7.5）。
 *
 * 两张表均支持点击表头排序（前端内存排序，默认按时间降序）。
 */

import { Fragment, useEffect, useState } from 'react'
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, describeError, saveBlob } from '../lib/api'
import { cn, downloadJson, fmtDateTime } from '../lib/format'
import { compareValues, nextSort, type SortState } from '../lib/sort'
import { useWriteAction } from '../lib/actions'
import { ActionButton } from '../components/ActionButton'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { EmptyState } from '../components/EmptyState'
import { Pagination, usePagination } from '../components/Pagination'
import { SortableTh } from '../components/SortableTh'
import { StageBadge, stageRank } from '../components/StageBadge'
import { Toast } from '../components/Toast'
import type { FlowItem, OpsAuditRow } from '../lib/types'

const INPUT_CLS =
  'rounded-lg border border-line bg-panel px-3 py-1.5 text-sm text-ink placeholder:text-idle focus:border-accent/50 focus:outline-none'

const ACTION_TONE: Array<{ match: string; cls: string }> = [
  { match: 'reject', cls: 'text-danger' },
  { match: 'cancel', cls: 'text-danger' },
  { match: 'approval:approve', cls: 'text-ok' },
  { match: 'second-approval:approve', cls: 'text-ok' },
  { match: 'deploy_now', cls: 'text-warn' },
  { match: 'queue-patch', cls: 'text-info' },
]

function actionCls(action: string): string {
  for (const item of ACTION_TONE) {
    if (action.includes(item.match)) return item.cls
  }
  return 'text-muted'
}

/** 可回滚的配置变更动作（回滚依据：该记录 changed[*].from）。 */
const ROLLBACKABLE = ['monitored-app:update', 'monitored-app:toggle']

/** 时间戳（缺失按 0，排序兜底稳定）。 */
const tsOf = (value: string | null | undefined): number => (value ? Date.parse(value) : 0)

/** 流程审计可排序列（「事件流」为操作列，不参与排序）。 */
type FlowColumnKey = 'close_time' | 'service' | 'alert_id' | 'stage' | 'patch_id' | 'confidence'

/** 操作审计可排序列（参数为 JSON 快照、操作为回滚按钮，均不参与排序）。 */
type OpsColumnKey = 'ts' | 'actor' | 'action' | 'object' | 'result'

const DEFAULT_FLOW_SORT: SortState<FlowColumnKey> = { key: 'close_time', dir: 'desc' }
const DEFAULT_OPS_SORT: SortState<OpsColumnKey> = { key: 'ts', dir: 'desc' }

/** 流程审计列排序取值（阶段按推进顺序，置信度空值排最后）。 */
function flowSortValue(item: FlowItem, key: FlowColumnKey): string | number {
  switch (key) {
    case 'close_time':
      return tsOf(item.close_time)
    case 'service':
      return item.alert?.service ?? ''
    case 'alert_id':
      return item.alert?.alert_id ?? ''
    case 'stage':
      return stageRank(item.stage)
    case 'patch_id':
      return item.patch_id ?? ''
    case 'confidence':
      return item.confidence ?? -1
  }
}

/** 操作审计列排序取值（对象列按实际展示文本：target 优先，回退工作流 ID）。 */
function opsSortValue(row: OpsAuditRow, key: OpsColumnKey): string | number {
  switch (key) {
    case 'ts':
      return tsOf(row.ts)
    case 'actor':
      return row.actor
    case 'action':
      return row.action
    case 'object':
      return row.target ?? row.wf_id
    case 'result':
      return row.result
  }
}

/** 操作审计汇总条（P3-07）：近 N 天按操作者 / 动作 / 日趋势聚合。 */
function OpsSummary({ days }: { days: number }) {
  const { data } = useQuery({
    queryKey: ['audit-summary', days],
    queryFn: () => api.auditSummary({ days }),
    refetchInterval: 30000,
  })
  if (!data || data.total === 0) return null

  const maxDay = Math.max(...data.by_day.map((item) => item.count), 1)
  const top = (items: typeof data.by_actor) =>
    items
      .slice(0, 3)
      .map((item) => `${item.key}(${item.count})`)
      .join(' · ')

  return (
    <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-xl border border-line bg-panel px-4 py-3 text-xs">
      <div>
        <span className="text-muted">近 {data.days} 天操作</span>{' '}
        <span className="font-mono text-ink">{data.total}</span>
        {data.truncated && <span className="ml-1 text-warn" title="demo 仅采样最近 5000 条">（采样口径）</span>}
      </div>
      <div className="text-muted">
        操作者 <span className="text-ink">{top(data.by_actor)}</span>
      </div>
      <div className="text-muted">
        动作 <span className="font-mono text-ink">{top(data.by_action)}</span>
      </div>
      {data.by_day.length > 1 && (
        <div className="flex items-end gap-1" title="按日趋势（悬停查看当日操作数）">
          {data.by_day.map((day) => (
            <div
              key={day.key}
              title={`${day.key}：${day.count} 次`}
              style={{ height: `${Math.max(3, (day.count / maxDay) * 20)}px` }}
              className="w-2 rounded-sm bg-accent/60"
            />
          ))}
        </div>
      )}
    </div>
  )
}

/** 展开行：统一时间线（M4）——History + 闸门 + 命令投递 + 通知 + 人工待办，并可导出证据包。 */
function FlowEventRow({ wfId, onToast }: { wfId: string; onToast: (message: string) => void }) {
  const [exporting, setExporting] = useState(false)
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['flow-timeline', wfId],
    queryFn: () => api.flowTimeline(wfId),
    staleTime: 60000,
  })

  if (isLoading) return <div className="px-4 py-3 text-xs text-muted">加载统一时间线…</div>
  if (isError) return <div className="px-4 py-3 text-xs text-danger">读取失败：{describeError(error)}</div>

  const history = data?.history ?? {}
  const exportPackage = async () => {
    setExporting(true)
    try {
      const file = await api.evidencePackage(wfId, data?.run_id)
      saveBlob(file.blob, file.filename)
      onToast(
        `证据包已导出（审计 ${file.auditEventId || '—'}，root ${file.rootSha256.slice(0, 16)}）` +
          (file.historyComplete ? '' : '；History 未完整'),
      )
    } catch (err) {
      onToast(`导出失败：${describeError(err)}`)
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="bg-canvas px-4 py-3">
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
        <span>
          阶段 <span className="font-mono text-ink">{data?.stage ?? '—'}</span>
        </span>
        <span>
          History <span className="font-mono text-ink">{history.event_count ?? 0}</span> 条{' '}
          <span className={history.complete ? 'text-ok' : 'text-warn'}>
            {history.complete ? '已完整' : history.gap ? '断档待对账' : '投影中'}
          </span>
        </span>
        <span>命令 {data?.counts.command ?? 0}</span>
        <span>通知 {data?.counts.notification ?? 0}</span>
        <button
          type="button"
          disabled={exporting}
          onClick={exportPackage}
          className="rounded border border-accent/40 px-2 py-0.5 text-accent hover:bg-accent/10 disabled:opacity-50"
        >
          {exporting ? '导出中…' : '证据包'}
        </button>
        <Link to={`/flows/${encodeURIComponent(wfId)}`} className="ml-auto text-accent hover:underline">
          在详情页查看
        </Link>
      </div>
      {data?.items?.length ? (
        <ol className="max-h-72 space-y-1 overflow-auto">
          {data.items.map((item) => (
            <li key={item.id} className="flex gap-2 rounded border border-line/60 bg-panel px-2 py-1 text-xs">
              <span className="w-28 shrink-0 font-mono text-[10px] text-muted">
                {fmtDateTime(item.time)}
                {item.time_estimated ? ' ~' : ''}
              </span>
              <span className="w-16 shrink-0 text-[10px] text-idle">{item.source}</span>
              <span className="min-w-0 flex-1 truncate" title={item.summary}>
                {item.title}
                {item.summary && <span className="ml-2 font-mono text-[10px] text-muted">{item.summary}</span>}
              </span>
            </li>
          ))}
        </ol>
      ) : (
        <div className="text-xs text-muted">暂无时间线事件</div>
      )}
    </div>
  )
}

export function Audit() {
  const [q, setQ] = useState('')
  const [days, setDays] = useState(7)
  const [tab, setTab] = useState<'flows' | 'ops'>('flows')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [flowSort, setFlowSort] = useState<SortState<FlowColumnKey>>(DEFAULT_FLOW_SORT)
  const [opsSort, setOpsSort] = useState<SortState<OpsColumnKey>>(DEFAULT_OPS_SORT)
  const [opsActor, setOpsActor] = useState('')
  const [opsAction, setOpsAction] = useState('')
  const [toast, setToast] = useState<string | null>(null)
  const write = useWriteAction()

  // 流程审计：游标分页（M4）。只有第一页参与 30s 轮询，避免翻页越多轮询越重。
  const auditQuery = useInfiniteQuery({
    queryKey: ['audit', q, days],
    queryFn: ({ pageParam }) =>
      api.audit({ q: q || undefined, days, limit: 100, cursor: pageParam || undefined }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: (query) => ((query.state.data?.pages.length ?? 1) > 1 ? false : 30000),
  })
  const flowItems = auditQuery.data?.pages.flatMap((page) => page.items) ?? []
  const source = auditQuery.data?.pages[0]?.source

  // 操作审计：独立游标分页接口（不再固定最近 100 条）。
  const opsQuery = useInfiniteQuery({
    queryKey: ['audit-ops', opsActor, opsAction],
    queryFn: ({ pageParam }) =>
      api.auditOps({
        limit: 50,
        cursor: pageParam || undefined,
        actor: opsActor || undefined,
        action: opsAction || undefined,
      }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: tab === 'ops',
    refetchInterval: (query) => ((query.state.data?.pages.length ?? 1) > 1 ? false : 30000),
  })
  const opsItems = opsQuery.data?.pages.flatMap((page) => page.items) ?? []

  const showToast = (message: string) => {
    setToast(message)
    window.setTimeout(() => setToast(null), 8000)
  }

  /** 点击表头：同列翻转升降；换列按直觉默认方向（时间降序，其余升序）。 */
  const toggleFlowSort = (key: FlowColumnKey) => {
    setFlowSort((prev) => nextSort(prev, key, key === 'close_time' ? 'desc' : 'asc'))
  }
  const toggleOpsSort = (key: OpsColumnKey) => {
    setOpsSort((prev) => nextSort(prev, key, key === 'ts' ? 'desc' : 'asc'))
  }

  // ---- 排序 + 分页：q / 时间范围 / 页签 / 排序变化回到第 1 页；两张表各自独立 ----
  const sortedFlows = [...flowItems].sort(
    (a, b) => compareValues(flowSortValue(a, flowSort.key), flowSortValue(b, flowSort.key), flowSort.dir),
  )
  const sortedOps = [...opsItems].sort(
    (a, b) => compareValues(opsSortValue(a, opsSort.key), opsSortValue(b, opsSort.key), opsSort.dir),
  )
  const flowsPg = usePagination(sortedFlows.length, 20)
  const opsPg = usePagination(sortedOps.length, 20)
  const { setPage: setFlowsPage } = flowsPg
  const { setPage: setOpsPage } = opsPg
  useEffect(() => {
    setFlowsPage(1)
    setOpsPage(1)
  }, [q, days, tab, flowSort, opsSort, opsActor, opsAction, setFlowsPage, setOpsPage])

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium">审计回看</h1>
          <p className="mt-1 text-xs text-muted">终态流程审计记录与控制台操作留痕</p>
        </div>
        {(flowItems.length > 0 || opsItems.length > 0) && (
          <button
            type="button"
            onClick={() => downloadJson('aiops-audit.json', { flows: flowItems, ops: opsItems, source })}
            className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted transition-colors hover:bg-elevated hover:text-ink"
          >
            导出 JSON
          </button>
        )}
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索工作流 / 告警 / 服务 / 补丁"
          className={cn(INPUT_CLS, 'w-72')}
        />
        <select value={days} onChange={(e) => setDays(Number(e.target.value))} className={INPUT_CLS}>
          <option value={1}>近 1 天</option>
          <option value={3}>近 3 天</option>
          <option value={7}>近 7 天</option>
          <option value={30}>近 30 天</option>
        </select>
        {tab === 'flows' && source && (
          <span className="text-xs text-muted">
            数据源 <span className="text-ink">{source === 'db' ? 'SQL 数据面' : 'Temporal 回退'}</span>
          </span>
        )}
        {tab === 'ops' && (
          <>
            <input
              value={opsActor}
              onChange={(e) => setOpsActor(e.target.value)}
              placeholder="操作者过滤"
              className={cn(INPUT_CLS, 'w-40')}
            />
            <input
              value={opsAction}
              onChange={(e) => setOpsAction(e.target.value)}
              placeholder="动作过滤（如 approval:）"
              className={cn(INPUT_CLS, 'w-48')}
            />
          </>
        )}
        <div className="ml-auto flex gap-1 rounded-lg border border-line bg-panel p-0.5">
          {(
            [
              { key: 'flows', label: '流程审计' },
              { key: 'ops', label: '操作审计' },
            ] as const
          ).map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => setTab(item.key)}
              className={cn(
                'rounded-md px-3 py-1 text-xs transition-colors',
                tab === item.key ? 'bg-accent/15 text-accent' : 'text-muted hover:text-ink',
              )}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {tab === 'flows' && auditQuery.isError && (
        <div className="mt-4">
          <EmptyState title="无法加载审计数据" hint={describeError(auditQuery.error)} />
        </div>
      )}

      {tab === 'flows' && auditQuery.data && (
        flowItems.length ? (
          <>
          <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-panel">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <SortableTh
                    label="结束时间"
                    colKey="close_time"
                    sort={flowSort}
                    onToggle={toggleFlowSort}
                    padding="px-4 py-2.5"
                  />
                  <SortableTh label="服务" colKey="service" sort={flowSort} onToggle={toggleFlowSort} padding="px-4 py-2.5" />
                  <SortableTh label="告警" colKey="alert_id" sort={flowSort} onToggle={toggleFlowSort} padding="px-4 py-2.5" />
                  <SortableTh label="阶段" colKey="stage" sort={flowSort} onToggle={toggleFlowSort} padding="px-4 py-2.5" />
                  <SortableTh label="补丁" colKey="patch_id" sort={flowSort} onToggle={toggleFlowSort} padding="px-4 py-2.5" />
                  <SortableTh
                    label="置信度"
                    colKey="confidence"
                    sort={flowSort}
                    onToggle={toggleFlowSort}
                    padding="px-4 py-2.5"
                  />
                  <th className="px-4 py-2.5 font-medium" />
                </tr>
              </thead>
              <tbody>
                {sortedFlows.slice(flowsPg.start, flowsPg.start + flowsPg.pageSize).map((item) => (
                  <Fragment key={item.wf_id}>
                  <tr className="border-b border-line/60 last:border-0 hover:bg-elevated/60">
                    <td className="px-4 py-2.5 text-xs text-muted">{fmtDateTime(item.close_time)}</td>
                    <td className="px-4 py-2.5">{item.alert?.service ?? '—'}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">
                      {item.alert?.alert_id ?? '—'}
                    </td>
                    <td className="px-4 py-2.5">
                      <StageBadge stage={item.stage} />
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">{item.patch_id ?? '—'}</td>
                    <td className="px-4 py-2.5 font-mono text-xs">
                      {item.confidence != null ? item.confidence.toFixed(2) : '—'}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-right">
                      <button
                        type="button"
                        onClick={() => setExpanded(expanded === item.wf_id ? null : item.wf_id)}
                        className="mr-3 text-xs text-muted hover:text-ink"
                      >
                        {expanded === item.wf_id ? '收起' : '事件流'}
                      </button>
                      <Link
                        to={`/flows/${encodeURIComponent(item.wf_id)}`}
                        className="text-xs text-accent hover:underline"
                      >
                        回看
                      </Link>
                    </td>
                  </tr>
                  {expanded === item.wf_id && (
                    <tr className="border-b border-line/60">
                      <td colSpan={7} className="p-0">
                        <FlowEventRow wfId={item.wf_id} onToast={showToast} />
                      </td>
                    </tr>
                  )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination pg={flowsPg} />
          {auditQuery.hasNextPage && (
            <div className="mt-3 flex justify-center">
              <button
                type="button"
                onClick={() => auditQuery.fetchNextPage()}
                disabled={auditQuery.isFetchingNextPage}
                className="rounded-lg border border-line px-4 py-1.5 text-xs text-muted hover:bg-elevated hover:text-ink disabled:opacity-50"
              >
                {auditQuery.isFetchingNextPage ? '加载中…' : '加载更早的审计记录'}
              </button>
            </div>
          )}
          </>
        ) : (
          <div className="mt-4">
            <EmptyState title="没有匹配的审计记录" hint="调整搜索条件或时间范围" />
          </div>
        )
      )}

      {tab === 'ops' && <OpsSummary days={days} />}

      {tab === 'ops' && opsQuery.isError && (
        <div className="mt-4">
          <EmptyState title="无法加载操作审计" hint={describeError(opsQuery.error)} />
        </div>
      )}

      {tab === 'ops' && opsQuery.data && (
        sortedOps.length ? (
          <>
          <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-panel">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <SortableTh label="时间" colKey="ts" sort={opsSort} onToggle={toggleOpsSort} padding="px-4 py-2.5" />
                  <SortableTh label="操作者" colKey="actor" sort={opsSort} onToggle={toggleOpsSort} padding="px-4 py-2.5" />
                  <SortableTh label="动作" colKey="action" sort={opsSort} onToggle={toggleOpsSort} padding="px-4 py-2.5" />
                  <SortableTh label="对象" colKey="object" sort={opsSort} onToggle={toggleOpsSort} padding="px-4 py-2.5" />
                  <th className="px-4 py-2.5 font-medium">参数</th>
                  <SortableTh label="结果" colKey="result" sort={opsSort} onToggle={toggleOpsSort} padding="px-4 py-2.5" />
                  <th className="px-4 py-2.5 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {sortedOps.slice(opsPg.start, opsPg.start + opsPg.pageSize).map((row, index) => {
                  // 仅「工作流 ID」才可跳转；配置类操作的对象（target）不是工作流，渲染为纯文本。
                  // 用前缀判断而非依赖新字段，历史记录同样不再产生坏链。
                  const isWorkflow = row.wf_id.startsWith('aiops-fix-')
                  const objectLabel = row.target ?? row.wf_id
                  return (
                  <tr key={`${row.ts}-${index}`} className="border-b border-line/60 last:border-0 hover:bg-elevated/60">
                    <td className="whitespace-nowrap px-4 py-2.5 text-xs text-muted">{fmtDateTime(row.ts)}</td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-xs">{row.actor}</td>
                    <td className={cn('whitespace-nowrap px-4 py-2.5 font-mono text-xs', actionCls(row.action))}>
                      {row.action}
                    </td>
                    <td className="px-4 py-2.5">
                      {isWorkflow ? (
                        <Link
                          to={`/flows/${encodeURIComponent(row.wf_id)}`}
                          className="font-mono text-xs text-accent hover:underline"
                        >
                          {row.wf_id}
                        </Link>
                      ) : (
                        <span className="font-mono text-xs text-muted" title={objectLabel ?? ''}>
                          {objectLabel || '—'}
                        </span>
                      )}
                    </td>
                    <td
                      className="max-w-60 truncate px-4 py-2.5 font-mono text-xs text-muted"
                      title={Object.keys(row.params).length ? JSON.stringify(row.params) : undefined}
                    >
                      {Object.keys(row.params).length ? JSON.stringify(row.params) : '—'}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-xs text-muted">{row.result}</td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-right">
                      {ROLLBACKABLE.includes(row.action) && row.target && (
                        <ActionButton
                          onClick={() =>
                            write.open({
                              title: `回滚「${row.target}」到该变更之前？`,
                              confirmLabel: '回滚',
                              detail: (
                                <div className="space-y-1 text-xs">
                                  <div className="text-muted">
                                    依据 {fmtDateTime(row.ts)} 的变更记录，恢复以下字段的上一版取值：
                                  </div>
                                  <pre className="max-h-40 overflow-auto rounded border border-line bg-canvas p-2 font-mono text-[11px]">
                                    {JSON.stringify(
                                      (row.params as { changed?: unknown }).changed ?? {},
                                      null,
                                      2,
                                    )}
                                  </pre>
                                </div>
                              ),
                              run: () =>
                                api.rollbackMonitoredApp(row.target as string, row.ts),
                              success: `已回滚「${row.target}」`,
                            })
                          }
                        >
                          回滚
                        </ActionButton>
                      )}
                    </td>
                  </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <Pagination pg={opsPg} />
          {opsQuery.hasNextPage && (
            <div className="mt-3 flex justify-center">
              <button
                type="button"
                onClick={() => opsQuery.fetchNextPage()}
                disabled={opsQuery.isFetchingNextPage}
                className="rounded-lg border border-line px-4 py-1.5 text-xs text-muted hover:bg-elevated hover:text-ink disabled:opacity-50"
              >
                {opsQuery.isFetchingNextPage ? '加载中…' : '加载更早的操作记录'}
              </button>
            </div>
          )}
          </>
        ) : (
          <div className="mt-4">
            <EmptyState title="暂无控制台操作记录" hint="在控制台执行的审批 / 窗口操作会追加到这里" />
          </div>
        )
      )}

      <ConfirmDialog
        open={write.spec !== null}
        title={write.spec?.title ?? ''}
        tone={write.spec?.tone}
        confirmLabel={write.spec?.confirmLabel}
        busy={write.busy}
        error={write.error}
        onConfirm={write.confirm}
        onClose={write.close}
      >
        {write.spec?.detail}
      </ConfirmDialog>

      <Toast message={write.toast ?? toast} />
    </div>
  )
}
