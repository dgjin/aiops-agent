/** 系统总览（新首页）：以被监控系统为第一视角。
 *
 * 三级钻取的第一级：平台健康带（只读）→ 需要注意（异常前置）→ 系统卡片墙
 * （健康 / 活跃流程 / 就绪度）→ 全局态势（折叠，原流程看板能力完整保留）。
 * 数据全部来自现有 API（overview / monitored-apps / health / system / subsystems）。
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  CheckCircle2,
  ChevronRight,
  ClipboardCheck,
  Inbox,
  Timer,
  TriangleAlert,
} from 'lucide-react'
import { api, describeError } from '../lib/api'
import { cn } from '../lib/format'
import {
  AppHealthDot,
  ProbeBadge,
  ReadinessLine,
  WatcherLine,
  appHealth,
  type AppHealth,
} from '../components/MonitoredAppStatus'
import { CountdownText } from '../components/CountdownText'
import { EmptyState } from '../components/EmptyState'
import { GlobalSituation } from '../components/GlobalSituation'
import { StageBadge } from '../components/StageBadge'
import type { FlowItem, MonitoredApp, OverviewCounts } from '../lib/types'

/** 平台健康 chip（只读；异常自动变红/黄）。 */
function Chip({
  tone,
  label,
  value,
}: {
  tone: AppHealth | 'warn'
  label: string
  value: string
}) {
  const dot =
    tone === 'ok' ? 'bg-ok' : tone === 'warn' ? 'bg-warn' : tone === 'idle' ? 'bg-idle' : 'bg-danger animate-pulse'
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-panel px-3 py-1.5 text-xs text-muted">
      <span className={cn('h-1.5 w-1.5 rounded-full', dot)} />
      {label} <b className="font-medium text-ink">{value}</b>
    </span>
  )
}

/** 平台健康带：Temporal / Kill switch / 修复链路 / BFF（点击尾部链接去平台状态页）。 */
function HealthStrip() {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 15000, retry: false })
  const system = useQuery({ queryKey: ['system'], queryFn: api.system, refetchInterval: 15000 })
  const subsystems = useQuery({ queryKey: ['subsystems'], queryFn: api.subsystems, refetchInterval: 10000 })

  const temporalOk = health.data?.temporal.connected ?? false
  const killActive = Boolean(system.data?.kill_switch.state?.active)
  const degraded = subsystems.data?.degraded_mode ?? false
  const degradedNames = subsystems.data?.degraded ?? []
  const bffError = health.isError

  return (
    <div className="mt-5 flex flex-wrap items-center gap-2">
      <Chip
        tone={temporalOk ? 'ok' : 'danger'}
        label="Temporal"
        value={temporalOk ? '正常' : health.data ? '断开' : '连接中…'}
      />
      <Chip tone={killActive ? 'danger' : 'ok'} label="Kill switch" value={killActive ? '已激活' : '未激活'} />
      <Chip
        tone={degraded ? 'warn' : 'ok'}
        label="修复链路"
        value={degraded ? `降级（${degradedNames.join('、')}）` : '正常'}
      />
      <Chip tone={bffError ? 'danger' : 'ok'} label="BFF" value={bffError ? '不可达' : '正常'} />
      <Link to="/system" className="ml-auto text-xs text-idle transition-colors hover:text-accent">
        平台自身状态 →
      </Link>
    </div>
  )
}

/** 「需要注意」单行（左侧 3px 色条 + 图标 + 文案 + 行动链接）。 */
function AttentionRow({
  tone,
  icon,
  text,
  action,
  to,
}: {
  tone: 'warn' | 'danger'
  icon: typeof Timer
  text: React.ReactNode
  action: string
  to: string
}) {
  const Icon = icon
  return (
    <Link
      to={to}
      className={cn(
        'flex items-center gap-3 rounded-xl border border-line border-l-[3px] bg-panel px-3.5 py-2.5 text-sm transition-colors hover:border-accent/40',
        tone === 'danger' ? 'border-l-danger' : 'border-l-warn',
      )}
    >
      <Icon
        className={cn('h-4 w-4 shrink-0', tone === 'danger' ? 'text-danger' : 'text-warn')}
        strokeWidth={1.8}
      />
      <span className="min-w-0 flex-1 text-muted">{text}</span>
      <span className="shrink-0 text-xs text-accent">{action} →</span>
    </Link>
  )
}

