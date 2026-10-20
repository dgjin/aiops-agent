/** 系统工作台（/systems/:appId）：以单个被监控系统为视角的一屏聚合。
 *
 * 三级钻取的第二级：健康 / 进行中流程 / 活动时间线 / 流程历史 / 数据链路 /
 * 配置摘要 / 需求基线——全部围绕「该系统」组织，数据来自现有 API：
 * - monitored-apps 单条（probe / watcher / readiness）
 * - flows?service=X（后端已支持按系统过滤）
 * - systems/{appId}/timeline（P3⑨ 服务端聚合：gate_events 翻译 + 快照推导）
 * - requirements/analyses?app_id=X（后端已支持按系统过滤）
 *
 * 流程历史表支持点击表头排序（默认开始时间降序）。
 */

import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowUpRight, Wrench } from 'lucide-react'
import { api, describeError } from '../lib/api'
import { cn, fmtDateTime, fmtDuration, fmtTime } from '../lib/format'
import { compareValues, nextSort, type SortState } from '../lib/sort'
import {
  AppHealthDot,
  ProbeBadge,
  ReadinessLine,
  WatcherLine,
  fmtHM,
} from '../components/MonitoredAppStatus'
import { CountdownText } from '../components/CountdownText'
import { EmptyState } from '../components/EmptyState'
import { RELEASE_STATUS_META } from '../components/RequirementAnalysisModal'
import { SortableTh } from '../components/SortableTh'
import { StageBadge, stageRank } from '../components/StageBadge'
import type { FlowItem, MonitoredApp, RequirementAnalysisSummary, TimelineTone } from '../lib/types'

/** 发布结果 → 工作台纯文本颜色（徽标配色见 RELEASE_STATUS_META，此处只取文字色）。 */
const RELEASE_TEXT_CLS: Record<string, string> = {
  running: 'text-info',
  released: 'text-ok',
  escalated: 'text-warn',
  cancelled: 'text-muted',
  failed: 'text-danger',
  unknown: 'text-ok',
}

/** 时间线事件色调 → 色点 / 文本类名（muted 降级为 idle 灰，保持可读）。 */
const TONE_DOT: Record<TimelineTone, string> = {
  info: 'bg-info',
  ok: 'bg-ok',
  warn: 'bg-warn',
  danger: 'bg-danger',
  idle: 'bg-idle',
  muted: 'bg-idle',
}
const TONE_TEXT: Record<TimelineTone, string> = {
  info: 'text-muted',
  ok: 'text-ok',
  warn: 'text-warn',
  danger: 'text-danger',
  idle: 'text-idle',
  muted: 'text-idle',
}

/** 流程历史可排序列（「详情」为链接列，不参与排序）。 */
type ColumnKey = 'wf_id' | 'alert_id' | 'stage' | 'duration' | 'start_time'

/** 默认排序：开始时间降序（最新流程在前）。 */
const DEFAULT_SORT: SortState<ColumnKey> = { key: 'start_time', dir: 'desc' }

/** 时间戳（缺失按 0，排序兜底稳定）。 */
const tsOf = (value: string | null | undefined): number => (value ? Date.parse(value) : 0)

/** 列排序取值（阶段按推进顺序；运行中流程耗时按 0 兜底）。 */
function sortValue(item: FlowItem, key: ColumnKey): string | number {
  switch (key) {
    case 'wf_id':
      return item.wf_id
    case 'alert_id':
      return item.alert?.alert_id ?? ''
    case 'stage':
      return stageRank(item.stage)
    case 'duration':
      return item.duration_seconds ?? 0
    case 'start_time':
      return tsOf(item.start_time)
  }
}

/** 侧区卡片壳（统一标题 + 内容节奏）。 */
function SideCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-line bg-panel p-4">
      <div className="mb-2.5 text-xs font-semibold text-muted">{title}</div>
      {children}
    </div>
  )
}

/** 键值行（mono 值 + 截断悬浮全文）。 */
function KV({ label, value, mono = true }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-3 py-0.5 text-xs">
      <span className="shrink-0 text-muted">{label}</span>
      <span className={cn('min-w-0 break-all text-right text-ink', mono && 'font-mono text-[11px]')}>
        {value}
      </span>
    </div>
  )
}

