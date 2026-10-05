/** 应用路由：公开登录页 + 鉴权后的控制台外壳（AppShell）。
 *
 * 鉴权链路：
 * - 未持有任何凭证（localStorage）→ 直接跳登录页并记住来源路径；
 * - 持有凭证（会话或静态令牌）→ 进入外壳；凭证失效由接口 401 + 横幅引导重新登录。
 */

import type { ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { EmptyState } from './components/EmptyState'
import { getToken } from './lib/api'
import { Dashboard } from './pages/Dashboard'
import { Flows } from './pages/Flows'
import { FlowDetail } from './pages/FlowDetail'
import { Approvals } from './pages/Approvals'
import { WindowPage } from './pages/WindowPage'
import { Audit } from './pages/Audit'
import { MonitoredApps } from './pages/MonitoredApps'
import { SystemPage } from './pages/SystemPage'
import { Users } from './pages/Users'
import { Help } from './pages/Help'
import { Login } from './pages/Login'

/** 未登录时跳转登录页，并记住来源路径（登录后原路返回）。 */
function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation()
  if (!getToken()) {
    return (
      <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
    )
  }
  return <>{children}</>
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<Dashboard />} />
        <Route path="flows" element={<Flows />} />
        <Route path="flows/:wfId" element={<FlowDetail />} />
        <Route path="approvals" element={<Approvals />} />
        <Route path="window" element={<WindowPage />} />
        <Route path="audit" element={<Audit />} />
        <Route path="monitored-apps" element={<MonitoredApps />} />
        <Route path="system" element={<SystemPage />} />
        <Route path="users" element={<Users />} />
        <Route path="help" element={<Help />} />
        <Route path="*" element={<EmptyState title="页面不存在" hint="请从左侧导航进入" />} />
      </Route>
    </Routes>
  )
}
