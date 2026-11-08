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

/** 审批证据绑定（approval-evidence-v1）：说明「审批的是哪一次变更」。 */
export interface EvidenceBinding {
  contract: string
  case_id: string
  patch_id: string
  diff_hash: string
  test_report_hash: string
  policy_sha256: string
  run_id: string
}

/** 工作流已记录的审批（谁 / 何时 / 哪个通道 / 基于哪份证据 / 什么意见）。 */
export interface ApprovalRecord {
  gate: string
  decision: string
  actor?: string
  channel?: string
  comment?: string
  command_id?: string
  submitted_at?: string
  received_at?: string
  contract?: string
  case_id?: string
  patch_id?: string
  diff_hash?: string
  test_report_hash?: string
  policy_sha256?: string
  run_id?: string
}

/** 被工作流拒绝的审批命令（未生效；卡片保留按钮便于重试）。 */
export interface ApprovalRejection {
  gate: string
  code: string
  reason: string
  command_id: string
  at: string
}

/** 审批提交载荷：意见 + 当前页面展示的期望证据（用于拒绝过期页面）。 */
export interface ApprovalInput {
  decision: 'approve' | 'reject'
  comment?: string
  expected_case_id?: string
  expected_diff_hash?: string
  expected_run_id?: string
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
  /** 闸门事件链（仅终态流程由 result 透出；行内流程图据此精确定位中断点） */
  gate_events?: string[]
  stage_error: string | null
  /** 审批证据与运行溯源（approval-evidence-v1；旧流程 approval_contract 为空串） */
  case_id?: string
  approval_contract?: string
  evidence_binding?: EvidenceBinding | null
  approvals?: ApprovalRecord[]
  approval_error?: ApprovalRejection | null
  approval_rejections?: ApprovalRejection[]
  requested_by?: string
  parent_run_id?: string
  parent_wf_id?: string
  retry_wf_id?: string
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
  /** 转人工待办未闭环数（open+assigned，P1-3 侧栏角标） */
  escalations_open: number
}

export interface OverviewResp extends Timed {
  counts: OverviewCounts
  running: FlowItem[]
  terminal_dist_7d: Record<'DONE' | 'SHADOWED' | 'ESCALATED' | 'CANCELLED' | 'FAILED', number>
}

/** 未纳管服务（P3⑧）：触发过流程但不在被监控清单中的 service（按 service join）。 */
export interface UnmanagedService {
  service: string
  flows: number
  running: number
  last_start: string | null
  last_wf_id: string | null
}

/** 系统总览聚合响应（P3⑦ /api/systems/overview）：清单+探测+计数+未纳管一次返回。 */
export interface SystemsOverviewResp extends Timed {
  counts: OverviewCounts
  running: FlowItem[]
  terminal_dist_7d: Record<'DONE' | 'SHADOWED' | 'ESCALATED' | 'CANCELLED' | 'FAILED', number>
  apps: MonitoredApp[]
  unmanaged: UnmanagedService[]
}

/** 时间线事件色调（映射 Tailwind 语义色）。 */
export type TimelineTone = 'info' | 'ok' | 'warn' | 'danger' | 'idle' | 'muted'

/** 时间线事件：seq 序号 + 文案 + 色调；at 仅「告警接收」（流程开始）与终态（流程关闭）有精确时间。 */
export interface TimelineEvent {
  seq: number
  text: string
  tone: TimelineTone
  at: string | null
}

/** 单流程事件分组（运行中优先；events 已按阶段顺序排列）。 */
export interface TimelineGroup {
  wf_id: string
  stage: string | null
  exec_status: string
  current: boolean
  start_time: string | null
  close_time: string | null
  duration_seconds: number | null
  events: TimelineEvent[]
}

