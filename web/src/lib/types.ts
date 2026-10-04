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
  /** 待审批期的判断依据（工作流未结束时由 status 查询透出） */
  patch?: PatchInfo | null
  test_report?: TestReport | null
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
  /** 今日硬失败/超时（读不到 result 的流程，按执行状态派生） */
  today_failed: number
}

export interface OverviewResp extends Timed {
  counts: OverviewCounts
  running: FlowItem[]
  terminal_dist_7d: Record<'DONE' | 'ESCALATED' | 'CANCELLED' | 'FAILED', number>
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
  /** 公告窗口总长度（秒，来自策略快照）；用于绘制倒计时环形进度 */
  countdown_seconds?: number | null
}

export interface OpsAuditRow {
  ts: string
  actor: string
  action: string
  /** 仅工作流类操作有值（工作流 ID） */
  wf_id: string
  /** 配置类操作的对象标识（如被监控应用 id）；工作流类为 null */
  target?: string | null
  params: Record<string, unknown>
  result: string
}

export interface AuditResp extends Timed {
  items: FlowItem[]
  ops: OpsAuditRow[]
}

/** 审计报表聚合分组项 */
export interface AuditAggCount {
  key: string
  count: number
}

/** 审计报表聚合（P3-07）：按操作者 / 动作 / 日期 / 结果计数 */
export interface AuditSummaryResp extends Timed {
  total: number
  days: number
  /** demo 采样口径标记（最近 5000 条）；production 恒 false */
  truncated: boolean
  by_actor: AuditAggCount[]
  by_action: AuditAggCount[]
  /** 日期升序（趋势） */
  by_day: AuditAggCount[]
  by_result: AuditAggCount[]
  latest_ts: string | null
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

/** 被监控应用探测结果。 */
export interface MonitoredAppProbe {
  running: boolean
  target: string
  status_code?: number
  latency_ms?: number
  note?: string
  error?: string
}

/** 主动巡检状态（app_prober.py 落盘；未运行巡检器时为 null）。 */
export interface MonitoredAppWatcher {
  ok: boolean
  failures: number
  last_check_at?: string
  last_ok_at?: string
  last_error?: string
  status_code?: number
  latency_ms?: number
  alerted: boolean
  alerted_at?: string | null
  alert_id?: string | null
}

/** 被监控应用（控制台可维护；改动热生效，无需重启）。 */
export interface MonitoredApp {
  id: string
  name: string
  url: string
  service: string
  /** 应用落盘日志路径（供采集器按清单采集；支持通配） */
  log_path: string
  /** 页面健康关键字（可选；空=仅连接级探测）：响应内容须包含该关键字才算在线 */
  probe_keyword: string
  /** 修复目标仓库路径（AIOps 修复引擎据此定位并生成补丁；空=不参与自动修复） */
  repo: string
  enabled: boolean
  note: string
  created_at: string
  updated_at: string
  probe: MonitoredAppProbe
  /** 主动巡检状态（连续失败计数 / 是否已自动触发修复流程） */
  watcher?: MonitoredAppWatcher | null
}

/** 新增/编辑入参。 */
export interface MonitoredAppInput {
  name: string
  url: string
  service: string
  log_path?: string
  probe_keyword?: string
  repo?: string
  enabled: boolean
  note?: string
}

/** 写操作响应（新增/编辑/启停/删除统一返回）。 */
export interface MonitoredAppWriteResp extends Timed {
  ok: boolean
  app?: MonitoredApp
  id?: string
}

export interface MonitoredAppsResp extends Timed {
  items: MonitoredApp[]
}

/** 令牌轮换状态（不含令牌值）。 */
export interface AuthTokenMeta {
  id: string
  user: string
  role: string
  state: 'active' | 'previous' | string
  created_at: string | null
  expires_at: string | null
  retired_at: string | null
}

export interface AuthStatusResp extends Timed {
  auto_rotation_enabled: boolean
  interval_seconds: number
  grace_seconds: number
  last_rotated_at: string | null
  next_rotation_at: string | null
  due: boolean
  registry_path: string
  allow_reveal: boolean
  /** 调用方自己那枚令牌的状态（state=previous 表示已轮换、处于宽限期） */
  self_token: {
    user: string | null
    role: string | null
    state: string | null
    expires_at: string | null
    retired_at: string | null
  }
  tokens?: AuthTokenMeta[]
}

/** 修复链路依赖自检（降级横幅数据源）。 */
export interface SubsystemInfo {
  ok: boolean
  detail: string
}

export interface SubsystemsResp extends Timed {
  subsystems: Record<'temporal' | 'loki' | 'ollama' | 'docker', SubsystemInfo>
  /** 不可用的依赖名（空数组表示链路健康） */
  degraded: string[]
  degraded_mode: boolean
}

/** Kill switch（全局熔断）状态：激活后拒绝一切写操作与新流程启动。 */
export interface KillSwitchState {
  active: boolean
  reason: string
  actor: string
  /** 本次激活时间（未激活为 null） */
  since: string | null
  updated_at: string
}

/** system 接口中的 kill switch 载荷（读侧容错：不可读时 state=null + error）。 */
export interface KillSwitchPayload {
  state: KillSwitchState | null
  error: string | null
}

/** 配置项元数据：来源与生效方式（避免"以为改了其实没生效"）。 */
export interface ConfigItem {
  key: string
  label: string
  value: string
  source: string
  /** hot＝下一请求生效；restart＝需重启进程；snapshot＝启动快照（对运行中流程无效） */
  effect: 'hot' | 'restart' | 'snapshot'
  owner: string
  applies_to_running: boolean
  note: string
}

export interface SystemResp extends Timed {
  temporal: { connected: boolean; latency_ms: number; address: string; error: string | null }
  /** 全局熔断（kill switch）状态 */
  kill_switch: KillSwitchPayload
  policy: PolicyPayload | null
  policy_error: string | null
  index: IndexStats | null
  stable: { running: boolean; port: string; target: string; body?: Record<string, unknown>; error?: string }
  monitored_apps: MonitoredApp[]
  config_items: ConfigItem[]
  recent_releases: RecentRelease[]
}

export interface HealthResp extends Timed {
  ok: boolean
  temporal: { connected: boolean; latency_ms: number; error: string | null }
}

/** kill switch 写操作响应（激活/关闭）。 */
export interface KillSwitchResp extends Timed {
  kill_switch: KillSwitchState
}

export interface WriteResp extends Timed {
  ok: boolean
  wf_id: string
  action: string
}
