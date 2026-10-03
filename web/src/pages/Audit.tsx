/** 审计回看：终态流程审计 + 控制台操作审计（设计方案 7.5）。 */

import { Fragment, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, describeError } from '../lib/api'
import { cn, downloadJson, fmtDateTime } from '../lib/format'
import { useWriteAction } from '../lib/actions'
import { ActionButton } from '../components/ActionButton'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { EmptyState } from '../components/EmptyState'
import { EventTimeline } from '../components/EventTimeline'
import { StageBadge } from '../components/StageBadge'
import { Toast } from '../components/Toast'

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

/** 展开行：按需拉取该流程的审计记录，用**交互式 SVG** 展示闸门事件流。 */
function FlowEventRow({ wfId }: { wfId: string }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['flow-result', wfId],
    queryFn: () => api.flowResult(wfId),
    staleTime: 60000,
  })

  if (isLoading) return <div className="px-4 py-3 text-xs text-muted">加载事件流…</div>
  if (isError) return <div className="px-4 py-3 text-xs text-danger">读取失败：{describeError(error)}</div>

  const result = data?.result
  const events = result?.gate_events ?? []
  return (
    <div className="bg-canvas px-4 py-3">
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
        <span>
          阶段 <span className="font-mono text-ink">{result?.stage ?? '—'}</span>
        </span>
        <span>
          置信度{' '}
          <span className="font-mono text-ink">
            {result?.confidence != null ? result.confidence.toFixed(2) : '—'}
          </span>
        </span>
        <span>
          补丁 <span className="font-mono text-ink">{result?.patch_id ?? '—'}</span>
        </span>
        <span>
          发布{' '}
          <span className={result?.deploy_result?.rolled_back ? 'text-danger' : 'text-ok'}>
            {result?.deploy_result
              ? result.deploy_result.rolled_back
                ? '已回滚'
                : `已全量（${result.deploy_result.version}）`
              : '—'}
          </span>
        </span>
      </div>
      <EventTimeline events={events} />
    </div>
  )
}

export function Audit() {
  const [q, setQ] = useState('')
  const [days, setDays] = useState(7)
  const [tab, setTab] = useState<'flows' | 'ops'>('flows')
  const [expanded, setExpanded] = useState<string | null>(null)
  const write = useWriteAction()

  const { data, isError, error } = useQuery({
    queryKey: ['audit', q, days],
    queryFn: () => api.audit({ q: q || undefined, days }),
    refetchInterval: 15000,
  })

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium">审计回看</h1>
          <p className="mt-1 text-xs text-muted">终态流程审计记录与控制台操作留痕</p>
        </div>
        {data && (
          <button
            type="button"
            onClick={() => downloadJson('aiops-audit.json', data)}
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

      {isError && (
        <div className="mt-4">
          <EmptyState title="无法加载审计数据" hint={describeError(error)} />
        </div>
      )}

      {data && tab === 'flows' && (
        data.items.length ? (
          <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-panel">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <th className="px-4 py-2.5 font-medium">结束时间</th>
                  <th className="px-4 py-2.5 font-medium">服务</th>
                  <th className="px-4 py-2.5 font-medium">告警</th>
                  <th className="px-4 py-2.5 font-medium">阶段</th>
                  <th className="px-4 py-2.5 font-medium">补丁</th>
                  <th className="px-4 py-2.5 font-medium">置信度</th>
                  <th className="px-4 py-2.5 font-medium" />
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
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
                        <FlowEventRow wfId={item.wf_id} />
                      </td>
                    </tr>
                  )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="mt-4">
            <EmptyState title="没有匹配的审计记录" hint="调整搜索条件或时间范围" />
          </div>
        )
      )}

      {data && tab === 'ops' && <OpsSummary days={days} />}

      {data && tab === 'ops' && (
        data.ops.length ? (
          <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-panel">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <th className="px-4 py-2.5 font-medium">时间</th>
                  <th className="px-4 py-2.5 font-medium">操作者</th>
                  <th className="px-4 py-2.5 font-medium">动作</th>
                  <th className="px-4 py-2.5 font-medium">对象</th>
                  <th className="px-4 py-2.5 font-medium">参数</th>
                  <th className="px-4 py-2.5 font-medium">结果</th>
                  <th className="px-4 py-2.5 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {data.ops.map((row, index) => {
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
                    <td className="max-w-60 truncate px-4 py-2.5 font-mono text-xs text-muted">
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

      <Toast message={write.toast} />
    </div>
  )
}