/** 单系统活动时间线（P3⑨ /api/systems/{app_id}/timeline）。 */
export interface SystemTimelineResp extends Timed {
  app_id: string
  service: string
  groups: TimelineGroup[]
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
  /** 审批期补丁与测试证据（供对话框核对） */
  patch?: PatchInfo | null
  test_report?: TestReport | null
  /** 审批证据与运行溯源（approval-evidence-v1；旧流程 approval_contract 为空串） */
  case_id?: string
  approval_contract?: string
  evidence_binding?: EvidenceBinding | null
  approvals?: ApprovalRecord[]
  gate_events?: string[]
  approval_error?: ApprovalRejection | null
  approval_rejections?: ApprovalRejection[]
  requested_by?: string
  parent_run_id?: string
  parent_wf_id?: string
  retry_wf_id?: string
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
  approvals: ApprovalRecord[]
  gate_events: string[]
  deploy_result: DeployResult | null
  duration_seconds: number
  queued_patches: string[]
  /** 日志证据归档摘要（TRIAGING 阶段落库；需求流为 null） */
  evidence: EvidenceSummary | null
  /** 审批证据与运行溯源（approval-evidence-v1；旧流程 approval_contract 为空串） */
  run_id?: string
  case_id?: string
  requested_by?: string
  approval_contract?: string
  evidence_binding?: EvidenceBinding | null
  approval_error?: ApprovalRejection | null
  approval_rejections?: ApprovalRejection[]
  parent_run_id?: string
  parent_wf_id?: string
  retry_wf_id?: string
}

/** 证据质量六状态（M1）：与 aiops_agent/evidence_quality.py 固定口径一致；旧归档为 UNKNOWN。 */
export type EvidenceQualityStatus =
  | 'OK'
  | 'LOKI_UNAVAILABLE'
  | 'NO_LOGS'
  | 'STALE_LOGS'
  | 'UNMAPPED_SERVICE'
  | 'INSUFFICIENT_SIGNAL'
  | 'UNKNOWN'

/** 证据质量明细（归档记录内保存的结构）。 */
export interface EvidenceQuality {
  status: EvidenceQualityStatus
  /** 人类可读判定说明（为何不满足取证条件） */
  detail: string
  /** 最新一条日志时间（ISO；无日志为空串） */
  latest_log_at: string
  /** 命中错误/异常关键字的行数 */
  signal_lines: number
  lookback_minutes: number
  stale_after_minutes: number
  /** 证据预检阻断时的升级原因（仅在阻断路径写入） */
  escalation_reason?: string
}

/** 日志证据摘要（列表徽标 / 详情头部；不含原始日志正文）。 */
export interface EvidenceSummary {
  available: boolean
  wf_id: string
  alert_id: string
  service: string
  /** 证据采集时间（Loki 查询窗口终点） */
  collected_at: string
  /** Loki 是否可用；false 表示当时无日志可采集（升级原因常与此相关） */
  loki_available: boolean
  /** 原始采集行数 */
  total_lines: number
  /** 归档行数（超过上限时保留末尾，少于 total_lines 即截断） */
  stored_lines: number
  truncated: boolean
  template_count: number
  trace_count: number
  compression_ratio: number
  /** 证据质量状态（M1；旧归档 / 旧流程结果为 UNKNOWN 或缺省） */
  quality_status?: EvidenceQualityStatus
  /** 证据质量判定说明 */
  quality_detail?: string
  /** 最新一条日志时间（ISO） */
  latest_log_at?: string
  /** 命中错误/异常关键字的行数 */
  signal_lines?: number
}

/** 日志证据全文（流程详情 / 转人工核对弹窗）。 */
export interface LogEvidenceRecord extends Omit<EvidenceSummary, 'template_count' | 'trace_count' | 'available'> {
  archived_at: string
  trace_ids: string[]
  templates: { id: string; pattern: string; count: number }[]
  lines: string[]
  /** 证据质量明细（M1；旧归档为 null） */
  quality?: EvidenceQuality | null
}

