/** BFF API 客户端：统一错误处理 + 服务端时间校准（设计方案 5.4、10）。 */

import type {
  ApprovalsResp,
  AuditResp,
  AuditSummaryResp,
  AuthStatusResp,
  DiscoverMonitoredAppResp,
  FlowsResp,
  FlowDetailResp,
  HealthResp,
  KillSwitchResp,
  LoginResp,
  MonitoredAppInput,
  MonitoredAppsResp,
  MonitoredAppWriteResp,
  OverviewResp,
  ResultResp,
  SessionItem,
  SubsystemsResp,
  SystemResp,
  UserItem,
  WindowResp,
  WriteResp,
} from './types'

const TOKEN_KEY = 'aiops.token'

/** 读取本机保存的访问令牌（BFF 鉴权：Authorization: Bearer <token>）。 */
export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

/**
 * 规范化粘贴进来的令牌：去掉 `Bearer ` 前缀、首尾引号与空白。
 *
 * 这三种都是实测过的常见误粘贴：从 `.env`/JSON 复制会带上引号、
 * 从文档复制会带上 `Bearer `、从终端复制可能带换行——都会导致 401。
 */
export function normalizeToken(raw: string): string {
  return raw
    .replace(/[\u200b-\u200d\ufeff]/g, '') // 零宽字符：从聊天工具/网页复制最常见，肉眼不可见
    .trim()
    .replace(/^bearer\s+/i, '')
    .trim()
    .replace(/^["'`]|["'`]$/g, '')
    .trim()
}

/** 保存/清除访问令牌（保存前自动规范化）；空串表示清除。 */
export function setToken(token: string): void {
  const value = normalizeToken(token)
  try {
    if (value) localStorage.setItem(TOKEN_KEY, value)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* localStorage 不可用时忽略（令牌仅本次会话缺失） */
  }
}

/** 带 HTTP 状态码的接口错误：便于 UI 区分「不可达」与「未授权 / 权限不足 / 鉴权未配置」。 */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/** 网络层失败（BFF 真的不可达）——区别于上面的 HTTP 错误。 */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'NetworkError'
  }
}

let serverOffsetMs = 0

function syncServerTime(serverTime: unknown): void {
  if (typeof serverTime === 'string') {
    const parsed = Date.parse(serverTime)
    if (!Number.isNaN(parsed)) {
      serverOffsetMs = parsed - Date.now()
    }
  }
}

/** 服务端校准的当前时间（倒计时统一以此为基准）。 */
export function serverNow(): number {
  return Date.now() + serverOffsetMs
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let res: Response
  try {
    res = await fetch(path, {
      ...init,
      headers: { ...headers, ...((init?.headers as Record<string, string> | undefined) ?? {}) },
    })
  } catch {
    throw new NetworkError('BFF 不可达（请确认已启动 uvicorn bff.app:app --port 8600）')
  }
  const data: unknown = await res.json().catch(() => null)
  syncServerTime((data as { server_time?: unknown } | null)?.server_time)
  if (!res.ok) {
    const serverMsg = (data as { error?: string } | null)?.error
    const hint =
      res.status === 401
        ? '（凭证缺失或已失效：请登录，或在用户菜单中更新访问令牌）'
        : res.status === 403
          ? '（当前角色权限不足）'
          : res.status === 503
            ? '（鉴权未配置：服务端处于 fail-closed）'
            : ''
    throw new ApiError(res.status, (serverMsg || `请求失败（HTTP ${res.status}）`) + hint)
  }
  return data as T
}

/**
 * 把接口 / 网络错误转成可直接展示的中文说明。
 *
 * 页面原先直接 `String(error)`，会把 `ApiError:` 前缀连同整句提示抛给用户
 * （例如 "ApiError: 凭证无效或已过期（…）"），既刺眼又容易被误读成"令牌无效"。
 */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return '未授权：请登录，或在用户菜单中更新访问令牌后重试'
    if (err.status === 403) return '权限不足：当前角色无权访问该功能'
    if (err.status === 503) return '鉴权未配置：服务端处于 fail-closed，请先配置令牌注册表'
    return err.message
  }
  if (err instanceof NetworkError) return err.message
  if (err instanceof Error) return err.message
  return String(err)
}

