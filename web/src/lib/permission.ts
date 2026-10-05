/** 前端角色门控：与后端 middleware 的 `_WRITE_ROLE_RULES` 同源语义（防漂移）。
 *
 * - useSelf()：读取 /api/auth/status 的 `self` 身份（与 AppShell / 用户菜单共用查询缓存）；
 * - hasRole()：角色等级比较；未知 / 未加载一律判定为不满足（安全默认，宁可不渲染写入口）。
 */

import { useQuery } from '@tanstack/react-query'
import { api } from './api'
import type { SelfIdentity } from './types'

export type Role = 'viewer' | 'operator' | 'admin'

const ROLE_ORDER: Record<Role, number> = { viewer: 1, operator: 2, admin: 3 }

/** 当前登录身份（self）；查询未就绪时为 null。 */
export function useSelf(): SelfIdentity | null {
  const { data } = useQuery({
    queryKey: ['auth-status'],
    queryFn: api.authStatus,
    refetchInterval: 30000,
    retry: false,
  })
  return data?.self ?? null
}

/** 角色是否达到要求（required 为最低角色）。 */
export function hasRole(role: string | null | undefined, required: Role): boolean {
  return (ROLE_ORDER[role as Role] ?? 0) >= ROLE_ORDER[required]
}
