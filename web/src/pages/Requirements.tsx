/** 需求基线：被监控系统「需求收集与反馈」→ 已纳入基线条目的只读视图。
 *
 * 数据源：BFF `GET /api/requirements`（代拉被监控系统标准导出接口，契约 v1.0，
 * 客户端 aiops_agent.requirements_client，令牌环境变量 NL2SQL_OPS_TOKEN）。
 * - 多应用时顶部下拉切换（app_id 显式指定）；单应用直接展示；
 * - BFF 未配置令牌 / 应用不可达 / 401 时返回 ok=false + 中文原因：
 *   原文展示并补充部署侧配置指引，不影响其余功能；
 * - P0 / P1 高优条目置顶卡片突出，全清单表格展示（评估意见随标题小字展示）；
 * - 智能分析闭环（弹窗 components/RequirementAnalysisModal）：管理员发起分析 →
 *   查看结构化结果 → 反馈再分析（版本递增）→ 批准进入需求驱动修复工作流。
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { api, describeError } from '../lib/api'
import { cn, fmtDateTime } from '../lib/format'
import { EmptyState } from '../components/EmptyState'
import { PermissionGate } from '../components/PermissionGate'
import {
  ANALYSIS_STATUS_META,
  RequirementAnalysisModal,
} from '../components/RequirementAnalysisModal'
import type { RequirementAnalysisSummary, RequirementsEntry } from '../lib/types'

const KIND_LABELS: Record<string, string> = {
  REQUIREMENT: '功能需求',
  SUGGESTION: '改进建议',
  BUG: '问题缺陷',
  OTHER: '其他',
}

const PRIORITY_CLS: Record<string, string> = {
  P0: 'border-danger/40 bg-danger/10 text-danger',
  P1: 'border-warn/40 bg-warn/10 text-warn',
}

/** 优先级徽标（P0 红 / P1 橙 / 其余中性；空值「未定级」）。 */
function PriorityBadge({ priority }: { priority: string }) {
  if (!priority) return <span className="text-[10px] text-idle">未定级</span>
  return (
    <span
      className={cn(
        'rounded border px-1.5 py-0.5 text-[10px]',
        PRIORITY_CLS[priority] ?? 'border-line text-muted',
      )}
    >
      {priority}
    </span>
  )
}

/** 按取值函数聚合计数（降序；空值归「未标注」）。 */
function countBy(
  entries: RequirementsEntry[],
  pick: (entry: RequirementsEntry) => string,
): [string, number][] {
  const counts = new Map<string, number>()
  for (const entry of entries) {
    const key = pick(entry) || '未标注'
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1])
}

/** 一行分布摘要（类型 / 优先级 / 提交部门）。 */
function Distribution({ label, pairs }: { label: string; pairs: [string, number][] }) {
  if (!pairs.length) return null
  return (
    <div className="text-xs text-muted">
      <span className="text-idle">{label}：</span>
      {pairs.map(([key, count], index) => (
        <span key={key}>
          {index > 0 && <span className="mx-1 text-idle">·</span>}
          {key} <span className="text-ink">{count}</span>
        </span>
      ))}
    </div>
  )
}

/** 智能分析入口：无会话 → admin 发起；有会话 → 任意角色查看（状态徽标按钮）。 */
function AnalysisButton({
  summary,
  onOpen,
}: {
  summary?: RequirementAnalysisSummary
  onOpen: () => void
}) {
  if (summary) {
    const meta = ANALYSIS_STATUS_META[summary.status] ?? {
      label: summary.status || '未知',
      cls: 'border-line text-muted',
    }
    return (
      <button
        type="button"
        onClick={onOpen}
        title="查看分析结果 / 提交反馈 / 批准进入修复流程"
        className={cn(
          'rounded-lg border px-2 py-1 text-[11px] transition-opacity hover:opacity-80',
          meta.cls,
        )}
      >
        {meta.label} · v{summary.current_version}
      </button>
    )
  }
  return (
    <PermissionGate require="admin" fallback={<span className="text-[11px] text-idle">—</span>}>
      <button
        type="button"
        onClick={onOpen}
        title="调用 LLM 分析该需求并给出实现方案（可多轮反馈优化后进入修复流程）"
        className="rounded-lg border border-accent/40 px-2 py-1 text-[11px] text-accent transition-colors hover:bg-accent/10"
      >
        智能分析
      </button>
    </PermissionGate>
  )
}