/** 需要注意区：探测/巡检异常 → 转人工 → 待审批 → 公告窗口；全绿时收成一行。 */
function AttentionSection({
  apps,
  counts,
  running,
}: {
  apps: MonitoredApp[]
  counts: OverviewCounts
  running: FlowItem[]
}) {
  const rows: React.ReactNode[] = []

  for (const app of apps) {
    if (!app.enabled) continue
    if (!app.probe.running) {
      rows.push(
        <AttentionRow
          key={`probe-${app.id}`}
          tone="danger"
          icon={TriangleAlert}
          text={
            <>
              <b className="font-medium text-ink">{app.name}</b> 探测不可达
              {app.watcher ? `（连续失败 ${app.watcher.failures} 次` : '（'}
              {app.watcher ? '，达阈值将自动触发修复流程）' : ''}
            </>
          }
          action="进入工作台"
          to={`/systems/${encodeURIComponent(app.id)}`}
        />,
      )
    } else if (app.watcher?.alerted) {
      rows.push(
        <AttentionRow
          key={`watcher-${app.id}`}
          tone="danger"
          icon={TriangleAlert}
          text={
            <>
              <b className="font-medium text-ink">{app.name}</b> 巡检连续失败 {app.watcher.failures} 次
              {app.watcher.alert_id ? ` · 已自动触发修复（${app.watcher.alert_id}）` : ' · 已自动触发修复流程'}
            </>
          }
          action="进入工作台"
          to={`/systems/${encodeURIComponent(app.id)}`}
        />,
      )
    }
  }

  if (counts.escalations_open > 0) {
    rows.push(
      <AttentionRow
        key="escalations"
        tone="warn"
        icon={Inbox}
        text={
          <>
            <b className="font-medium text-ink">{counts.escalations_open} 个转人工待办</b> 等待处置
          </>
        }
        action="前往处置"
        to="/escalations"
      />,
    )
  }

  if (counts.wait_approval > 0) {
    const earliest = running
      .filter((item) => item.stage === 'WAIT_APPROVAL' && item.deadline)
      .sort((a, b) => Date.parse(a.deadline!.at) - Date.parse(b.deadline!.at))[0]
    rows.push(
      <AttentionRow
        key="approvals"
        tone="warn"
        icon={ClipboardCheck}
        text={
          <>
            <b className="font-medium text-ink">{counts.wait_approval} 个流程等待审批</b>
            {earliest && (
              <>
                {' '}
                最近超时 <CountdownText at={earliest.deadline!.at} className="text-sm" />
              </>
            )}
          </>
        }
        action="前往审批"
        to="/approvals"
      />,
    )
  }

  if (counts.notifying > 0) {
    const earliest = running
      .filter((item) => item.stage === 'NOTIFYING' && item.deadline)
      .sort((a, b) => Date.parse(a.deadline!.at) - Date.parse(b.deadline!.at))[0]
    rows.push(
      <AttentionRow
        key="window"
        tone="warn"
        icon={Timer}
        text={
          <>
            <b className="font-medium text-ink">{counts.notifying} 个发布窗口计时中</b>
            {earliest && (
              <>
                {' '}
                最近到期 <CountdownText at={earliest.deadline!.at} className="text-sm" />
              </>
            )}
            ，到期自动金丝雀发布
          </>
        }
        action="前往窗口"
        to="/window"
      />,
    )
  }

  return (
    <section className="mt-6">
      <h2 className="mb-3 text-sm font-medium text-muted">
        需要注意{rows.length > 0 ? ` · ${rows.length} 项` : ''}
      </h2>
      {rows.length > 0 ? (
        <div className="space-y-2">{rows}</div>
      ) : (
        <div className="flex items-center gap-2 rounded-xl border border-ok/30 bg-ok/5 px-3.5 py-2.5 text-sm text-ok">
          <CheckCircle2 className="h-4 w-4 shrink-0" strokeWidth={1.8} />
          所有系统运行正常，无待办事项
        </div>
      )}
    </section>
  )
}

