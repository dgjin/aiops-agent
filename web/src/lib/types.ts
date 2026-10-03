/** BFF 响应契约（与 bff/app.py、bff/temporal_gateway.py 逐字段对齐）。 */

export interface Timed {
  server_time: string
}

export interface AlertMeta {
  alert_id: string
  service: string
  severity?: string
  description?: string
}

export interface Deadline {
  kind: 'approval' | 'second_approval' | 'countdown'
  at: string
}

export interface QueuedPatch {
  workflow_id: string | null
  alert_id: string
}

export interface FlowItem {
  wf_id: string
  run_id: string
  exec_status: string
  start_time: string | null
  close_time: string | null
  stage: string | null
  alert: AlertMeta | null
  deadline: Deadline | null
  needs_second: boolean
  patch_id: string | null
  approval: string | null
  second_approval: string | null
  deploy_command: string | null
  queued_patches: QueuedPatch[]
  duration_seconds: number | null
  confidence: number | null
  model_version: string | null
  stage_error: string | null
  /** 审批中心附加：待办类型 */
  pending?: 'approval' | 'second_approval'
  /** 发布窗口附加：公告版本号（从广播留痕提取） */
  version?: string | null
}

export interface OverviewCounts {
  running: number
  wait_approval: number
  notifying: number
  today_done: number
  today_escalated: number
}

export interface OverviewResp extends Timed {
  counts: OverviewCounts
  running: FlowItem[]
  terminal_dist_7d: Record<'DONE' | 'ESCALATED' | 'CANCELLED', number>
}

export interface FlowsResp extends Timed {
  items: FlowItem[]
}

export interface StatusPayload {
  stage: string
  alert: AlertMeta | null
  deadline: Deadline | null
  needs_second: boolean
  patch_id: string | null
  approval: string | null
  second_approval: string | null
  deploy_command: string | null
  queued_patches: QueuedPatch[]
}

export interface RootCause {
  error_type: string
  suspect_files: string[]
  confidence: number
  summary: string
}

export interface PatchInfo {
  patch_id: string
  alert_id: string
  files: string[]
  diff: string
  description: string
  risk: string
  model_version: string
  confidence: number
}

export interface TestReport {
  patch_id: string
  passed: boolean
  unit_tests: string
  regression_tests: string
  sast: string
  details: string
}

export interface DeployResult {
  version: string
  rolled_back: boolean
  reason: string
}

export interface ResultPayload {
  workflow_id: string
  alert_id: string
  service: string
  stage: string
  root_cause: RootCause | null
  confidence: number | null
  patch_id: string | null
  model_version: string | null
  patch: PatchInfo | null
  test_report: TestReport | null
  approvals: { gate: string; decision: string }[]
  gate_events: string[]
  deploy_result: DeployResult | null
  duration_seconds: number
  queued_patches: string[]
}

export interface Artifacts {
  notify: string[]
  argocd: string[]
  sandbox_dir: string | null
}

export interface FlowDetailResp extends Timed {
  wf_id: string
  run_id: string
  exec_status: string
  start_time: string | null
  close_time: string | null
  workflow_type: string
  task_queue: string
  status: StatusPayload | null
  result: ResultPayload | null
  errors: string[]
  artifacts: Artifacts | null
}

export interface ResultResp extends Timed {
  result: ResultPayload
}

export interface ApprovalsResp extends Timed {
  items: FlowItem[]
}

export interface WindowResp extends Timed {
  items: FlowItem[]
}

export interface OpsAuditRow {
  ts: string
  actor: string
  action: string
  wf_id: string
  params: Record<string, unknown>
  result: string
}

export interface AuditResp extends Timed {
  items: FlowItem[]
  ops: OpsAuditRow[]
}

export interface IndexStats {
  model: string | null
  dim: number | null
  backend: string | null
  created_at: string | null
  repo: string | null
  chunks: number
  files: number
  tickets: number
  faiss_file: boolean
}

export interface PolicyLock {
  key: string
  value: unknown
  reason: string
}

export interface PolicyPayload {
  source: string
  summary: string
  triage: Record<string, unknown>
  approval: Record<string, unknown>
  notify_window: Record<string, unknown>
  canary: Record<string, unknown>
  locks: PolicyLock[]
}

export interface RecentRelease {
  file: string
  name: string | null
  version: string | null
  service: string | null
  alert_id: string | null
  patch_id: string | null
  mtime: string
}

export interface SystemResp extends Timed {
  temporal: { connected: boolean; latency_ms: number; address: string; error: string | null }
  policy: PolicyPayload | null
  policy_error: string | null
  index: IndexStats | null
  stable: { running: boolean; port: string; target: string; body?: Record<string, unknown>; error?: string }
  recent_releases: RecentRelease[]
}

export interface HealthResp extends Timed {
  ok: boolean
  temporal: { connected: boolean; latency_ms: number; error: string | null }
}

export interface WriteResp extends Timed {
  ok: boolean
  wf_id: string
  action: string
}