export interface EvidenceResp extends Timed {
  evidence: LogEvidenceRecord
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
  /** 日志证据摘要（运行中读归档；终态取自 result.evidence） */
  evidence?: EvidenceSummary | null
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
  capacity: Record<string, unknown>
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

/** 接入就绪度（三态：null=未配置 / true=就绪 / false=未就绪）。 */
export interface MonitoredAppReadiness {
  /** 修复仓库：已配置且目录存在 */
  repo_ok: boolean | null
  /** 应用专属代码索引已存在（仓库变更时首次检索自动重建） */
  index_ok: boolean | null
  /** 可采集日志文件数（null=未配置；0=路径未命中） */
  log_files: number | null
}

/** 被监控应用（控制台可维护；改动热生效，无需重启）。 */
export interface MonitoredApp {
  id: string
  name: string
  url: string
  service: string
  /** 应用落盘日志路径（供采集器按清单采集；支持通配） */
  log_path: string
  /** 页面健康关键字（可选）：配置后响应内容须包含该关键字才算在线；留空采用通用白屏判据（响应非空 + HTML 完整 + body 含可渲染容器） */
  probe_keyword: string
  /** 健康检查路径（可选；空=根路径"/"）：Manifest 自动预填，探测/巡检按该路径执行页面判据 */
  health_path?: string
  /** 修复目标仓库路径（AIOps 修复引擎据此定位并生成补丁；空=不参与自动修复） */
  repo: string
  /** 依赖服务（P2-2 浅拓扑）：本应用依赖的服务名列表；供 triage 注入依赖上下文与工作台展示 */
  depends_on: string[]
  enabled: boolean
  note: string
  created_at: string
  updated_at: string
  probe: MonitoredAppProbe
  /** 主动巡检状态（连续失败计数 / 是否已自动触发修复流程） */
  watcher?: MonitoredAppWatcher | null
  /** 接入就绪度（仓库 / 索引 / 日志三态；驱动运维排查提示） */
  readiness?: MonitoredAppReadiness
}

/** 标准接口自动探测结果（Manifest v1.0：GET <url>/.well-known/aiops.json）。 */
export interface DiscoverMonitoredAppResp extends Timed {
  url: string
  ok: boolean
  error: string
  errors: string[]
  warnings: string[]
  manifest: Record<string, unknown> | null
  /** 成功时的建议预填值（控制台一键填表） */
  suggested?: {
    name: string | null
    service: string | null
    probe_keyword: string
    health_path: string
    log_path: string
  }
}

/** 新增/编辑入参。 */
export interface MonitoredAppInput {
  name: string
  url: string
  service: string
  log_path?: string
  probe_keyword?: string
  health_path?: string
  repo?: string
  /** 依赖服务（P2-2 浅拓扑）：本应用依赖的服务名列表；缺省=保留原值，[]=显式清空 */
  depends_on?: string[]
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

/** 需求基线数据源（被监控应用清单摘要，供下拉切换）。 */
export interface RequirementsApp {
  id: string
  name: string
  url: string
  service: string
  enabled: boolean
}

/** 跨系统聚合（app_id=all）的单系统拉取状态（逐系统容错；失败携中文原因）。 */
export interface RequirementSourceSummary {
  id: string
  name: string
  service: string
  /** 系统名（导出响应 system，缺失回退应用名）。 */
  system: string
  ok: boolean
  error: string
  returned: number
}

/** 需求评估历史条目（被监控系统 v0.9.102：继续评估的每次结论；时间升序）。 */
export interface RequirementRevision {
  id: number
  entryId: number
  action: string
  priority: string
  baselineVersion: string
  assessment: string
  reviewer: string
  createdAt: string | null
}

/** 需求基线条目（被监控系统标准导出接口 v1.0 契约，字段说明见 requirements_client）。 */
export interface RequirementsEntry {
  id: number | string
  kind: string
  title: string
  content: string
  status: string
  priority: string
  baselineVersion: string
  assessment: string
  submitter: string
  department: string
  reviewer: string
  reviewedAt: string | null
  createdAt: string | null
  updatedAt: string | null
  /** 评估历史（可能缺省：老版本被监控系统不带该字段）。 */
  revisions?: RequirementRevision[]
  /** 聚合模式（app_id=all）来源标注：条目所属被监控应用 / 系统（单系统模式缺省）。 */
  _source_app_id?: string
  _source_app_name?: string
  _source_service?: string
  _source_system?: string
}

/** 需求基线读接口响应（BFF 代拉标准导出接口；ok=false 时 error 为中文原因）。 */
export interface RequirementsResp extends Timed {
  ok: boolean
  error: string
  warnings: string[]
  apps: RequirementsApp[]
  app: RequirementsApp | null
  /** 聚合模式标识（app_id=all；单系统模式缺省）。 */
  aggregated?: boolean
  url: string
  service: string
  system: string
  exported_at: string
  filter: Record<string, unknown>
  returned: number
  entries: RequirementsEntry[]
  /** 各系统拉取状态（聚合模式有意义；单系统模式为空数组）。 */
  sources: RequirementSourceSummary[]
}

/** ===== 需求智能分析闭环（查看 / 反馈再分析 / 批准进入修复工作流）===== */

/** LLM 分析结果（risk / complexity 为文本，confidence 0-1；degraded=true 不可批准）。 */
export interface RequirementAnalysisResult {
  understanding: string
  plan: string[]
  suspect_files: string[]
  acceptance: string[]
  risk: string
  complexity: string
  confidence: number
  degraded: boolean
}

/** 单次分析的元信息（模型 / 降级原因 / 耗时）。 */
export interface RequirementAnalysisMeta {
  model?: string
  degraded?: boolean
  reason?: string
  elapsed_seconds?: number
}

/** 分析版本（逐次追加；trigger=initial / feedback / retry / refresh，feedback 为触发它的反馈原文）。 */
export interface RequirementAnalysisVersion {
  version: number
  ts: string
  trigger: string
  feedback: string
  actor: string
  status: string
  error: string
  analysis: RequirementAnalysisResult | null
  meta: RequirementAnalysisMeta | null
}

/** 分析会话（一条需求一个会话：analyzing→analyzed→approved，失败可反馈 / 重试）。
 *  ``release_status``（仅批准过的会话）：修复流程发布结果——released=已发布成功
 *  （需求终态，不可再次分析）/ running / escalated / cancelled / failed / unknown。
 */
export interface RequirementAnalysisEntry {
  id: string
  app_id: string
  service: string
  entry_id: string
  entry_title: string
  entry_kind: string
  entry_priority: string
  entry_snapshot: Record<string, unknown>
  status: string
  current_version: number
  versions: RequirementAnalysisVersion[]
  approved: { version: number; wf_id: string; at: string; actor: string } | null
  release_status?: string | null
  created_at: string
  updated_at: string
}

/** 分析会话摘要（列表接口用；不含版本明细）。 */
export interface RequirementAnalysisSummary {
  id: string
  app_id: string
  service: string
  entry_id: string
  entry_title: string
  entry_kind: string
  entry_priority: string
  status: string
  current_version: number
  latest_degraded: boolean | null
  latest_confidence: number | null
  approved: { version: number; wf_id: string; at: string; actor: string } | null
  /** 修复流程发布结果（同 RequirementAnalysisEntry.release_status；未批准为 undefined）。 */
  release_status?: string | null
  created_at: string
  updated_at: string
}

export interface RequirementAnalysesResp extends Timed {
  analyses: RequirementAnalysisSummary[]
}

export interface RequirementAnalysisResp extends Timed {
  analysis: RequirementAnalysisEntry
}

export interface RequirementAnalysisCreateResp extends RequirementAnalysisResp {
  started: boolean
}

/** 同步被监控系统最新内容：started=true 表示已追加 refresh 版本重新分析（终态会话仅刷新快照）。 */
export interface RequirementAnalysisSyncResp extends RequirementAnalysisResp {
  started: boolean
}

export interface RequirementAnalysisApproveResp extends RequirementAnalysisResp {
  wf_id: string
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
  /** 调用方身份统一视图（会话/静态令牌共用；顶栏用户菜单数据源） */
  self: SelfIdentity
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
  /** 审批命令 ID（旧协议流程为空串） */
  command_id?: string
  /** 送达语义：accepted=工作流已受理并记录；rejected=工作流业务拒绝；submitted=命令已提交 */
  delivery?: 'accepted' | 'rejected' | 'submitted'
  message?: string
}

// ----------------------------------------------------------------------
// 用户与会话（批次 1 后端；登录/用户管理/在线会话）
// ----------------------------------------------------------------------

/** 调用方身份统一视图（/api/auth/status 的 self 字段）。 */
export interface SelfIdentity {
  user: string | null
  role: string | null
  /** session=用户名密码登录的会话令牌；static=长期静态令牌（自动化/应急通道） */
  source: 'session' | 'static' | string
  state: string | null
  expires_at: string | null
}

/** 登录响应（换发会话令牌；明文令牌仅在本次响应回显）。 */
export interface LoginResp extends Timed {
  token: string
  user: string
  role: string
  expires_at: string
  ttl_seconds: number
}

/** 用户条目（不含密码哈希）。 */
export interface UserItem {
  username: string
  role: 'viewer' | 'operator' | 'admin' | string
  state: 'active' | 'disabled' | string
  created_at: string | null
  updated_at: string | null
}

/** 在线会话条目（脱敏：id 为令牌指纹前 8 位）。 */
export interface SessionItem {
  id: string
  user: string
  role: string
  created_at: string | null
  expires_at: string | null
  last_seen_at: string | null
}

// ----------------------------------------------------------------------
// 转人工处置闭环（P1-3）：ESCALATED 流程 → 待办 → 指派 → 处置/重试 → 超时再升级
// ----------------------------------------------------------------------

/** 待办处置轨迹条目。 */
export interface EscalationHistory {
  ts: string
  action: string
  actor: string
  detail: string
}

/** 转人工待办处置分类（M1）：观测异常/证据不足与业务闸门分开统计。 */
export type EscalationCategory =
  | 'model'
  | 'approval'
  | 'sandbox'
  | 'release'
  | 'observability'
  | 'evidence'
  | 'requirement'
  | 'other'

/** 结构化关闭原因（PRD M2）：RESOLVED 必须填写实际根因 / 处置 / 验证结果。 */
export type EscalationCloseReason =
  | 'RESOLVED'
  | 'FALSE_POSITIVE'
  | 'OBSERVABILITY_FIXED'
  | 'DUPLICATE'
  | 'NO_CODE_CHANGE'
  | 'OTHER'

/** 对智能体判断的评价（人工复盘口径）。 */
export type AgentVerdict = 'CORRECT' | 'PARTLY_CORRECT' | 'INCORRECT' | 'UNKNOWN'

/** 知识审核状态：'' 未提交；DRAFT 待审核；APPROVED 已入库；REJECTED 已拒绝/已撤销。 */
export type KnowledgeStatus = '' | 'DRAFT' | 'APPROVED' | 'REJECTED'

/** 结构化关闭记录（服务端 escalation.resolution）。 */
export interface EscalationResolution {
  close_reason: EscalationCloseReason
  /** 实际根因（与智能体判断分开记录） */
  actual_root_cause: string
  actual_action: string
  verification: string
  verdict: AgentVerdict
  /** 是否提交知识库候选（提交后进入 DRAFT 审核队列） */
  knowledge_candidate: boolean
  /** 入库前已确认检查敏感信息 */
  sensitive_confirmed: boolean
  knowledge_status: KnowledgeStatus
  knowledge_reviewed_by: string
  knowledge_reviewed_at: string
  knowledge_reject_reason: string
  note: string
  closed_by: string
  closed_at: string
  /** 记录来源（M3：retry-tracking 表示重试流程终态自动关闭） */
  source?: string
}

/** 结构化关闭请求体；M3 起仅备注关闭已停用（服务端返回 400 迁移错误）。 */
export interface EscalationClosePayload {
  note?: string
  close_reason?: EscalationCloseReason
  actual_root_cause?: string
  actual_action?: string
  verification?: string
  verdict?: AgentVerdict
  knowledge_candidate?: boolean
  sensitive_confirmed?: boolean
  /** 读取时的记录版本（CAS）：与库中不一致返回 409 */
  version?: number
}

/** 知识导出同步结果（关闭/审核后由 BFF 写入 knowledge_records.json）。 */
export interface KnowledgeExport {
  exported: number
  skipped: number
  path?: string
  error?: string
}

/** 转人工待办条目（读侧附加 overdue / sla_minutes 派生状态）。 */
export interface EscalationEntry {
  id: string
  wf_id: string
  alert_id: string
  service: string
  /** 自动升级原因摘要（由闸门事件链翻译） */
  auto_reason: string
  /** 处置分类（读侧派生：新记录读字段，历史记录按升级原因确定性回填） */
  category?: EscalationCategory
  /** 分类中文标签（服务端派生，前端直接展示） */
  category_label?: string
  /** 处置计划（未关闭待办的下一步动作；M2） */
  plan?: string
  /** 结构化关闭记录（历史数据为 null） */
  resolution?: EscalationResolution | null
  /** 关闭原因（服务端从 resolution 派生） */
  close_reason?: EscalationCloseReason | ''
  close_reason_label?: string
  /** 知识审核状态与中文标签（服务端派生） */
  knowledge_status?: KnowledgeStatus
  knowledge_label?: string
  alert: AlertMeta | null
  status: 'open' | 'assigned' | 'retrying' | 'closed'
  assignee: string
  note: string
  escalated_at: string
  updated_at: string
  closed_at: string | null
  /** 是否已因处置超时再升级（仅真实送达才置 true） */
  re_escalated: boolean
  history: EscalationHistory[]
  /** 已超 SLA 未处置（读侧派生；retrying 暂停计时） */
  overdue: boolean
  sla_minutes: number
  /** 需求来源待办（读侧派生；重试修复将重启需求修复流而非告警诊断流） */
  requirement?: boolean
  /** 日志证据摘要（转人工核对；无归档为 null） */
  evidence: EvidenceSummary | null
  /** 所属监控系统展示名（命中「被监控系统」清单→应用名；未纳管→服务名） */
  system: string
  /** 是否已在「被监控系统」清单纳管（node / redis 等基础设施服务为 false） */
  system_managed: boolean
  /** 重试跟踪（M3）：新修复流程与父运行链 */
  retry_wf_id?: string
  parent_run_id?: string
  retry_started_at?: string | null
  retry_outcome?: '' | 'DONE' | 'ESCALATED' | 'FAILED' | 'CANCELLED' | 'SHADOWED'
  retry_outcome_label?: string
  /** 再升级通知投递（M3）：仅 live 且无错误才算已送达 */
  notify_mode?: '' | 'live' | 'recorded' | 'failed'
  notify_label?: string
  notify_attempts?: number
  notify_next_retry_at?: string | null
  notify_error?: string
  notify_updated_at?: string | null
  notify_pending?: boolean
  /** 记录版本（CAS）：写操作回传，旧版本返回 409 */
  version?: number
}

export interface EscalationStats {
  total: number
  open: number
  assigned: number
  /** 重试跟踪中（M3；开放统计包含，不算 overdue） */
  retrying?: number
  closed: number
  re_escalated: number
  /** 通知待重试（M3）：recorded / failed 且尚未送达 */
  notify_pending?: number
  /** 分类计数（key 为 EscalationCategory；含 other） */
  categories?: Record<string, number>
  /** 知识审核计数（M2）：待审核 / 已入库 / 已拒绝 */
  knowledge?: { draft: number; approved: number; rejected: number }
  sla_minutes: number
}

export interface EscalationsResp extends Timed {
  escalations: EscalationEntry[]
  stats: EscalationStats
  /** 本次读侧幂等登记的新增条数 */
  registered: number
}

export interface EscalationWriteResp extends Timed {
  ok: boolean
  escalation: EscalationEntry
  /** retry 专用：新启动的修复流程 ID */
  new_wf_id?: string
  /** retry 专用：重试跟踪的流程 ID（与原待办 retry_wf_id 一致） */
  retry_wf_id?: string
  /** close / knowledge review 专用：知识导出同步结果 */
  knowledge_export?: KnowledgeExport
}

// ----------------------------------------------------------------------
// 运营度量（M5）：分维度完成率 / MTTA / 回滚 / 证据 / 沙箱 / 待办 SLA / 值班告警
// 口径：所有比率在分母为 0 时为 null，页面显示「暂无数据」，不得渲染成 0%。
// ----------------------------------------------------------------------

/** 闸门拦截分档计数（key 为事件族前缀）。 */
export interface OpsGateBreakdown {
  key: string
  label: string
  count: number
}

/** 单一口径块（总览 / 按来源复用同一结构）。 */
export interface OpsRateBlock {
  closed: number
  done: number
  auto_fix_rate: number | null
  human_intervention_rate: number | null
  human_intervention_sample: number
  mttr: { avg_seconds: number | null; p95_seconds: number | null; sample: number }
}

export interface OpsRiskRow {
  key: string
  label: string
  closed: number
  done: number
  auto_fix_rate: number | null
}

export interface OpsServiceRow {
  service: string
  closed: number
  done: number
  auto_fix_rate: number | null
  human_intervention_rate: number | null
}

export interface OpsEvidenceStatusRow {
  key: string
  label: string
  count: number
  /** 观测面异常（Loki 不可用/无日志/过期/未映射）：不计入模型低置信 */
  observability: boolean
}

export interface OpsEvidenceBlock {
  available: boolean
  sample: number
  ok_rate: number | null
  observability_share: number | null
  by_status: OpsEvidenceStatusRow[]
}

export interface OpsLogShipBlock {
  available: boolean
  services: number
  healthy: number
  unhealthy: number
  freshness_p95_seconds: number | null
  coverage: { covered: number; total: number; rate: number | null } | null
  unhealthy_services: string[]
}

export interface OpsSandboxModeRow {
  key: string
  label: string
  count: number
  passed: number
  /** 仅 UNIT_TEST 计入真实通过率；CONTRACT_TEST / STATIC_ONLY 单列 */
  is_real: boolean
}

export interface OpsSandboxBlock {
  available: boolean
  sample: number
  by_mode: OpsSandboxModeRow[]
  real_pass_rate: number | null
  real_sample: number
  static_count: number
  contract_count: number
  unknown_count: number
  /** 结构可应用率：补丁具备 diff + 文件清单（真实 git apply 由发布阶段判定） */
  patch_structural_rate: number | null
  patch_structural_sample: number
  /** 人工改判率：待办关闭时判定「AI 根因不正确」的占比 */
  override_rate: number | null
  override_sample: number
  high_confidence_false_pass: number
}

export interface OpsEscalationDetailBlock {
  available: boolean
  open_sample: number
  open_avg_seconds: number | null
  open_p95_seconds: number | null
  handle_avg_seconds: number | null
  sla_minutes: number
  sla_breach: number | null
  close_reasons: Array<{ key: string; label: string; count: number }>
  knowledge: { draft: number; approved: number; rejected: number; adopted: number | null }
}

export interface OpsDutyAlert {
  code: string
  label: string
  /** danger / warn / unknown（数据不足时为 unknown，不冒充「正常」） */
  level: string
  value: number | null
  threshold: string
  detail: string
  /** 仓库内 Runbook 相对路径 + 锚点（如 docs/AIOps 值班 Runbook.md#log-stall） */
  runbook: string
}

export interface OpsMetricsResp extends Timed {
  period_days: number
  /** 数据面：warehouse（production SQL 镜像）/ temporal（读侧回退） */
  source: string
  /** SQL 镜像最近同步时间（warehouse 数据面） */
  mirror_updated_at?: string | null
  /** 窗口内终态流程总数（各项比率的共同分母） */
  closed_total: number
  by_stage: Record<string, number>
  /** 告警→发布完成时长（仅 DONE 样本） */
  mttr: { avg_seconds: number | null; p95_seconds: number | null; sample: number }
  /** DONE / 全部终态 */
  auto_fix_rate: number | null
  auto_fix_sample: number
  /** 被人工写操作（审批/指令/排队/转人工处置）触及的终态占比 */
  human_intervention_rate: number | null
  human_intervention_sample: number
  /** 告警触发 → 首个人工动作 */
  mtta: { avg_seconds: number | null; p95_seconds: number | null; sample: number }
  /** gate_events 命中拦截族的终态占比 */
  gate_block_rate: number | null
  gate_block_sample: number
  gate_breakdown: OpsGateBreakdown[]
  /** 分来源（alert=告警修复 / requirement=需求修复），分母各自独立 */
  by_kind: Record<string, OpsRateBlock>
  by_risk: OpsRiskRow[]
  by_service: OpsServiceRow[]
  /** 回滚率：degraded / 进入金丝雀（healthy 或 degraded） */
  rollback: { rate: number | null; sample: number; released: number }
  evidence: OpsEvidenceBlock
  logship: OpsLogShipBlock
  sandbox: OpsSandboxBlock
  escalation_detail: OpsEscalationDetailBlock
  cost: { available: boolean; model_cost_usd: number | null; note: string }
  duty_alerts: OpsDutyAlert[]
  /** 转人工待办现状（存储不可读时为 null） */
  escalations: EscalationStats | null
  /** 数据源降级的部件名（temporal / audit / escalations / warehouse…） */
  degraded: string[]
}

export interface OpsRunbookResp extends Timed {
  code: string
  title: string
  body: string
  path: string
  /** 文件缺失或 code 未收录时为 false（前端给出人工排查指引） */
  known: boolean
}