/** 系统卡片（健康点 + 活跃流程带 + 就绪度 + 进入工作台）。 */
function SystemCard({ app, running }: { app: MonitoredApp; running: FlowItem[] }) {
  const active = running.filter((item) => item.alert?.service === app.service)
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-line bg-panel p-4 transition-colors hover:border-accent/40">
      <div className="flex items-start gap-2.5">
        <AppHealthDot app={app} className="mt-[7px]" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[15px] font-semibold text-ink">{app.name}</div>
          <div className="mt-0.5 truncate font-mono text-[11px] text-idle" title={app.url}>
            {app.service} · {app.url}
          </div>
        </div>
        <ProbeBadge app={app} />
      </div>

      {/* 活跃流程带：无进行中流程时灰字占位，保持卡片高度节奏一致 */}
      <div className="space-y-1.5 rounded-lg border border-dashed border-line px-3 py-2.5">
        {active.length > 0 ? (
          active.map((item) => (
            <Link
              key={item.wf_id}
              to={`/flows/${encodeURIComponent(item.wf_id)}`}
              className="flex items-center justify-between gap-2 transition-colors hover:text-accent"
            >
              <span className="flex min-w-0 items-center gap-2">
                <StageBadge stage={item.stage} />
                <span className="truncate font-mono text-[11px] text-muted" title={item.wf_id}>
                  {item.wf_id}
                </span>
              </span>
              {item.deadline && <CountdownText at={item.deadline.at} className="shrink-0 text-sm" />}
            </Link>
          ))
        ) : (
          <div className="text-xs text-idle">无进行中流程</div>
        )}
      </div>

      {app.enabled && app.watcher && !app.watcher.ok && <WatcherLine watcher={app.watcher} />}
      {app.readiness && <ReadinessLine readiness={app.readiness} />}

      <div className="mt-auto flex justify-end">
        <Link
          to={`/systems/${encodeURIComponent(app.id)}`}
          className="rounded-lg border border-accent/40 px-3.5 py-1.5 text-sm font-medium text-accent transition-colors hover:bg-accent/10"
        >
          进入工作台 →
        </Link>
      </div>
    </div>
  )
}

export function SystemOverview() {
  const [showSituation, setShowSituation] = useState(false)
  const overview = useQuery({
    queryKey: ['overview'],
    queryFn: api.overview,
    refetchInterval: 5000,
  })
  const apps = useQuery({
    queryKey: ['monitored-apps'],
    queryFn: api.monitoredApps,
    refetchInterval: 15000,
  })

  if (apps.isError) {
    return <EmptyState title="无法加载被监控系统清单" hint={describeError(apps.error)} />
  }
  if (!overview.data || !apps.data) return <div className="text-sm text-muted">加载中…</div>

  // 异常前置排序：不可达（danger）→ 正常（ok）→ 停用（idle）
  const order: Record<AppHealth, number> = { danger: 0, ok: 1, idle: 2 }
  const sorted = [...apps.data.items].sort((a, b) => order[appHealth(a)] - order[appHealth(b)])

  return (
    <div>
      <h1 className="text-lg font-medium">系统总览</h1>
      <p className="mt-1 text-xs text-muted">以被监控系统为第一视角 · 异常前置排序 · 5 秒自动刷新</p>

      <HealthStrip />

      <AttentionSection
        apps={apps.data.items}
        counts={overview.data.counts}
        running={overview.data.running}
      />

      <section className="mt-7">
        <h2 className="mb-3 text-sm font-medium text-muted">
          被监控系统 · {sorted.length} 个
          <Link to="/monitored-apps" className="ml-3 text-xs font-normal text-idle hover:text-accent">
            清单管理 →
          </Link>
        </h2>
        {sorted.length > 0 ? (
          <div className="grid gap-3.5 md:grid-cols-2 xl:grid-cols-3">
            {sorted.map((app) => (
              <SystemCard key={app.id} app={app} running={overview.data.running} />
            ))}
          </div>
        ) : (
          <EmptyState title="暂无被监控系统" hint="在「系统清单管理」中新增，或用标准接口自动探测导入" />
        )}
      </section>

      <section className="mt-8">
        <button
          type="button"
          onClick={() => setShowSituation((prev) => !prev)}
          className="flex w-full items-center justify-between rounded-xl border border-line bg-panel px-4 py-3 text-sm text-muted transition-colors hover:border-accent/35"
        >
          <span>全局态势 · 流程视角统计（近 7 日终态分布 / MTTR / 自动修复率 / 闸门拦截率）— 默认收起</span>
          <ChevronRight
            className={cn('h-4 w-4 shrink-0 text-idle transition-transform', showSituation && 'rotate-90')}
            strokeWidth={1.8}
          />
        </button>
        {showSituation && (
          <div className="mt-4">
            <GlobalSituation />
          </div>
        )}
      </section>
    </div>
  )
}
