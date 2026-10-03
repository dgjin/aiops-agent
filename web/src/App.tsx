/** 应用外壳：左侧导航 + 服务器时钟/连接点 + 路由（设计方案 7.1、7.5）。 */

import { useState } from 'react'
import { NavLink, Outlet, Route, Routes } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ClipboardCheck,
  History,
  LayoutDashboard,
  Radar,
  Server,
  Timer,
} from 'lucide-react'
import { ApiError, api, getToken, normalizeToken, setToken, serverNow } from './lib/api'
import { cn, fmtDateTime } from './lib/format'
import { useNowTick } from './lib/hooks'
import { useWriteAction } from './lib/actions'
import { ConfirmDialog } from './components/ConfirmDialog'
import { Toast } from './components/Toast'
import { EmptyState } from './components/EmptyState'
import { Dashboard } from './pages/Dashboard'
import { Flows } from './pages/Flows'
import { FlowDetail } from './pages/FlowDetail'
import { Approvals } from './pages/Approvals'
import { WindowPage } from './pages/WindowPage'
import { Audit } from './pages/Audit'
import { MonitoredApps } from './pages/MonitoredApps'
import { SystemPage } from './pages/SystemPage'
import type { OverviewCounts } from './lib/types'

interface NavItem {
  to: string
  label: string
  icon: typeof LayoutDashboard
  end?: boolean
  badgeKey?: keyof OverviewCounts
}

const NAV_ITEMS: NavItem[] = [
  { to: '/', label: '流程看板', icon: LayoutDashboard, end: true },
  { to: '/flows', label: '流程列表', icon: Activity },
  { to: '/approvals', label: '审批中心', icon: ClipboardCheck, badgeKey: 'wait_approval' },
  { to: '/window', label: '发布窗口', icon: Timer, badgeKey: 'notifying' },
  { to: '/audit', label: '审计回看', icon: History },
  { to: '/monitored-apps', label: '被监控应用', icon: Radar },
  { to: '/system', label: '系统状态', icon: Server },
]

/** 服务器校准时钟（底部常驻）。 */
function ServerClock() {
  useNowTick()
  return (
    <span className="font-mono text-xs tabular-nums text-muted">
      {new Date(serverNow()).toLocaleTimeString('zh-CN', { hour12: false })}
    </span>
  )
}

/** BFF / Temporal 连接点（15 秒探测）。
 *
 * 注意：必须区分「网络不可达」与「HTTP 错误」。此前把任何错误都显示成
 * 「BFF 不可达」，令牌缺失/过期（401）时会把排查方向带偏到网络。
 */
function ConnectionDot() {
  const { data, isError, error } = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    refetchInterval: 15000,
    retry: false,
  })

  const status = error instanceof ApiError ? error.status : null
  const state = isError
    ? status === 401
      ? { dot: 'bg-danger', text: '未授权（请设置访问令牌）' }
      : status === 403
        ? { dot: 'bg-danger', text: '权限不足' }
        : status === 503
          ? { dot: 'bg-danger', text: '鉴权未配置' }
          : { dot: 'bg-danger', text: 'BFF 不可达' }
    : !data
      ? { dot: 'bg-idle', text: '连接中…' }
      : data.temporal.connected
        ? { dot: 'bg-ok', text: 'Temporal 正常' }
        : { dot: 'bg-danger', text: 'Temporal 断开' }
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted">
      <span
        className={cn('h-1.5 w-1.5 rounded-full', state.dot, state.dot !== 'bg-ok' && 'animate-pulse')}
      />
      {state.text}
    </span>
  )
}

