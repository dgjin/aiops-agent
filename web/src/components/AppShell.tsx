/** 应用外壳：侧栏（可收缩）+ 顶栏（帮助 / 外观 / 用户菜单）+ 全局横幅 + 路由出口。
 *
 * - 侧栏收缩状态持久化于 localStorage（'aiops.sidebar'），w-56 ↔ w-16；
 *   收缩开关位于顶栏左侧（纯图标按钮，收起/展开二态），不占用侧栏底部；
 * - 全局横幅（登录失效 / 令牌轮换 / kill switch / 降级）由原 App.tsx 迁入；
 * - 「用户管理」导航仅管理员可见（服务端同样强制 admin，前端只是不展示入口）；
 * - 快捷键：? 打开帮助中心（输入框内不触发）。
 */

import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ClipboardCheck,
  HelpCircle,
  History,
  Inbox,
  LayoutDashboard,
  PanelLeftClose,
  PanelLeftOpen,
  Radar,
  Server,
  Timer,
  Users,
} from 'lucide-react'
import { ApiError, api, serverNow } from '../lib/api'
import { cn, fmtDateTime } from '../lib/format'
import { useNowTick } from '../lib/hooks'
import { BrandLockup, Logo } from './Logo'
import { ThemeMenu } from './ThemeMenu'
import { UserMenu } from './UserMenu'
import type { OverviewCounts } from '../lib/types'

interface NavItem {
  to: string
  label: string
  icon: typeof LayoutDashboard
  end?: boolean
  badgeKey?: keyof OverviewCounts
  /** 仅管理员可见（服务端同样要求 admin）。 */
  adminOnly?: boolean
}

const NAV_ITEMS: NavItem[] = [
  { to: '/', label: '流程看板', icon: LayoutDashboard, end: true },
  { to: '/flows', label: '流程列表', icon: Activity },
  { to: '/approvals', label: '审批中心', icon: ClipboardCheck, badgeKey: 'wait_approval' },
  { to: '/escalations', label: '转人工待办', icon: Inbox, badgeKey: 'escalations_open' },
  { to: '/window', label: '发布窗口', icon: Timer, badgeKey: 'notifying' },
  { to: '/audit', label: '审计回看', icon: History },
  { to: '/monitored-apps', label: '被监控应用', icon: Radar },
  { to: '/system', label: '系统状态', icon: Server },
  { to: '/users', label: '用户管理', icon: Users, adminOnly: true },
  { to: '/help', label: '帮助中心', icon: HelpCircle },
]

const SIDEBAR_KEY = 'aiops.sidebar'

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === 'collapsed'
  } catch {
    return false
  }
}

/** 服务器校准时钟（侧栏底部常驻）。 */
function ServerClock() {
  useNowTick()
  return (
    <span className="font-mono text-xs tabular-nums text-muted">
      {new Date(serverNow()).toLocaleTimeString('zh-CN', { hour12: false })}
    </span>
  )
}

/** BFF / Temporal 连接点（15 秒探测；collapsed 时仅圆点 + title）。
 *
 * 注意：必须区分「网络不可达」与「HTTP 错误」。此前把任何错误都显示成
 * 「BFF 不可达」，令牌缺失/过期（401）时会把排查方向带偏到网络。
 */
function ConnectionDot({ compact = false }: { compact?: boolean }) {
  const { data, isError, error } = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    refetchInterval: 15000,
    retry: false,
  })

  const status = error instanceof ApiError ? error.status : null
  const state = isError
    ? status === 401
      ? { dot: 'bg-danger', text: '未授权（请登录）' }
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
    <span
      className="inline-flex items-center gap-1.5 text-xs text-muted"
      title={compact ? state.text : undefined}
    >
      <span
        className={cn('h-1.5 w-1.5 rounded-full', state.dot, state.dot !== 'bg-ok' && 'animate-pulse')}
      />
      {!compact && state.text}
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

/** 登录失效 / 鉴权未配置横幅：与「BFF 不可达」区分开，直接给出该做的事。 */
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
          {status === 401 ? '登录已过期或凭证失效' : '鉴权未配置：服务端处于 fail-closed'}
        </span>
        <div className="mt-0.5 text-muted">
          {status === 401 ? (
            <>
              BFF 本身是正常的（已收到响应），只是会话过期或令牌失效。请
              <Link to="/login" className="mx-0.5 text-danger underline">
                重新登录
              </Link>
              ；自动化场景可在右上角用户菜单的「访问令牌」中更新静态令牌。
            </>
          ) : (
            '服务端未配置令牌注册表（AIOPS_CONSOLE_AUTH_TOKENS 或 data/console_tokens.json），所有 /api 请求被拒绝（503）。'
          )}
        </div>
      </div>
    </div>
  )
}

/** 令牌过期预警横幅：静态令牌已轮换（处于宽限期）时提示更新。 */
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
        <span className="font-medium">当前静态令牌已轮换</span>
        <span className="ml-2 text-muted">
          它将在宽限期（{humanSeconds(data.grace_seconds)}）结束后失效，请在右上角用户菜单的「访问令牌」中更新为最新令牌。
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