function qs(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export interface QueuePatchInput {
  new_wf_id: string
  service: string
  alert_id: string
  description?: string
}

export const api = {
  health: () => request<HealthResp>('/api/health'),
  overview: () => request<OverviewResp>('/api/overview'),
  flows: (params: { status?: string; service?: string; stage?: string; limit?: number } = {}) =>
    request<FlowsResp>(`/api/flows${qs(params)}`),
  flowDetail: (wfId: string) =>
    request<FlowDetailResp>(`/api/flows/${encodeURIComponent(wfId)}`),
  flowResult: (wfId: string) =>
    request<ResultResp>(`/api/flows/${encodeURIComponent(wfId)}/result`),
  approvals: () => request<ApprovalsResp>('/api/approvals'),
  window: () => request<WindowResp>('/api/window'),
  audit: (params: { q?: string; stage?: string; days?: number } = {}) =>
    request<AuditResp>(`/api/audit${qs(params)}`),
  /** 操作审计报表聚合（按操作者 / 动作 / 日期 / 结果）。 */
  auditSummary: (params: { days?: number; top?: number } = {}) =>
    request<AuditSummaryResp>(`/api/audit/summary${qs(params)}`),
  system: () => request<SystemResp>('/api/system'),
  /** 紧急停止开关（admin）：激活后拒绝一切写操作；管理端点自身豁免以便随时关闭。 */
  killSwitch: (active: boolean, reason: string) =>
    request<KillSwitchResp>('/api/system/kill-switch', {
      method: 'POST',
      body: JSON.stringify({ active, reason }),
    }),
  /** 修复链路依赖自检（全局降级横幅）。 */
  subsystems: () => request<SubsystemsResp>('/api/subsystems'),
  /** 令牌轮换状态（含调用方自身令牌状态）。 */
  authStatus: () => request<AuthStatusResp>('/api/auth/status'),

  // ---- 登录 / 登出 / 本人改密（认证域） ----
  /** 登录：用户名+密码换会话令牌（公开端点；失败按 IP 限速）。 */
  login: (username: string, password: string) =>
    request<LoginResp>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  /** 登出：吊销当前会话令牌（静态令牌无会话可吊销，返回 hint）。 */
  logout: () =>
    request<{ revoked: boolean; hint?: string }>('/api/auth/logout', { method: 'POST' }),
  /** 本人改密（需原密码）；成功后全部会话被吊销（relogin_required=true 需重新登录）。 */
  changeOwnPassword: (oldPassword: string, newPassword: string) =>
    request<{ updated: boolean; revoked_sessions: number; relogin_required: boolean }>(
      '/api/auth/password',
      {
        method: 'POST',
        body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
      },
    ),

  // ---- 用户管理（管理员；/api/users 全接口服务端要求 admin） ----
  users: () => request<{ users: UserItem[] }>('/api/users'),
  createUser: (username: string, password: string, role: string) =>
    request<{ user: UserItem }>('/api/users', {
      method: 'POST',
      body: JSON.stringify({ username, password, role }),
    }),
  updateUser: (username: string, body: { role?: string; state?: string }) =>
    request<{ user: UserItem }>(`/api/users/${encodeURIComponent(username)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  deleteUser: (username: string) =>
    request<{ deleted: boolean; revoked_sessions: number }>(
      `/api/users/${encodeURIComponent(username)}`,
      { method: 'DELETE' },
    ),
  /** 重置他人密码（管理员；成功后强制下线其全部会话）。 */
  resetUserPassword: (username: string, password: string) =>
    request<{ updated: boolean; revoked_sessions: number }>(
      `/api/users/${encodeURIComponent(username)}/password`,
      { method: 'POST', body: JSON.stringify({ password }) },
    ),
  /** 用户在线会话（管理员；脱敏）。 */
  userSessions: (username: string) =>
    request<{ sessions: SessionItem[] }>(`/api/users/${encodeURIComponent(username)}/sessions`),
  /** 强制下线（管理员）：吊销该用户全部有效会话。 */
  revokeUserSessions: (username: string) =>
    request<{ revoked_sessions: number }>(
      `/api/users/${encodeURIComponent(username)}/sessions`,
      { method: 'DELETE' },
    ),

  /** 立即轮换令牌（管理员）。 */
  rotateTokens: (reason = 'manual') =>
    request<
      AuthStatusResp & {
        rotated_at: string
        identities: { user: string; role: string }[]
        reveal_enabled: boolean
        registry_path: string
        hint?: string
        new_tokens?: { token: string; user: string; role: string }[]
      }
    >('/api/auth/rotate', { method: 'POST', body: JSON.stringify({ reason }) }),

  approval: (wfId: string, decision: 'approve' | 'reject') =>
    request<WriteResp>(`/api/flows/${encodeURIComponent(wfId)}/approval`, {
      method: 'POST',
      body: JSON.stringify({ decision }),
    }),
  secondApproval: (wfId: string, decision: 'approve' | 'reject') =>
    request<WriteResp>(`/api/flows/${encodeURIComponent(wfId)}/second-approval`, {
      method: 'POST',
      body: JSON.stringify({ decision }),
    }),
  deployCommand: (wfId: string, command: 'deploy_now' | 'cancel') =>
    request<WriteResp>(`/api/flows/${encodeURIComponent(wfId)}/deploy-command`, {
      method: 'POST',
      body: JSON.stringify({ command }),
    }),
  queuePatch: (wfId: string, body: QueuePatchInput) =>
    request<WriteResp>(`/api/flows/${encodeURIComponent(wfId)}/queue-patch`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // 被监控应用维护（写盘即热生效）
  monitoredApps: () => request<MonitoredAppsResp>('/api/monitored-apps'),
  /**
   * 标准接口自动探测：GET <url>/.well-known/aiops.json（AIOps Manifest v1.0）。
   * 成功返回 suggested 预填值（免人工翻代码找日志路径 / 健康关键字）；
   * 失败是常态（未接入标准接口的应用），由调用方提示可手动填写。
   */
  discoverMonitoredApp: (url: string) =>
    request<DiscoverMonitoredAppResp>('/api/monitored-apps/discover', {
      method: 'POST',
      body: JSON.stringify({ url }),
    }),
  createMonitoredApp: (body: MonitoredAppInput) =>
    request<MonitoredAppWriteResp>('/api/monitored-apps', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  updateMonitoredApp: (id: string, body: MonitoredAppInput) =>
    request<MonitoredAppWriteResp>(`/api/monitored-apps/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  toggleMonitoredApp: (id: string) =>
    request<MonitoredAppWriteResp>(`/api/monitored-apps/${encodeURIComponent(id)}/toggle`, {
      method: 'POST',
    }),
  deleteMonitoredApp: (id: string) =>
    request<MonitoredAppWriteResp>(`/api/monitored-apps/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
  /** 导出清单（不含探测结果，便于跨环境搬运）。 */
  exportMonitoredApps: () =>
    request<{ version: number; items: MonitoredAppInput[] }>('/api/monitored-apps/export'),
  /** 批量导入：merge＝按名称覆盖/新增；replace＝清空后导入。 */
  importMonitoredApps: (items: unknown[], mode: 'merge' | 'replace') =>
    request<
      MonitoredAppWriteResp & {
        mode: string
        total: number
        added: number
        updated: number
        errors: { name: string | null; error: string }[]
      }
    >('/api/monitored-apps/import', { method: 'POST', body: JSON.stringify({ items, mode }) }),
  /** 批量启停 / 删除。 */
  batchMonitoredApps: (ids: string[], action: 'enable' | 'disable' | 'delete') =>
    request<MonitoredAppWriteResp & { action: string; affected: number; missing: string[] }>(
      '/api/monitored-apps/batch',
      { method: 'POST', body: JSON.stringify({ ids, action }) },
    ),
  /** 复制条目（名称自动去重）。 */
  duplicateMonitoredApp: (id: string, name?: string) =>
    request<MonitoredAppWriteResp>(`/api/monitored-apps/${encodeURIComponent(id)}/duplicate`, {
      method: 'POST',
      body: JSON.stringify({ name: name ?? null }),
    }),
  /** 按审计记录回滚配置（缺省回滚到最近一次变更之前）。 */
  rollbackMonitoredApp: (id: string, ts?: string) =>
    request<MonitoredAppWriteResp & { rolled_back_from?: string }>(
      `/api/monitored-apps/${encodeURIComponent(id)}/rollback`,
      { method: 'POST', body: JSON.stringify({ ts: ts ?? null }) },
    ),
}
