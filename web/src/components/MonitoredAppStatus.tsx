/** 被监控应用状态展示组件（健康点 / 探测 / 巡检 / 就绪度）。
 *
 * 2026-10 UI 重构：从 MonitoredApps 页抽出——系统清单、系统总览卡片墙、
 * 系统工作台侧栏三处共用同一套判定，避免各处口径漂移。
 */

import type { MonitoredApp, MonitoredAppReadiness, MonitoredAppWatcher } from '../lib/types'
import { cn } from '../lib/format'

/** 应用总体健康语义：停用=idle（灰）、探测正常=ok（绿）、否则 danger（红，脉冲）。 */
export type AppHealth = 'ok' | 'idle' | 'danger'

export function appHealth(app: MonitoredApp): AppHealth {
  if (!app.enabled) return 'idle'
  return app.probe.running ? 'ok' : 'danger'
}

/** 健康圆点（侧栏动态系统列表 / 总览卡片 / 工作台页头共用同一判定）。 */
export function AppHealthDot({ app, className }: { app: MonitoredApp; className?: string }) {
  const health = appHealth(app)
  return (
    <span
      className={cn(
        'h-1.5 w-1.5 shrink-0 rounded-full',
        health === 'ok' ? 'bg-ok' : health === 'idle' ? 'bg-idle' : 'bg-danger animate-pulse',
        className,
      )}
    />
  )
}

/** 探测状态徽标：在线（含状态码）/ 不可达 / 已停用。 */
export function ProbeBadge({ app }: { app: MonitoredApp }) {
  if (!app.enabled) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-idle">
        <span className="h-1.5 w-1.5 rounded-full bg-idle" />
        已停用
      </span>
    )
  }
  const { running, status_code, latency_ms } = app.probe
  return (
    <span className={cn('inline-flex items-center gap-1.5 text-xs', running ? 'text-muted' : 'text-danger')}>
      <span className={cn('h-1.5 w-1.5 rounded-full', running ? 'bg-ok' : 'bg-danger animate-pulse')} />
      {running ? `在线 ${status_code ?? ''}${latency_ms != null ? ` · ${latency_ms}ms` : ''}` : '不可达'}
    </span>
  )
}

/** 巡检时间仅取 HH:MM（本地时区显示）。 */
export function fmtHM(iso?: string): string {
  if (!iso) return ''
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return ''
  return `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`
}

/** 主动巡检状态行：连续失败计数 / 已自动触发修复流程（app_prober.py 落盘数据）。 */
export function WatcherLine({ watcher }: { watcher: MonitoredAppWatcher }) {
  if (watcher.alerted) {
    return (
      <div
        className="mt-0.5 text-[10px] text-danger"
        title={`告警 ID：${watcher.alert_id ?? '-'}${watcher.last_error ? `；最近错误：${watcher.last_error}` : ''}`}
      >
        巡检：连续失败 {watcher.failures} 次 · 已自动触发修复流程
      </div>
    )
  }
  if (!watcher.ok) {
    return (
      <div className="mt-0.5 text-[10px] text-muted" title={watcher.last_error ?? ''}>
        巡检：连续失败 {watcher.failures} 次，达到阈值后自动触发修复
      </div>
    )
  }
  return (
    <div className="mt-0.5 text-[10px] text-idle">
      巡检正常{watcher.last_check_at ? ` · ${fmtHM(watcher.last_check_at)}` : ''}
    </div>
  )
}

/** 就绪度圆点：null=灰（未配置）/ true=绿 / false=红（tone=warn 时黄，用于「待自动构建」类非异常状态）。 */
export function ReadyDot({ state, tone = 'status' }: { state: boolean | null; tone?: 'status' | 'warn' }) {
  const cls =
    state === null ? 'bg-idle' : state ? 'bg-ok' : tone === 'warn' ? 'bg-warn' : 'bg-danger'
  return <span className={cn('h-1 w-1 shrink-0 rounded-full', cls)} />
}

/** 接入就绪度行：仓库 / 索引 / 日志三态（前台添加后的自动适配状态一览）。 */
export function ReadinessLine({ readiness }: { readiness: MonitoredAppReadiness }) {
  const { repo_ok, index_ok, log_files } = readiness
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px]">
      <span
        className={cn(
          'inline-flex items-center gap-1',
          repo_ok === null ? 'text-idle' : repo_ok ? 'text-muted' : 'text-danger',
        )}
        title={
          repo_ok === null
            ? '未配置修复仓库：不参与自动修复（告警 / 日志 / 探测照常生效）'
            : repo_ok
              ? '修复仓库目录存在：修复引擎可定位并生成补丁'
              : '配置的修复仓库目录不存在：请检查路径，否则修复将回退默认行为'
        }
      >
        <ReadyDot state={repo_ok} />
        {repo_ok === null ? '仓库未配置' : repo_ok ? '修复仓库就绪' : '修复仓库缺失'}
      </span>
      {repo_ok === true && (
        <span
          className={cn('inline-flex items-center gap-1', index_ok ? 'text-muted' : 'text-warn')}
          title={
            index_ok
              ? '应用专属代码索引已存在（仓库变更时首次检索自动重建）'
              : '专属索引将在首次修复检索时自动构建，无需手工建库或重启'
          }
        >
          <ReadyDot state={index_ok} tone="warn" />
          {index_ok ? '索引就绪' : '索引待建（首次检索自动构建）'}
        </span>
      )}
      <span
        className={cn(
          'inline-flex items-center gap-1',
          log_files === null ? 'text-idle' : log_files > 0 ? 'text-muted' : 'text-danger',
        )}
        title={
          log_files === null
            ? '未配置日志路径：不采集日志（突增检测不含该应用）'
            : log_files > 0
              ? `采集器可命中 ${log_files} 个日志文件（通配已展开）`
              : '日志路径当前未命中任何文件：请检查路径或应用是否已产生日志'
        }
      >
        <ReadyDot state={log_files === null ? null : log_files > 0} />
        {log_files === null ? '日志未配置' : log_files > 0 ? `日志就绪（${log_files} 个文件）` : '日志路径未命中'}
      </span>
    </div>
  )
}