export function AppShell() {
  const [collapsed, setCollapsed] = useState(readCollapsed)
  const toggleSidebar = () =>
    setCollapsed((prev) => {
      const next = !prev
      try {
        localStorage.setItem(SIDEBAR_KEY, next ? 'collapsed' : 'expanded')
      } catch {
        /* 隐私模式等场景忽略：仅本次会话生效 */
      }
      return next
    })

  const { data: overview } = useQuery({
    queryKey: ['overview'],
    queryFn: api.overview,
    refetchInterval: 5000,
  })
  const { data: auth } = useQuery({
    queryKey: ['auth-status'],
    queryFn: api.authStatus,
    refetchInterval: 30000,
    retry: false,
  })
  const items = NAV_ITEMS.filter((item) => !item.adminOnly || auth?.self?.role === 'admin')

  const location = useLocation()
  const navigate = useNavigate()
  const current = NAV_ITEMS.find((item) =>
    item.end ? location.pathname === item.to : location.pathname.startsWith(item.to),
  )

  // 快捷键：? → 帮助中心（输入框 / 文本域内不触发）
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (
        target &&
        (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
      ) {
        return
      }
      if (event.key === '?') {
        event.preventDefault()
        navigate('/help')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate])

  return (
    <div className="min-h-full">
      <aside
        className={cn(
          'fixed inset-y-0 left-0 z-40 flex flex-col border-r border-line bg-panel transition-[width] duration-200',
          collapsed ? 'w-16' : 'w-56',
        )}
      >
        <div
          className={cn(
            'flex h-14 shrink-0 items-center border-b border-line',
            collapsed ? 'justify-center px-2' : 'px-4',
          )}
        >
          {collapsed ? (
            <Logo size={26} className="text-ink" />
          ) : (
            <BrandLockup subtitle="自动运维智能体" />
          )}
        </div>
        <nav className={cn('flex-1 space-y-1 overflow-y-auto py-3', collapsed ? 'px-2' : 'px-3')}>
          {items.map((item) => {
            const count = item.badgeKey ? (overview?.counts[item.badgeKey] ?? 0) : 0
            const Icon = item.icon
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                title={collapsed ? item.label : undefined}
                className={({ isActive }) =>
                  cn(
                    'relative flex items-center rounded-lg py-2 text-sm transition-colors',
                    collapsed ? 'justify-center' : 'gap-2.5 px-3',
                    isActive ? 'nav-active' : 'text-muted hover:bg-elevated hover:text-ink',
                  )
                }
              >
                <Icon size={16} strokeWidth={1.8} className="shrink-0" />
                {!collapsed && <span className="flex-1 truncate">{item.label}</span>}
                {count > 0 && !collapsed && (
                  <span className="rounded-full bg-warn/15 px-1.5 py-0.5 font-mono text-[10px] text-warn">
                    {count}
                  </span>
                )}
                {count > 0 && collapsed && (
                  <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-warn" />
                )}
              </NavLink>
            )
          })}
        </nav>
        <div className={cn('shrink-0 border-t border-line py-3', collapsed ? 'px-2' : 'px-4')}>
          {collapsed ? (
            <div className="flex justify-center">
              <ConnectionDot compact />
            </div>
          ) : (
            <div className="space-y-1.5">
              <div className="text-[10px] text-idle">服务器时间</div>
              <ServerClock />
              <ConnectionDot />
            </div>
          )}
        </div>
      </aside>

      <div className={cn('min-h-full transition-[margin] duration-200', collapsed ? 'ml-16' : 'ml-56')}>
        <header className="topbar-glow sticky top-0 z-30 flex h-12 items-center justify-between gap-3 border-b border-line bg-panel/95 px-6 backdrop-blur">
          <div className="flex min-w-0 items-center gap-1.5">
            <button
              type="button"
              onClick={toggleSidebar}
              title={collapsed ? '展开侧栏' : '收起侧栏'}
              aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
              className="-ml-1.5 rounded-lg p-2 text-muted transition-colors hover:bg-elevated hover:text-accent"
            >
              {collapsed ? (
                <PanelLeftOpen size={16} strokeWidth={1.8} />
              ) : (
                <PanelLeftClose size={16} strokeWidth={1.8} />
              )}
            </button>
            <div className="truncate text-sm text-muted">{current?.label ?? ''}</div>
          </div>
          <div className="flex items-center gap-1">
            <NavLink
              to="/help"
              title="帮助中心（按 ? 快速打开）"
              aria-label="帮助中心"
              className={({ isActive }) =>
                cn(
                  'rounded-lg p-2 text-muted transition-colors hover:bg-elevated hover:text-ink',
                  isActive && 'bg-elevated text-ink',
                )
              }
            >
              <HelpCircle size={16} strokeWidth={1.8} />
            </NavLink>
            <ThemeMenu />
            <UserMenu />
          </div>
        </header>
        <AuthBanner />
        <TokenBanner />
        <KillSwitchBanner />
        <DegradedBanner />
        <main className="mx-auto max-w-6xl px-8 py-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