/** 依赖服务名 → 已纳管系统的工作台链接（P2-2 浅拓扑；未纳管则纯文本）。 */
function ServiceRef({ service, apps }: { service: string; apps: MonitoredApp[] }) {
  const target = apps.find((item) => item.service === service)
  if (!target) return <span title="未纳管（仅声明依赖名）">{service}</span>
  return (
    <Link to={`/systems/${encodeURIComponent(target.id)}`} className="text-accent hover:underline">
      {service}
    </Link>
  )
}

/** 需求基线卡：最近一次分析会话的摘要（按系统过滤）。 */
function RequirementsCard({ appId }: { appId: string }) {
  const { data } = useQuery({
    queryKey: ['requirement-analyses', appId],
    queryFn: () => api.requirementAnalyses({ app_id: appId }),
    refetchInterval: 30000,
  })
  const latest: RequirementAnalysisSummary | undefined = [...(data?.analyses ?? [])].sort(
    (a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at),
  )[0]

  if (!latest) {
    return <div className="text-xs text-idle">暂无分析记录（在需求基线页发起）</div>
  }
  const releaseMeta =
    latest.approved != null ? RELEASE_STATUS_META[latest.release_status ?? ''] : undefined
  const statusText =
    latest.approved != null
      ? `${releaseMeta?.short ?? '已批准'}（v${latest.approved.version}）`
      : latest.status === 'analyzing'
        ? '分析中…'
        : latest.status === 'analyzed'
          ? `已分析（v${latest.current_version}）`
          : latest.status
  const statusCls =
    latest.approved != null
      ? (RELEASE_TEXT_CLS[latest.release_status ?? ''] ?? 'text-ok')
      : latest.status === 'analyzing'
        ? 'text-info'
        : 'text-muted'
  return (
    <div className="space-y-1 text-xs">
      <div className="line-clamp-2 text-ink" title={latest.entry_title}>
        {latest.entry_title}
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className={statusCls} title={releaseMeta?.title}>
          {statusText}
        </span>
        {latest.latest_confidence != null && (
          <span className="font-mono text-[11px] text-muted">置信 {latest.latest_confidence.toFixed(2)}</span>
        )}
      </div>
      {latest.approved?.wf_id && (
        <Link
          to={`/flows/${encodeURIComponent(latest.approved.wf_id)}`}
          className="block truncate font-mono text-[10px] text-accent hover:underline"
        >
          修复流程：{latest.approved.wf_id}
        </Link>
      )}
    </div>
  )
}

