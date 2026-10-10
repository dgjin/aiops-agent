/** 系统工作台（/systems/:appId）：以单个被监控系统为视角的一屏聚合。
 *
 * 三级钻取的第二级：健康 / 进行中流程 / 最近活动 / 流程历史 / 数据链路 /
 * 配置摘要 / 需求基线——全部围绕「该系统」组织，数据来自现有 API：
 * - monitored-apps 单条（probe / watcher / readiness）
 * - flows?service=X（后端已支持按系统过滤）
 * - requirements/analyses?app_id=X（后端已支持按系统过滤）
 */

import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowUpRight, Wrench } from 'lucide-react'
import { api, describeError } from '../lib/api'
import { cn, fmtDateTime, fmtDuration, fmtTime } from '../lib/format'
import {
  AppHealthDot,
  ProbeBadge,
  ReadinessLine,
  WatcherLine,
  fmtHM,
} from '../components/MonitoredAppStatus'
import { CountdownText } from '../components/CountdownText'
import { EmptyState } from '../components/EmptyState'
import { StageBadge } from '../components/StageBadge'
import type { FlowItem, MonitoredApp, RequirementAnalysisSummary } from '../lib/types'

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
  const statusText =
    latest.approved != null
      ? `已批准（v${latest.approved.version}）`
      : latest.status === 'analyzing'
        ? '分析中…'
        : latest.status === 'analyzed'
          ? `已分析（v${latest.current_version}）`
          : latest.status
  const statusCls = latest.approved != null ? 'text-ok' : latest.status === 'analyzing' ? 'text-info' : 'text-muted'
  return (
    <div className="space-y-1 text-xs">
      <div className="line-clamp-2 text-ink" title={latest.entry_title}>
        {latest.entry_title}
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className={statusCls}>{statusText}</span>
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
  const running = items.filter((item) => item.exec_status === 'RUNNING')
  const recent = items
    .filter((item) => item.exec_status !== 'RUNNING')
    .sort((a, b) => Date.parse(b.close_time ?? b.start_time ?? '') - Date.parse(a.close_time ?? a.start_time ?? ''))
    .slice(0, 6)

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

          {/* 最近活动（终态流程） */}
          <section className="rounded-xl border border-line bg-panel p-4">
            <h2 className="mb-3 text-sm font-medium">最近活动</h2>
            {recent.length > 0 ? (
              <div className="space-y-2">
                {recent.map((item) => (
                  <Link
                    key={item.wf_id}
                    to={`/flows/${encodeURIComponent(item.wf_id)}`}
                    className="flex items-center gap-3 text-xs transition-colors hover:text-accent"
                  >
                    <span className="w-24 shrink-0 font-mono text-[11px] text-idle">
                      {fmtDateTime(item.close_time ?? item.start_time)}
                    </span>
                    <StageBadge stage={item.stage} />
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted" title={item.wf_id}>
                      {item.wf_id}
                    </span>
                    <span className="shrink-0 font-mono text-[11px] text-idle">{fmtDuration(item.duration_seconds)}</span>
                  </Link>
                ))}
              </div>
            ) : (
              <div className="text-xs text-idle">暂无历史流程（新告警或需求触发后出现在这里）</div>
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
                      <th className="px-4 py-2.5 font-medium">工作流</th>
                      <th className="px-4 py-2.5 font-medium">告警</th>
                      <th className="px-4 py-2.5 font-medium">阶段</th>
                      <th className="px-4 py-2.5 font-medium">耗时</th>
                      <th className="px-4 py-2.5 font-medium">开始时间</th>
                      <th className="px-4 py-2.5 text-right font-medium">详情</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.slice(0, 15).map((item) => (
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