/** 把秒数说成人话（轮换间隔 / 宽限期）。 */
function humanSeconds(seconds: number): string {
  if (!seconds) return '—'
  if (seconds % 86400 === 0) return `${seconds / 86400} 天`
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`
  return `${seconds} 秒`
}

/** 访问令牌（BFF 鉴权）：本机保存；展示**自动轮换**状态与自身令牌有效期。 */
function TokenControl() {
  const [token, setTokenState] = useState(() => getToken())
  const [saved, setSaved] = useState(false)
  const masked = token ? `${token.slice(0, 4)}••••${token.slice(-2)}` : ''

  const { data } = useQuery({
    queryKey: ['auth-status'],
    queryFn: api.authStatus,
    refetchInterval: 30000,
    retry: false,
  })
  const write = useWriteAction()

  const save = () => {
    const normalized = normalizeToken(token)
    setToken(normalized) // 自动去掉 Bearer 前缀 / 首尾引号与空白
    setTokenState(normalized)
    setSaved(true)
    setTimeout(() => setSaved(false), 2500)
    // 令牌变化后重新拉取全部数据（原请求均为未授权状态）
    window.location.reload()
  }

  const self = data?.self_token
  const inGrace = self?.state === 'previous'

  return (
    <div className="space-y-1.5">
      <div className="text-[10px] text-idle">
        访问令牌{' '}
        {token ? (
          <span className="font-mono text-ok">已设置 {masked}</span>
        ) : (
          <span className="text-danger">未设置</span>
        )}
      </div>
      <div className="flex gap-1.5">
        <input
          type="password"
          value={token}
          onChange={(e) => setTokenState(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && save()}
          placeholder="Bearer token"
          className="min-w-0 flex-1 rounded-md border border-line bg-canvas px-2 py-1 font-mono text-[11px] text-ink placeholder:text-idle focus:border-accent/50 focus:outline-none"
        />
        <button
          type="button"
          onClick={save}
          className="shrink-0 rounded-md border border-line px-2 py-1 text-[11px] text-muted hover:bg-elevated hover:text-ink"
        >
          {saved ? '已保存' : '保存'}
        </button>
      </div>

      {data && (
        <div className="space-y-0.5 text-[10px] text-idle">
          <div>
            自动轮换{' '}
            {data.auto_rotation_enabled ? (
              <span className="text-muted">每 {humanSeconds(data.interval_seconds)}</span>
            ) : (
              <span className="text-muted">已关闭</span>
            )}
          </div>
          {data.next_rotation_at && (
            <div>
              下次 <span className="font-mono">{fmtDateTime(data.next_rotation_at)}</span>
            </div>
          )}
          {inGrace && (
            <div className="text-warn">
              当前令牌已轮换、处于宽限期（{humanSeconds(data.grace_seconds)}），请尽快更新
            </div>
          )}
          {data.allow_reveal && (
            <div className="text-warn">已开启接口回显令牌（AIOPS_TOKEN_ALLOW_REVEAL=true）</div>
          )}
          {/* 仅在自动轮换开启时提供手动轮换入口：轮换关闭的部署里，点它只会把令牌换成
              "只写进 600 注册表文件"的新值、界面又取不回来，反而把人锁在门外。
              需要恢复该入口：把 AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS 设为大于 0 并重启 BFF。 */}
          {data.auto_rotation_enabled && self?.role === 'admin' && (
            <button
              type="button"
              onClick={() =>
                write.open({
                  title: '立即轮换所有访问令牌？',
                  confirmLabel: '轮换',
                  detail: (
                    <div className="space-y-1.5 text-xs">
                      <div className="text-muted">
                        将为全部身份生成新令牌；**旧令牌在宽限期（
                        {humanSeconds(data.grace_seconds)}）内仍然有效**，因此当前会话不会立刻失效。
                      </div>
                      <div className="text-muted">
                        新令牌写入 <span className="font-mono">{data.registry_path}</span>（权限 600）
                        {data.allow_reveal ? '，并会在此后提示中回显一次。' : '，需从该文件读取。'}
                      </div>
                    </div>
                  ),
                  run: () => api.rotateTokens('manual'),
                  success: '已轮换令牌（旧令牌宽限期内仍可用）',
                })
              }
              className="underline hover:text-ink"
            >
              立即轮换
            </button>
          )}
        </div>
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

/** 未授权 / 鉴权未配置横幅：与「BFF 不可达」区分开，直接给出该做的事。
 *
 * （复用 ConnectionDot 的 health 查询，不额外发请求）
 */
function AuthBanner() {
  const { error } = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    refetchInterval: 15000,
    retry: false,
  })
  const status = error instanceof ApiError ? error.status : null
  if (status !== 401 && status !== 503) return null
  return (
    <div className="flex items-start gap-2 border-b border-danger/40 bg-danger/10 px-8 py-2 text-xs text-danger">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <div>
        <span className="font-medium">
          {status === 401 ? '未授权：需要访问令牌' : '鉴权未配置：服务端处于 fail-closed'}
        </span>
        <div className="mt-0.5 text-muted">
          {status === 401
            ? 'BFF 本身是正常的（已收到响应），只是凭证缺失或已失效。请在左下角「访问令牌」填入最新令牌并保存；若刚轮换过令牌，请使用新令牌。'
            : '服务端未配置令牌注册表（AIOPS_CONSOLE_AUTH_TOKENS 或 data/console_tokens.json），所有 /api 请求被拒绝（503）。'}
        </div>
      </div>
    </div>
  )
}

/** 令牌过期预警横幅：自身令牌已轮换（处于宽限期）时提示更新。 */
function TokenBanner() {
  const { data } = useQuery({
    queryKey: ['auth-status'],
    queryFn: api.authStatus,
    refetchInterval: 30000,
    retry: false,
  })
  if (data?.self_token?.state !== 'previous') return null
  return (
    <div className="flex items-start gap-2 border-b border-warn/40 bg-warn/10 px-8 py-2 text-xs text-warn">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <div>
        <span className="font-medium">当前访问令牌已轮换</span>
        <span className="ml-2 text-muted">
          它将在宽限期（{humanSeconds(data.grace_seconds)}）结束后失效，请在左下角「访问令牌」中更新为最新令牌。
        </span>
        <div className="mt-0.5 font-mono text-muted">
          新令牌位于：{data.registry_path}（可让管理员在控制台执行「立即轮换」并经日志/文件获取）
        </div>
      </div>
    </div>
  )
}

/** 全局降级横幅：修复链路依赖不可用时显性提示（此前完全无提示）。 */
function DegradedBanner() {
  const { data } = useQuery({
    queryKey: ['subsystems'],
    queryFn: api.subsystems,
    refetchInterval: 10000,
  })
  if (!data?.degraded_mode) return null
  const detail = Object.entries(data.subsystems)
    .filter(([, info]) => !info.ok)
    .map(([name, info]) => `${name}（${info.detail}）`)
    .join('、')
  return (
    <div className="flex items-start gap-2 border-b border-warn/40 bg-warn/10 px-8 py-2 text-xs text-warn">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <div>
        <span className="font-medium">修复链路处于降级模式</span>
        <span className="ml-2 text-muted">不可用：{detail}</span>
        <div className="mt-0.5 text-muted">
          这些依赖缺失会导致日志证据 / 根因分析 / 沙箱验证降级——表现为修复失败、转人工或回落兜底补丁。
        </div>
      </div>
    </div>
  )
}

/** 全局 kill switch 横幅：激活期间常驻提示（写操作被拒绝的原因与操作者）。 */
function KillSwitchBanner() {
  const { data } = useQuery({
    queryKey: ['system'],
    queryFn: api.system,
    refetchInterval: 15000,
  })
  const state = data?.kill_switch?.state
  if (!state?.active) return null
  return (
    <div className="flex items-start gap-2 border-b border-danger/50 bg-danger/15 px-8 py-2 text-xs text-danger">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <div>
        <span className="font-medium">Kill switch 已激活：写操作已全部被拒绝</span>
        <span className="ml-2 font-mono text-muted">
          {state.actor || '未知操作者'} · {fmtDateTime(state.since)}
        </span>
        <div className="mt-0.5 text-muted">
          原因：{state.reason || '未注明'}。审批 / 发布指令 / 配置变更与 webhook 新流程均不可用；
          可在「系统状态」页由管理员关闭后恢复。
        </div>
      </div>
    </div>
  )
}

function Layout() {
  const { data } = useQuery({
    queryKey: ['overview'],
    queryFn: api.overview,
    refetchInterval: 5000,
  })
  return (
    <div className="min-h-full">
      <aside className="fixed inset-y-0 left-0 z-40 flex w-56 flex-col border-r border-line bg-panel">
        <div className="border-b border-line px-5 py-4">
          <div className="text-sm font-semibold tracking-wide">AIOps 运维控制台</div>
          <div className="mt-0.5 text-xs text-idle">自动运维智能体</div>
        </div>
        <nav className="flex-1 space-y-1 px-3 py-4">
          {NAV_ITEMS.map((item) => {
            const count = item.badgeKey ? (data?.counts[item.badgeKey] ?? 0) : 0
            const Icon = item.icon
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors',
                    isActive
                      ? 'bg-accent/10 text-accent'
                      : 'text-muted hover:bg-elevated hover:text-ink',
                  )
                }
              >
                <Icon size={16} strokeWidth={1.8} />
                <span className="flex-1">{item.label}</span>
                {count > 0 && (
                  <span className="rounded-full bg-warn/15 px-1.5 py-0.5 font-mono text-[10px] text-warn">
                    {count}
                  </span>
                )}
              </NavLink>
            )
          })}
        </nav>
        <div className="space-y-2 border-t border-line px-5 py-4">
          <div className="text-[10px] text-idle">服务器时间</div>
          <ServerClock />
          <ConnectionDot />
          <TokenControl />
        </div>
      </aside>
      <main className="ml-56">
        <AuthBanner />
        <TokenBanner />
        <KillSwitchBanner />
        <DegradedBanner />
        <div className="mx-auto max-w-6xl px-8 py-8">
          <Outlet />
        </div>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="flows" element={<Flows />} />
        <Route path="flows/:wfId" element={<FlowDetail />} />
        <Route path="approvals" element={<Approvals />} />
        <Route path="window" element={<WindowPage />} />
        <Route path="audit" element={<Audit />} />
        <Route path="monitored-apps" element={<MonitoredApps />} />
        <Route path="system" element={<SystemPage />} />
        <Route path="*" element={<EmptyState title="页面不存在" hint="请从左侧导航进入" />} />
      </Route>
    </Routes>
  )
}
