/** 写操作入口的角色门控：权限不足时隐藏按钮，占位提示悬停说明所需角色。
 *
 * 与后端同源（middleware）：审批 / 发布指令 / 补丁需 operator；
 * 被监控应用 / kill switch / 用户管理需 admin。
 */

import type { ReactNode } from 'react'
import { hasRole, useSelf, type Role } from '../lib/permission'

const ROLE_LABEL: Record<Role, string> = {
  viewer: '观察者',
  operator: '操作员',
  admin: '管理员',
}

export function PermissionGate({
  require,
  children,
  fallback,
}: {
  require: Role
  children: ReactNode
  /** 权限不足时的占位内容；缺省为提示 chip（悬停说明所需角色）。传 null 则完全隐藏。 */
  fallback?: ReactNode
}) {
  const self = useSelf()
  // 身份未就绪：先不渲染，避免「占位 chip → 按钮」的闪烁
  if (self === null) return null
  if (hasRole(self.role, require)) return <>{children}</>
  if (fallback !== undefined) return <>{fallback}</>
  return (
    <span
      className="inline-flex cursor-not-allowed items-center rounded-lg border border-dashed border-line px-3.5 py-1.5 text-xs text-idle"
      title={`需要${ROLE_LABEL[require]}（${require}）角色`}
    >
      需要{ROLE_LABEL[require]}权限
    </span>
  )
}