export function SystemWorkbench() {
  const { appId = '' } = useParams()

  const apps = useQuery({
    queryKey: ['monitored-apps'],
    queryFn: api.monitoredApps,
    refetchInterval: 15000,
  })
  const app: MonitoredApp | undefined = apps.data?.items.find((item) => item.id === appId)

  const flows = useQuery({
    queryKey: ['flows', { service: app?.service }],
    queryFn: () => api.flows({ service: app!.service, limit: 50 }),
    enabled: Boolean(app?.service),
    refetchInterval: 20000,
  })

  // P3⑨：活动时间线（服务端聚合——gate_events 翻译 + 运行态快照推导，替代逐流程拉详情）
  const timeline = useQuery({
    queryKey: ['system-timeline', appId],
    queryFn: () => api.systemTimeline(appId, { flows: 5 }),
    enabled: Boolean(appId),
    refetchInterval: 20000,
  })
  const [flowSort, setFlowSort] = useState<SortState<ColumnKey>>(DEFAULT_SORT)

  /** 点击表头：同列翻转升降；换列按直觉默认方向（开始时间降序，其余升序）。 */
  const toggleFlowSort = (key: ColumnKey) => {
    setFlowSort((prev) => nextSort(prev, key, key === 'start_time' ? 'desc' : 'asc'))
  }

  if (apps.isError) {
    return <EmptyState title="无法加载被监控系统清单" hint={describeError(apps.error)} />
  }
  if (!apps.data) return <div className="text-sm text-muted">加载中…</div>
  if (!app) {
    return (
      <EmptyState
        title={`未找到系统：${appId}`}
        hint="该系统可能已被删除或停用；请从「系统清单管理」或首页卡片墙进入"
      />
    )
  }

  const items: FlowItem[] = flows.data?.items ?? []
  const sortedItems = [...items].sort((a, b) =>
    compareValues(sortValue(a, flowSort.key), sortValue(b, flowSort.key), flowSort.dir),
  )
  const running = items.filter((item) => item.exec_status === 'RUNNING')
  const timelineGroups = timeline.data?.groups ?? []
  // 浅拓扑（P2-2）：上游依赖（清单声明）+ 下游调用方（反查全量清单）
  const allApps = apps.data.items
  const deps = app.depends_on ?? []
  const dependents = allApps.filter(
    (item) => item.service !== app.service && (item.depends_on ?? []).includes(app.service),
  )

  return (
    <div>
      {/* 页头 */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <AppHealthDot app={app} className="h-2 w-2" />
            <h1 className="truncate text-lg font-medium">{app.name}</h1>
            <span className="rounded-full border border-accent/40 px-2 py-0.5 font-mono text-[10px] text-accent">
              {app.service}
            </span>
          </div>
          <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted">
            <span className="font-mono">{app.url}</span>
            <span className="text-idle">·</span>
            <ProbeBadge app={app} />
            {app.watcher && app.enabled && (
              <>
                <span className="text-idle">·</span>
                <span className={app.watcher.ok ? 'text-muted' : 'text-danger'}>
                  {app.watcher.ok
                    ? `巡检正常${app.watcher.last_check_at ? ` · ${fmtHM(app.watcher.last_check_at)}` : ''}`
                    : `巡检连续失败 ${app.watcher.failures} 次`}
                </span>
              </>
            )}
            <span className="text-idle">·</span>
            <span>15 秒自动刷新</span>
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <a
            href={app.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 rounded-lg border border-line px-3.5 py-1.5 text-sm text-muted transition-colors hover:bg-elevated hover:text-ink"
          >
            进入应用
            <ArrowUpRight className="h-3.5 w-3.5" strokeWidth={1.8} />
          </a>
          <Link
            to="/monitored-apps"
            className="inline-flex items-center gap-1.5 rounded-lg border border-line px-3.5 py-1.5 text-sm text-muted transition-colors hover:bg-elevated hover:text-ink"
          >
            <Wrench className="h-3.5 w-3.5" strokeWidth={1.8} />
            清单中维护
          </Link>
        </div>
      </div>

      {/* 主体：主区（流程）+ 侧区（状态） */}
      <div className="mt-6 grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_300px]">
        <div className="space-y-4">
          {/* 进行中流程 */}
          <section className="rounded-xl border border-line bg-panel p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-medium">进行中流程 · {running.length}</h2>
              <Link to={`/flows?service=${encodeURIComponent(app.service)}`} className="text-xs text-idle hover:text-accent">
                全部流程 →
              </Link>
            </div>
            {flows.isError ? (
              <div className="text-xs text-danger">{describeError(flows.error)}</div>
            ) : running.length > 0 ? (
              <div className="space-y-2.5">
                {running.map((item) => (
                  <Link
                    key={item.wf_id}
                    to={`/flows/${encodeURIComponent(item.wf_id)}`}
                    className="block rounded-lg border border-line px-3 py-2.5 transition-colors hover:border-accent/40"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="flex min-w-0 items-center gap-2">
                        <StageBadge stage={item.stage} />
                        <span className="truncate font-mono text-xs text-muted" title={item.wf_id}>
                          {item.wf_id}
                        </span>
                      </span>
                      {item.deadline && <CountdownText at={item.deadline.at} className="shrink-0 text-sm" />}
                    </div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-idle">
                      {item.alert?.alert_id && <span className="font-mono">告警 {item.alert.alert_id}</span>}
                      {item.stage_error && <span className="text-danger">{item.stage_error}</span>}
                    </div>
                  </Link>
                ))}
              </div>
            ) : (
              <div className="text-xs text-idle">无进行中流程</div>
            )}
          </section>

          {/* 活动时间线（P3⑨：服务端聚合——gate_events 中文翻译 + 运行态快照推导） */}
          <section className="rounded-xl border border-line bg-panel p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-medium">活动时间线</h2>
              <span className="text-[11px] text-idle">按流程分组 · 运行中优先 · 最近 5 个</span>
            </div>
            {timeline.isError ? (
              <div className="text-xs text-danger">{describeError(timeline.error)}</div>
            ) : timelineGroups.length > 0 ? (
              <div className="space-y-3">
                {timelineGroups.map((group) => (
                  <div key={group.wf_id} className="overflow-hidden rounded-lg border border-line">
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line/60 bg-elevated/40 px-3 py-2">
                      <span className="flex min-w-0 items-center gap-2">
                        <StageBadge stage={group.stage} />
                        <Link
                          to={`/flows/${encodeURIComponent(group.wf_id)}`}
                          className="truncate font-mono text-[11px] text-muted transition-colors hover:text-accent"
                          title={group.wf_id}
                        >
                          {group.wf_id}
                        </Link>
                        {group.current && (
                          <span className="shrink-0 rounded-full border border-info/40 bg-info/10 px-1.5 py-0.5 text-[10px] text-info">
                            进行中
                          </span>
                        )}
                      </span>
                      <span className="shrink-0 font-mono text-[10px] text-idle">
                        {fmtDateTime(group.start_time)}
                        {group.close_time ? ` → ${fmtDateTime(group.close_time)}` : ''}
                        {group.duration_seconds != null ? ` · ${fmtDuration(group.duration_seconds)}` : ''}
                      </span>
                    </div>
                    <ol className="px-3.5 py-2">
                      {group.events.map((event) => (
                        <li key={event.seq} className="flex items-start gap-2.5 py-[3px] text-xs">
                          <span
                            className={cn('mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full', TONE_DOT[event.tone])}
                          />
                          <span className={cn('min-w-0 flex-1', TONE_TEXT[event.tone])}>{event.text}</span>
                          {event.at && (
                            <span className="shrink-0 font-mono text-[10px] text-idle">{fmtTime(event.at)}</span>
                          )}
                        </li>
                      ))}
                    </ol>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-idle">
                暂无活动记录（新告警或需求触发后，修复全链路的闸门事件将在这里展开）
              </div>
            )}
          </section>

          {/* 流程历史表 */}
          <section className="overflow-hidden rounded-xl border border-line bg-panel">
            <div className="border-b border-line px-4 py-3 text-sm font-medium">流程历史（最近 {Math.min(items.length, 15)} 条）</div>
            {items.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-line text-left text-muted">
                      <SortableTh
                        label="工作流"
                        colKey="wf_id"
                        sort={flowSort}
                        onToggle={toggleFlowSort}
                        padding="px-4 py-2.5"
                      />
                      <SortableTh
                        label="告警"
                        colKey="alert_id"
                        sort={flowSort}
                        onToggle={toggleFlowSort}
                        padding="px-4 py-2.5"
                      />
                      <SortableTh
                        label="阶段"
                        colKey="stage"
                        sort={flowSort}
                        onToggle={toggleFlowSort}
                        padding="px-4 py-2.5"
                      />
                      <SortableTh
                        label="耗时"
                        colKey="duration"
                        sort={flowSort}
                        onToggle={toggleFlowSort}
                        padding="px-4 py-2.5"
                      />
                      <SortableTh
                        label="开始时间"
                        colKey="start_time"
                        sort={flowSort}
                        onToggle={toggleFlowSort}
                        padding="px-4 py-2.5"
                      />
                      <th className="px-4 py-2.5 text-right font-medium">详情</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedItems.slice(0, 15).map((item) => (
                      <tr key={item.wf_id} className="border-b border-line/60 last:border-0">
                        <td className="max-w-[14rem] px-4 py-2.5">
                          <span className="block truncate font-mono text-[11px] text-muted" title={item.wf_id}>
                            {item.wf_id}
                          </span>
                        </td>
                        <td className="px-4 py-2.5 font-mono text-[11px] text-muted">{item.alert?.alert_id ?? '—'}</td>
                        <td className="px-4 py-2.5">
                          <StageBadge stage={item.stage} />
                        </td>
                        <td className="px-4 py-2.5 font-mono text-[11px] text-muted">
                          {item.exec_status === 'RUNNING' ? (
                            <span className="text-info">运行中</span>
                          ) : (
                            fmtDuration(item.duration_seconds)
                          )}
                        </td>
                        <td className="px-4 py-2.5 text-[11px] text-idle">
                          {fmtTime(item.start_time)} {item.start_time ? fmtDateTime(item.start_time).slice(0, 5) : ''}
                        </td>
                        <td className="px-4 py-2.5 text-right">
                          <Link to={`/flows/${encodeURIComponent(item.wf_id)}`} className="text-accent hover:underline">
                            详情
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="p-6">
                <EmptyState title="暂无流程记录" hint="该系统还没有触发过修复流程。" />
              </div>
            )}
          </section>
        </div>

        {/* 侧区 */}
        <div className="space-y-4">
          <SideCard title="实时健康">
            <div className="space-y-1">
              <KV label="探活" value={<ProbeBadge app={app} />} mono={false} />
              <KV
                label="巡检"
                value={
                  app.watcher
                    ? app.watcher.ok
                      ? `正常（连续失败 0）`
                      : `连续失败 ${app.watcher.failures} 次`
                    : '未运行'
                }
                mono={false}
              />
              {app.watcher?.last_check_at && <KV label="最近巡检" value={fmtHM(app.watcher.last_check_at)} />}
              {app.watcher?.last_error && (
                <div className="mt-1 break-all text-[10px] text-danger" title={app.watcher.last_error}>
                  {app.watcher.last_error}
                </div>
              )}
              {app.watcher?.alerted && app.watcher.alert_id && (
                <KV label="已触发告警" value={app.watcher.alert_id} />
              )}
            </div>
          </SideCard>

          <SideCard title="数据链路">
            {app.readiness ? (
              <ReadinessLine readiness={app.readiness} />
            ) : (
              <div className="text-xs text-idle">就绪度数据不可用</div>
            )}
            {app.watcher && app.enabled && <div className="mt-1.5"><WatcherLine watcher={app.watcher} /></div>}
          </SideCard>

          <SideCard title="配置摘要">
            <div className="space-y-1">
              <KV label="service" value={app.service} />
              {app.log_path && <KV label="日志路径" value={<span title={app.log_path}>{app.log_path}</span>} />}
              {app.probe_keyword && (
                <KV label="健康关键字" value={<span title={app.probe_keyword}>{app.probe_keyword}</span>} />
              )}
              {app.health_path && <KV label="健康路径" value={app.health_path} />}
              {app.repo && <KV label="修复仓库" value={<span title={app.repo}>{app.repo}</span>} />}
              <KV
                label="依赖服务"
                value={
                  deps.length > 0 ? (
                    <span title={deps.join(', ')}>
                      {deps.map((svc, i) => (
                        <span key={svc}>
                          {i > 0 && '、'}
                          <ServiceRef service={svc} apps={allApps} />
                        </span>
                      ))}
                    </span>
                  ) : (
                    <span className="text-idle">未声明</span>
                  )
                }
                mono={false}
              />
              {dependents.length > 0 && (
                <KV
                  label="被依赖"
                  value={
                    <span title={dependents.map((d) => d.service).join(', ')}>
                      {dependents.map((d, i) => (
                        <span key={d.id}>
                          {i > 0 && '、'}
                          <Link
                            to={`/systems/${encodeURIComponent(d.id)}`}
                            className="text-accent hover:underline"
                          >
                            {d.service}
                          </Link>
                        </span>
                      ))}
                    </span>
                  }
                  mono={false}
                />
              )}
            </div>
            <Link
              to="/monitored-apps"
              className="mt-2.5 block rounded-lg border border-line py-1.5 text-center text-xs text-muted transition-colors hover:bg-elevated hover:text-ink"
            >
              编辑配置（清单页）
            </Link>
          </SideCard>

          <SideCard title="需求基线">
            <RequirementsCard appId={app.id} />
            <Link
              to="/requirements"
              className="mt-2.5 block rounded-lg border border-line py-1.5 text-center text-xs text-muted transition-colors hover:bg-elevated hover:text-ink"
            >
              查看需求基线 →
            </Link>
          </SideCard>
        </div>
      </div>

      {!app.enabled && (
        <div className="mt-4 rounded-lg border border-idle/40 bg-idle/10 px-3 py-2 text-xs text-muted">
          该系统已停用：不参与探测 / 巡检 / 自动修复；可在「系统清单管理」中重新启用。
        </div>
      )}
    </div>
  )
}