export function Requirements() {
  const [appId, setAppId] = useState('')
  const [analysisTarget, setAnalysisTarget] = useState<{
    entry: RequirementsEntry
    analysisId: string | null
  } | null>(null)
  const { data, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['requirements', appId],
    queryFn: () => api.requirements(appId ? { app_id: appId } : {}),
  })

  const current = data?.app ?? null
  const analysisAppId = current?.id ?? ''
  const analysesQuery = useQuery({
    queryKey: ['requirement-analyses', analysisAppId],
    queryFn: () => api.requirementAnalyses(analysisAppId ? { app_id: analysisAppId } : {}),
    enabled: !!analysisAppId,
    // 有分析执行中时 3 秒轮询（全部完成后自动停止）
    refetchInterval: (query) =>
      (query.state.data?.analyses ?? []).some((item) => item.status === 'analyzing') ? 3000 : false,
  })
  const analysisMap = useMemo(() => {
    const map = new Map<string, RequirementAnalysisSummary>()
    for (const item of analysesQuery.data?.analyses ?? []) map.set(String(item.entry_id), item)
    return map
  }, [analysesQuery.data])

  const openAnalysis = (entry: RequirementsEntry) => {
    const summary = analysisMap.get(String(entry.id))
    setAnalysisTarget({ entry, analysisId: summary?.id ?? null })
  }

  const entries = data?.ok ? data.entries : []
  const high = entries.filter((entry) => entry.priority === 'P0' || entry.priority === 'P1')
  const p0 = high.filter((entry) => entry.priority === 'P0').length
  const p1 = high.length - p0

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-medium">需求基线</h1>
          <p className="mt-1 text-xs text-muted">
            被监控系统「需求收集与反馈」中已由管理员评估并纳入基线的条目；P0 / P1 高优置顶，
            管理员可发起智能分析（查看方案 → 反馈优化 → 批准进入修复流程）
          </p>
        </div>
        <button
          type="button"
          onClick={() => refetch()}
          title="重新拉取需求基线条目"
          className="flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-xs text-muted transition-colors hover:bg-elevated hover:text-ink"
        >
          <RefreshCw size={13} className={cn(isFetching && 'animate-spin')} />
          刷新
        </button>
      </div>

      {/* 数据源行：多应用下拉切换；失败态同样可切换目标应用排查 */}
      {data && data.apps.length > 0 && (
        <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted">
          {data.apps.length > 1 ? (
            <label className="flex items-center gap-1.5">
              数据源：
              <select
                value={appId || current?.id || ''}
                onChange={(event) => setAppId(event.target.value)}
                className="rounded-lg border border-line bg-canvas px-2 py-1 text-xs text-ink focus:border-accent/50 focus:outline-none"
              >
                {data.apps.map((app) => (
                  <option key={app.id} value={app.id}>
                    {app.name}
                    {app.enabled ? '' : '（已停用）'}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            current && (
              <span>
                数据源：{current.name}
                {current.enabled ? '' : '（已停用）'}
              </span>
            )
          )}
          {current?.url && <span className="font-mono">{current.url}</span>}
          {data.system && <span>系统：{data.system}</span>}
          {data.exported_at && <span>导出时间：{fmtDateTime(data.exported_at)}</span>}
        </div>
      )}

      {isError && (
        <div className="mt-5">
          <EmptyState title="无法加载需求基线" hint={describeError(error)} />
        </div>
      )}

      {/* 拉取失败（未配置令牌 / 应用不可达 / 401 等）：原文展示 + 部署侧指引 */}
      {data && !data.ok && (
        <div className="mt-5 rounded-xl border border-warn/40 bg-warn/10 p-4 text-xs">
          <div className="font-medium text-warn">暂无法获取需求基线数据</div>
          <div className="mt-1.5 text-muted">{data.error}</div>
          <div className="mt-1.5 text-idle">
            部署侧配置：被监控应用需部署「需求收集与反馈」能力并开放标准导出接口
            （GET /api/requirements/export）；BFF 环境变量{' '}
            <span className="font-mono text-[11px]">NL2SQL_OPS_TOKEN</span> 的值须与该应用的{' '}
            <span className="font-mono text-[11px]">OPS_API_TOKEN</span>{' '}
            一致（配置后重启 BFF，再点右上角「刷新」）。
          </div>
        </div>
      )}

      {data && data.ok && (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
            <span>共 {entries.length} 条</span>
            <span className={cn(p0 > 0 && 'text-danger')}>P0 高优 {p0}</span>
            <span className={cn(p1 > 0 && 'text-warn')}>P1 高优 {p1}</span>
            {analysisMap.size > 0 && <span>智能分析 {analysisMap.size} 条</span>}
            {data.warnings.map((warning) => (
              <span key={warning} className="text-warn">
                {warning}
              </span>
            ))}
          </div>

          <div className="mt-2 space-y-1">
            <Distribution
              label="类型"
              pairs={countBy(entries, (entry) => KIND_LABELS[entry.kind] ?? entry.kind)}
            />
            <Distribution label="优先级" pairs={countBy(entries, (entry) => entry.priority)} />
            <Distribution label="提交部门" pairs={countBy(entries, (entry) => entry.department)} />
          </div>

          {high.length > 0 && (
            <section className="mt-5">
              <h2 className="text-sm font-medium text-ink">高优条目（P0 / P1）</h2>
              <div className="mt-2 space-y-3">
                {high.map((entry) => (
                  <div key={String(entry.id)} className="rounded-xl border border-line bg-panel p-4">
                    <div className="flex flex-wrap items-center gap-2.5">
                      <PriorityBadge priority={entry.priority} />
                      <span className="text-sm font-medium">{entry.title}</span>
                      <span className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted">
                        {KIND_LABELS[entry.kind] ?? (entry.kind || '—')}
                      </span>
                    </div>
                    {entry.content && <p className="mt-2 text-xs text-muted">{entry.content}</p>}
                    {entry.assessment && (
                      <p className="mt-1.5 text-xs text-idle">评估意见：{entry.assessment}</p>
                    )}
                    <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted">
                      <span className="font-mono">#{entry.id}</span>
                      {entry.baselineVersion && <span>基线 {entry.baselineVersion}</span>}
                      <span>
                        {entry.submitter || '—'}
                        {entry.department ? ` · ${entry.department}` : ''}
                      </span>
                      {entry.updatedAt && <span>更新 {fmtDateTime(entry.updatedAt)}</span>}
                      <span className="ml-auto">
                        <AnalysisButton
                          summary={analysisMap.get(String(entry.id))}
                          onOpen={() => openAnalysis(entry)}
                        />
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )}

          {entries.length > 0 ? (
            <section className="mt-5">
              <h2 className="text-sm font-medium text-ink">全清单</h2>
              <div className="mt-2 overflow-x-auto rounded-xl border border-line">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-line text-left text-muted">
                      <th className="px-3 py-2 font-medium">ID</th>
                      <th className="px-3 py-2 font-medium">类型</th>
                      <th className="px-3 py-2 font-medium">优先级</th>
                      <th className="px-3 py-2 font-medium">标题</th>
                      <th className="px-3 py-2 font-medium">基线版本</th>
                      <th className="px-3 py-2 font-medium">提交人 / 部门</th>
                      <th className="px-3 py-2 font-medium">更新时间</th>
                      <th className="px-3 py-2 font-medium">智能分析</th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries.map((entry) => (
                      <tr key={String(entry.id)} className="border-b border-line/60 last:border-0">
                        <td className="px-3 py-2 font-mono text-muted">{entry.id}</td>
                        <td className="px-3 py-2 text-muted">
                          {KIND_LABELS[entry.kind] ?? (entry.kind || '—')}
                        </td>
                        <td className="px-3 py-2">
                          <PriorityBadge priority={entry.priority} />
                        </td>
                        <td className="max-w-md px-3 py-2">
                          <div className="truncate" title={entry.title}>
                            {entry.title}
                          </div>
                          {entry.assessment && (
                            <div className="truncate text-[11px] text-idle" title={entry.assessment}>
                              评估：{entry.assessment}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2 text-muted">{entry.baselineVersion || '—'}</td>
                        <td className="px-3 py-2 text-muted">
                          {entry.submitter || '—'}
                          {entry.department ? ` · ${entry.department}` : ''}
                        </td>
                        <td className="px-3 py-2 text-muted">
                          {entry.updatedAt ? fmtDateTime(entry.updatedAt) : '—'}
                        </td>
                        <td className="px-3 py-2">
                          <AnalysisButton
                            summary={analysisMap.get(String(entry.id))}
                            onOpen={() => openAnalysis(entry)}
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ) : (
            <div className="mt-5">
              <EmptyState
                title="暂无已纳入基线的需求"
                hint="被监控系统管理员在「需求收集与反馈」中评估条目并「纳入基线」后，这里即可看到"
              />
            </div>
          )}
        </>
      )}

      {analysisTarget && current && (
        <RequirementAnalysisModal
          open
          appId={current.id}
          entry={analysisTarget.entry}
          existingId={analysisTarget.analysisId}
          onClose={() => setAnalysisTarget(null)}
        />
      )}
    </div>
  )
}
