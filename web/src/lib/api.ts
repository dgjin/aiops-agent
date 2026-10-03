/** BFF API 客户端：统一错误处理 + 服务端时间校准（设计方案 5.4、10）。 */

import type {
  ApprovalsResp,
  AuditResp,
  FlowsResp,
  FlowDetailResp,
  HealthResp,
  OverviewResp,
  ResultResp,
  SystemResp,
  WindowResp,
  WriteResp,
} from './types'

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
  let res: Response
  try {
    res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch {
    throw new Error('BFF 不可达（请确认已启动 uvicorn bff.app:app --port 8600）')
  }
  const data: unknown = await res.json().catch(() => null)
  syncServerTime((data as { server_time?: unknown } | null)?.server_time)
  if (!res.ok) {
    const message =
      (data as { error?: string } | null)?.error || `请求失败（HTTP ${res.status}）`
    throw new Error(message)
  }
  return data as T
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
  system: () => request<SystemResp>('/api/system'),

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
}
