/** 应用外壳：左侧导航 + 服务器时钟/连接点 + 路由（设计方案 7.1、7.5）。 */

import { NavLink, Outlet, Route, Routes } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  ClipboardCheck,
  History,
  LayoutDashboard,
  Server,
  Timer,
} from 'lucide-react'
import { api, serverNow } from './lib/api'
import { cn } from './lib/format'
import { useNowTick } from './lib/hooks'
import { EmptyState } from './components/EmptyState'
import { Dashboard } from './pages/Dashboard'
import { Flows } from './pages/Flows'
import { FlowDetail } from './pages/FlowDetail'
import { Approvals } from './pages/Approvals'
import { WindowPage } from './pages/WindowPage'
import { Audit } from './pages/Audit'
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

/** BFF / Temporal 连接点（15 秒探测）。 */
function ConnectionDot() {
  const { data, isError } = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    refetchInterval: 15000,
  })
  const state = isError
    ? { dot: 'bg-danger', text: 'BFF 不可达' }
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
        </div>
      </aside>
      <main className="ml-56">
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
        <Route path="system" element={<SystemPage />} />
        <Route path="*" element={<EmptyState title="页面不存在" hint="请从左侧导航进入" />} />
      </Route>
    </Routes>
  )
}
