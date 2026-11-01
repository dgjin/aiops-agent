/** 转人工待办（P1-3）：ESCALATED 处置闭环——指派 / 处置计划 / 结构化关闭 / 重试修复 / 知识审核。
 *
 * - 列表 3 秒轮询；读侧自动幂等登记（ESCALATED 流程 → open 待办），已关闭不重开；
 * - 超 SLA（默认 30 分钟）未处置的条目由 BFF 后台巡检经通知渠道再升级一次，
 *   前端以「已超时未处置 / 已再升级」标记提示值班同学；
 * - 处置路径：指派责任人 → 登记处理计划（历史积压允许保持 open）→ 结构化关闭
 *   （关闭原因 / 实际根因 / 实际处置 / 验证结果 / 对智能体判断的评价）或「重试修复」
 *   （新幂等键重启完整修复链路）；需求来源的待办（entry.requirement）重试时重启需求
 *   修复流（批准版方案 → 沙箱 → 审批 → 发布）。
 * - 知识回流（M2）：关闭时勾选「提交知识库候选」进入 DRAFT；管理员在详情弹窗审核
 *   （approve / reject / revoke），批准后 BFF 同步导出语料，索引重建仍是显式动作。
 * - 内容管理：关键词搜索（监控系统 / 服务 / 升级原因 / 告警 / 工作流 / 责任人 / 备注 / 处置计划）+ 监控系统
 *   筛选（按当前数据动态生成选项与计数；纳管应用显示应用名，node / redis 等未纳管服务回退服务名
 *   并标注）+ 来源筛选（告警 / 需求）+ 仅看超时；
 *   状态页签带全量计数；列表为统一表格（监控系统 / 状态 / 升级原因 / 告警·工作流 / 责任人 /
 *   登记·SLA / 日志证据 / 操作），点击表头自动排序（升降切换，aria-sort 标注），默认按监控系统
 *   分块 + 待处置优先——均前端内存计算，状态页签仍走服务端。
 * - 排版：固定列宽（table-fixed + colgroup）铺满内容区，不再横向滚动内嵌——所有列一屏可见；
 *   长文本一律 truncate + title 悬停看全文；工作流 ID 即「详情」入口，行内操作区只留处置按钮。
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { FileText, RefreshCw, Search, X } from 'lucide-react'
import { api, describeError } from '../lib/api'
import { cn, fmtDateTime } from '../lib/format'
import { hasRole, useSelf } from '../lib/permission'
import { compareValues, nextSort, type SortState } from '../lib/sort'
import { useWriteAction } from '../lib/actions'
import { ActionButton } from '../components/ActionButton'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { CopyableId } from '../components/CopyableId'
import { EmptyState } from '../components/EmptyState'
import { LogEvidence } from '../components/LogEvidence'
import {
  Modal,
  fieldClass,
  primaryButtonClass,
  secondaryButtonClass,
} from '../components/Modal'
import { Pagination, usePagination } from '../components/Pagination'
import { PermissionGate } from '../components/PermissionGate'
import { SortableTh } from '../components/SortableTh'
import { Toast } from '../components/Toast'
import type {
  AgentVerdict,
  EscalationCloseReason,
  EscalationEntry,
  KnowledgeStatus,
} from '../lib/types'

const STATUS_TABS = [
  { value: 'all', label: '全部' },
  { value: 'open', label: '待指派' },
  { value: 'assigned', label: '已指派' },
  { value: 'closed', label: '已关闭' },
] as const

type StatusFilter = (typeof STATUS_TABS)[number]['value']

const STATUS_META: Record<EscalationEntry['status'], { label: string; cls: string }> = {
  open: { label: '待指派', cls: 'border-danger/40 text-danger' },
  assigned: { label: '已指派', cls: 'border-warn/40 text-warn' },
  closed: { label: '已关闭', cls: 'border-ok/40 text-ok' },
}

/** 筛选/排序下拉统一输入样式（同 Requirements / Audit 页 INPUT_CLS 族）。 */
const SELECT_CLS =
  'rounded-lg border border-line bg-panel px-2 py-1.5 text-xs text-ink focus:border-accent/50 focus:outline-none'

/** 可排序列（「告警 / 工作流」与操作列不参与排序；default 为「监控系统分块 + 待处置优先」）。 */
type ColumnKey = 'system' | 'status' | 'reason' | 'assignee' | 'escalated_at'
type SortKey = ColumnKey | 'default'

/** 默认排序：按监控系统分块（组内待处置优先）；点击表头后切换为按列排序。 */
const DEFAULT_SORT: SortState<SortKey> = { key: 'default', dir: 'asc' }

/** 状态权重（默认排序组内：open → assigned → closed）。 */
const STATUS_RANK: Record<EscalationEntry['status'], number> = { open: 0, assigned: 1, closed: 2 }

/** 处置分类展示（M1）：服务端已给 category_label，这里补色值；未知分类回退「其他」。 */
const CATEGORY_META: Record<string, { label: string; cls: string }> = {
  model: { label: '模型低置信', cls: 'border-danger/40 text-danger' },
  approval: { label: '审批未通过', cls: 'border-warn/40 text-warn' },
  sandbox: { label: '测试未通过', cls: 'border-warn/40 text-warn' },
  release: { label: '发布劣化', cls: 'border-danger/40 text-danger' },
  observability: { label: '观测异常', cls: 'border-accent/40 text-accent' },
  evidence: { label: '证据不足', cls: 'border-line text-muted' },
  requirement: { label: '需求来源', cls: 'border-accent/40 text-accent' },
  other: { label: '其他', cls: 'border-line text-idle' },
}

/** 证据质量提示（M1）：仅在非 OK 时显示，避免每行都堆徽标。 */
const EVIDENCE_HINT: Record<string, { label: string; cls: string }> = {
  LOKI_UNAVAILABLE: { label: 'Loki 不可用', cls: 'border-danger/40 text-danger' },
  NO_LOGS: { label: '无日志', cls: 'border-danger/40 text-danger' },
  STALE_LOGS: { label: '日志过期', cls: 'border-warn/40 text-warn' },
  UNMAPPED_SERVICE: { label: '标签未映射', cls: 'border-warn/40 text-warn' },
  INSUFFICIENT_SIGNAL: { label: '信号不足', cls: 'border-warn/40 text-warn' },
  UNKNOWN: { label: '质量未记录', cls: 'border-line text-idle' },
}

/** 结构化关闭原因（M2；与服务端 CLOSE_REASONS 同源）。 */
const CLOSE_REASONS: { value: EscalationCloseReason; label: string }[] = [
  { value: 'RESOLVED', label: '已修复' },
  { value: 'FALSE_POSITIVE', label: '误报' },
  { value: 'OBSERVABILITY_FIXED', label: '观测已修复' },
  { value: 'DUPLICATE', label: '重复告警' },
  { value: 'NO_CODE_CHANGE', label: '无需改代码' },
  { value: 'OTHER', label: '其他' },
]

/** 对智能体判断的评价（人工复盘口径）。 */
const VERDICTS: { value: AgentVerdict; label: string }[] = [
  { value: 'CORRECT', label: '判断正确' },
  { value: 'PARTLY_CORRECT', label: '部分正确' },
  { value: 'INCORRECT', label: '判断错误' },
  { value: 'UNKNOWN', label: '未评价' },
]

/** 知识审核状态徽标（M2）。 */
const KNOWLEDGE_META: Record<KnowledgeStatus, { label: string; cls: string }> = {
  '': { label: '未提交', cls: 'border-line text-idle' },
  DRAFT: { label: '待审核', cls: 'border-warn/40 text-warn' },
  APPROVED: { label: '已入库', cls: 'border-ok/40 text-ok' },
  REJECTED: { label: '已拒绝', cls: 'border-danger/40 text-danger' },
}

const verdictLabelOf = (value: AgentVerdict | undefined): string =>
  VERDICTS.find((item) => item.value === value)?.label ?? '未评价'

/** 分类取值（旧数据可能缺字段：按 other 归并，保证筛选不丢条目）。 */
const categoryOf = (entry: EscalationEntry): string => entry.category || 'other'

/** 分类中文标签（服务端 category_label 已给出，前端仅用于下拉选项排序/回退）。 */
const categoryLabelOf = (key: string): string =>
  CATEGORY_META[key]?.label ?? CATEGORY_META.other.label

const tsOf = (value: string | null | undefined): number => (value ? Date.parse(value) : 0)

/** 待办所属监控系统（服务端派生：纳管取应用名，未纳管回退服务名；分块 / 筛选 / 搜索同一口径）。 */
const systemLabelOf = (entry: EscalationEntry) => entry.system || entry.service || '未标注系统'

/** 待处置优先：未关闭在前 → 超时在前 → 状态权重 → 早登记先处置。 */
function compareUrgency(a: EscalationEntry, b: EscalationEntry): number {
  const closed = Number(a.status === 'closed') - Number(b.status === 'closed')
  if (closed) return closed
  const overdue = Number(b.overdue) - Number(a.overdue)
  if (overdue) return overdue
  const rank = STATUS_RANK[a.status] - STATUS_RANK[b.status]
  if (rank) return rank
  return tsOf(a.escalated_at) - tsOf(b.escalated_at)
}

/** 列排序取值（状态 / SLA 用权重或布尔，其余字符串化）。 */
function sortValue(entry: EscalationEntry, key: ColumnKey): string | number {
  switch (key) {
    case 'system':
      return systemLabelOf(entry)
    case 'status':
      return STATUS_RANK[entry.status]
    case 'reason':
      return entry.auto_reason || ''
    case 'assignee':
      return entry.assignee || ''
    case 'escalated_at':
      return tsOf(entry.escalated_at)
  }
}

/** 列排序比较器（同值按登记时间升序兜底）。 */
function compareByColumn(
  a: EscalationEntry,
  b: EscalationEntry,
  key: ColumnKey,
  dir: SortState['dir'],
): number {
  return (
    compareValues(sortValue(a, key), sortValue(b, key), dir) ||
    tsOf(a.escalated_at) - tsOf(b.escalated_at)
  )
}

/** 指派/改派责任人（提交即热生效，落操作审计）。 */
function AssignDialog({ entry, onClose }: { entry: EscalationEntry | null; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [assignee, setAssignee] = useState(entry?.assignee ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const close = () => {
    if (busy) return
    onClose()
  }

  const submit = async () => {
    if (!entry) return
    if (!assignee.trim()) {
      setError('责任人不能为空')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.assignEscalation(entry.id, assignee.trim())
      await queryClient.invalidateQueries()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={entry !== null}
      title={entry?.assignee ? '改派责任人' : '指派责任人'}
      onClose={close}
      footer={
        <>
          <button type="button" className={secondaryButtonClass} onClick={close}>
            取消
          </button>
          <button type="button" className={primaryButtonClass} disabled={busy} onClick={submit}>
            {busy ? '提交中…' : entry?.assignee ? '改派' : '指派'}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted">
          <div>服务：{entry?.service || '—'}</div>
          <div className="truncate" title={entry?.auto_reason}>
            原因：{entry?.auto_reason || '—'}
          </div>
          <div className="col-span-2">
            工作流：<span className="font-mono text-xs">{entry?.wf_id ?? '—'}</span>
          </div>
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">责任人（值班同学 / 小组）*</label>
          <input
            autoFocus
            value={assignee}
            onChange={(event) => setAssignee(event.target.value)}
            placeholder="例如 zhangsan 或 支付值班组"
            className={fieldClass}
          />
        </div>
        {entry?.assignee && (
          <p className="text-xs text-idle">当前责任人：{entry.assignee}（提交即改派）</p>
        )}
        {error && <p className="text-xs text-danger">{error}</p>}
      </div>
    </Modal>
  )
}

/** 处置关闭（M2）：结构化记录写入 resolution，备注同时进入处置轨迹与操作审计。
 *
 * RESOLVED（已修复）必须填写实际根因 / 实际处置 / 验证结果——这是知识回流的入索引条件；
 * 其他原因至少给出一句说明。勾选「提交知识库候选」须先确认已检查敏感信息。
 */
function CloseDialog({ entry, onClose }: { entry: EscalationEntry; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState<EscalationCloseReason>('RESOLVED')
  const [rootCause, setRootCause] = useState('')
  const [action, setAction] = useState('')
  const [verification, setVerification] = useState('')
  const [verdict, setVerdict] = useState<AgentVerdict>('UNKNOWN')
  const [note, setNote] = useState('')
  const [candidate, setCandidate] = useState(false)
  const [sensitive, setSensitive] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const resolved = reason === 'RESOLVED'
  const missing = resolved
    ? !rootCause.trim() || !action.trim() || !verification.trim()
    : !(note.trim() || action.trim() || rootCause.trim())
  const disabled = busy || missing || (candidate && !sensitive)

  const changeReason = (next: EscalationCloseReason) => {
    setReason(next)
    if (next !== 'RESOLVED') {
      setCandidate(false)
      setSensitive(false)
    }
  }

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.closeEscalation(entry.id, {
        close_reason: reason,
        actual_root_cause: rootCause.trim(),
        actual_action: action.trim(),
        verification: verification.trim(),
        verdict,
        note: note.trim(),
        knowledge_candidate: candidate,
        sensitive_confirmed: sensitive,
      })
      await queryClient.invalidateQueries()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      size="lg"
      title="处置关闭"
      onClose={busy ? () => undefined : onClose}
      footer={
        <>
          <button type="button" className={secondaryButtonClass} onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className={primaryButtonClass}
            disabled={disabled}
            onClick={submit}
          >
            {busy ? '提交中…' : '确认关闭'}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted">
          <div>服务：{entry.service || '—'}</div>
          <div className="truncate" title={entry.auto_reason}>
            智能体判断：{entry.auto_reason || '—'}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1 block text-xs text-muted">关闭原因 *</label>
            <select
              value={reason}
              onChange={(event) => changeReason(event.target.value as EscalationCloseReason)}
              className={fieldClass}
            >
              {CLOSE_REASONS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">对智能体判断的评价</label>
            <select
              value={verdict}
              onChange={(event) => setVerdict(event.target.value as AgentVerdict)}
              className={fieldClass}
            >
              {VERDICTS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div>
          <label className="mb-1 block text-xs text-muted">
            实际根因{resolved ? ' *' : ''}（与智能体判断分开记录）
          </label>
          <textarea
            rows={2}
            value={rootCause}
            onChange={(event) => setRootCause(event.target.value)}
            placeholder="例如：数据库连接池耗尽导致核销请求超时"
            className={fieldClass}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">
            实际处置{resolved ? ' *' : ''}
          </label>
          <textarea
            rows={2}
            value={action}
            onChange={(event) => setAction(event.target.value)}
            placeholder="例如：调大连接池上限并滚动重启，已提交变更单"
            className={fieldClass}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">
            验证结果{resolved ? ' *' : ''}（可核对的指标 / 日志 / 时长）
          </label>
          <textarea
            rows={2}
            value={verification}
            onChange={(event) => setVerification(event.target.value)}
            placeholder="例如：错误率回落至 0.1% 以下并持续 30 分钟"
            className={fieldClass}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">处置备注（写入处置轨迹与操作审计）</label>
          <textarea
            rows={2}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="补充说明（其他原因时至少填写备注 / 处置 / 根因之一）"
            className={fieldClass}
          />
        </div>

        {resolved && (
          <div className="space-y-2 rounded-lg border border-line/70 p-3">
            <label className="flex items-start gap-2 text-xs text-muted">
              <input
                type="checkbox"
                checked={candidate}
                onChange={(event) => {
                  setCandidate(event.target.checked)
                  if (!event.target.checked) setSensitive(false)
                }}
                className="mt-0.5"
              />
              <span>
                提交知识库候选（进入待审核队列）
                <span className="mt-0.5 block text-[11px] text-idle">
                  管理员审核通过后写入 knowledge_records.json；索引重建仍是显式动作。
                </span>
              </span>
            </label>
            {candidate && (
              <label className="flex items-start gap-2 text-xs text-muted">
                <input
                  type="checkbox"
                  checked={sensitive}
                  onChange={(event) => setSensitive(event.target.checked)}
                  className="mt-0.5"
                />
                <span>
                  已检查以上内容不含凭据、令牌、内部地址与个人信息（必选）
                </span>
              </label>
            )}
          </div>
        )}

        {missing && (
          <p className="text-xs text-idle">
            {resolved
              ? '关闭原因为「已修复」时，实际根因 / 实际处置 / 验证结果三项必填。'
              : '请至少填写处置说明（备注 / 实际处置 / 实际根因之一）。'}
          </p>
        )}
        {error && <p className="text-xs text-danger">{error}</p>}
      </div>
    </Modal>
  )
}

/** 处置计划：未关闭待办登记下一步动作（允许保持 open，不批量关闭历史积压）。 */
function PlanDialog({ entry, onClose }: { entry: EscalationEntry; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [plan, setPlan] = useState(entry.plan ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.planEscalation(entry.id, plan.trim())
      await queryClient.invalidateQueries()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      title="处置计划"
      onClose={onClose}
      footer={
        <>
          <button type="button" className={secondaryButtonClass} onClick={onClose}>
            取消
          </button>
          <button type="button" className={primaryButtonClass} disabled={busy} onClick={submit}>
            {busy ? '保存中…' : '保存计划'}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted">
          <div>服务：{entry.service || '—'}</div>
          <div className="truncate" title={entry.wf_id}>
            工作流：<span className="font-mono text-xs">{entry.wf_id}</span>
          </div>
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">
            下一步动作（谁在何时做什么；可留空清除）
          </label>
          <textarea
            rows={3}
            value={plan}
            onChange={(event) => setPlan(event.target.value)}
            placeholder="例如：本周五前核对 Loki 标签映射并重试；若日志仍缺失则转观测平台排查"
            className={fieldClass}
          />
        </div>
        {error && <p className="text-xs text-danger">{error}</p>}
      </div>
    </Modal>
  )
}

/** 处置详情（M2）：三段式对照（智能体判断 / 实际根因 / 验证结果）+ 知识审核入口。 */
function DetailDialog({ entry, onClose }: { entry: EscalationEntry; onClose: () => void }) {
  const queryClient = useQueryClient()
  const self = useSelf()
  const isAdmin = hasRole(self?.role, 'admin')
  const resolution = entry.resolution ?? null
  const knowledge = (entry.knowledge_status ?? '') as KnowledgeStatus
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const review = async (decision: 'approve' | 'reject' | 'revoke') => {
    setBusy(true)
    setError(null)
    try {
      await api.reviewEscalationKnowledge(entry.id, decision, reason.trim())
      await queryClient.invalidateQueries()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const block = (title: string, value: string, empty = '—') => (
    <div className="rounded-lg border border-line/70 p-3">
      <div className="text-[11px] text-idle">{title}</div>
      <div className="mt-1 whitespace-pre-wrap text-xs text-ink">{value || empty}</div>
    </div>
  )

  return (
    <Modal
      open
      size="lg"
      title={`处置详情 · ${entry.service || '未知服务'}`}
      onClose={onClose}
      footer={
        <button type="button" className={secondaryButtonClass} onClick={onClose}>
          关闭
        </button>
      }
    >
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted">
          <div>
            告警 <span className="font-mono">{entry.alert_id || '—'}</span>
          </div>
          <div className="truncate" title={entry.wf_id}>
            工作流 <span className="font-mono">{entry.wf_id}</span>
          </div>
          <div>
            关闭人：{resolution?.closed_by || entry.assignee || '—'}
          </div>
          <div>关闭时间：{resolution?.closed_at ? fmtDateTime(resolution.closed_at) : fmtDateTime(entry.closed_at)}</div>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {entry.close_reason_label && (
            <span className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted">
              关闭原因：{entry.close_reason_label}
            </span>
          )}
          <span className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted">
            判断评价：{verdictLabelOf(resolution?.verdict)}
          </span>
          <span
            className={cn(
              'rounded border px-1.5 py-0.5 text-[10px]',
              KNOWLEDGE_META[knowledge].cls,
            )}
          >
            知识库：{KNOWLEDGE_META[knowledge].label}
          </span>
          {entry.plan && (
            <span className="rounded border border-accent/40 px-1.5 py-0.5 text-[10px] text-accent">
              处置计划：{entry.plan}
            </span>
          )}
        </div>

        <div className="grid gap-2 md:grid-cols-3">
          {block('智能体判断（升级原因）', entry.auto_reason)}
          {block('实际根因（人工）', resolution?.actual_root_cause ?? '')}
          {block('验证结果（人工）', resolution?.verification ?? '')}
        </div>
        {block('实际处置（人工）', resolution?.actual_action ?? '')}
        {resolution?.note && block('处置备注', resolution.note)}

        {!resolution && (
          <p className="rounded-lg border border-line/70 p-3 text-xs text-idle">
            该待办为旧格式关闭（仅备注，无结构化处置记录），不参与知识回流；
            备注：{entry.note || '—'}
          </p>
        )}

        {resolution && !resolution.knowledge_candidate && (
          <p className="text-xs text-idle">
            本次关闭未提交知识库候选（仅 RESOLVED 且勾选候选的记录可进入知识库）。
          </p>
        )}

        {resolution?.knowledge_candidate && (
          <div className="space-y-2 rounded-lg border border-line/70 p-3">
            <div className="text-xs text-muted">
              知识审核：{KNOWLEDGE_META[knowledge].label}
              {resolution.knowledge_reviewed_by && (
                <span className="ml-2 text-idle">
                  {resolution.knowledge_reviewed_by}
                  {resolution.knowledge_reviewed_at
                    ? ` · ${fmtDateTime(resolution.knowledge_reviewed_at)}`
                    : ''}
                </span>
              )}
            </div>
            {resolution.knowledge_reject_reason && (
              <div className="text-xs text-danger">
                审核意见：{resolution.knowledge_reject_reason}
              </div>
            )}
            {isAdmin ? (
              <div className="space-y-2">
                <input
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="审核意见（拒绝 / 撤销时填写）"
                  className={fieldClass}
                />
                <div className="flex flex-wrap gap-1.5">
                  {knowledge !== 'APPROVED' && (
                    <button
                      type="button"
                      className={primaryButtonClass}
                      disabled={busy}
                      onClick={() => review('approve')}
                    >
                      批准入库
                    </button>
                  )}
                  <button
                    type="button"
                    className={secondaryButtonClass}
                    disabled={busy}
                    onClick={() => review('reject')}
                  >
                    拒绝
                  </button>
                  {knowledge === 'APPROVED' && (
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      disabled={busy}
                      onClick={() => review('revoke')}
                    >
                      撤销入库
                    </button>
                  )}
                </div>
                <p className="text-[11px] text-idle">
                  批准后导出 knowledge_records.json，索引重建仍需显式执行（见部署手册）。
                </p>
              </div>
            ) : (
              <p className="text-[11px] text-idle">知识审核仅管理员可操作。</p>
            )}
            {error && <p className="text-xs text-danger">{error}</p>}
          </div>
        )}
      </div>
    </Modal>
  )
}

export function Escalations() {
  const write = useWriteAction()
  const [status, setStatus] = useState<StatusFilter>('all')
  const [query, setQuery] = useState('')
  const [filterSystem, setFilterSystem] = useState('')
  const [filterCategory, setFilterCategory] = useState('')
  const [filterSource, setFilterSource] = useState('')
  const [onlyOverdue, setOnlyOverdue] = useState(false)
  const [sort, setSort] = useState<SortState<SortKey>>(DEFAULT_SORT)
  const [assignTarget, setAssignTarget] = useState<EscalationEntry | null>(null)
  const [closeTarget, setCloseTarget] = useState<EscalationEntry | null>(null)
  const [evidenceTarget, setEvidenceTarget] = useState<EscalationEntry | null>(null)
  const [planTarget, setPlanTarget] = useState<EscalationEntry | null>(null)
  const [detailTarget, setDetailTarget] = useState<EscalationEntry | null>(null)

  const { data, isError, error } = useQuery({
    queryKey: ['escalations', status],
    queryFn: () => api.escalations(status),
    refetchInterval: 3000,
  })

  /** 重置全部内容管理条件。 */
  const resetConditions = () => {
    setQuery('')
    setFilterSystem('')
    setFilterCategory('')
    setFilterSource('')
    setOnlyOverdue(false)
    setSort(DEFAULT_SORT)
  }

  /** 点击表头：同列翻转升降；换列按直觉默认方向（登记时间 / SLA 降序，其余升序）。 */
  const toggleSort = (key: ColumnKey) => {
    setSort((prev) => nextSort(prev, key, key === 'escalated_at' ? 'desc' : 'asc'))
  }

  // ---- 内容管理：搜索 + 筛选 + 排序（前端内存计算；状态页签仍走服务端过滤）----
  const items = data?.escalations ?? []
  const q = query.trim().toLowerCase()
  const matched = items.filter((entry) => {
    if (filterSystem && systemLabelOf(entry) !== filterSystem) return false
    if (filterCategory && categoryOf(entry) !== filterCategory) return false
    if (filterSource === 'requirement' && !entry.requirement) return false
    if (filterSource === 'alert' && entry.requirement) return false
    if (onlyOverdue && !entry.overdue) return false
    if (!q) return true
    // 命中范围：监控系统 / 服务 / 升级原因 / 告警 / 工作流 / 责任人 / 备注 / 来源标签 / 处置分类
    const haystack = [
      systemLabelOf(entry),
      entry.service,
      entry.auto_reason,
      entry.alert_id,
      entry.wf_id,
      entry.assignee,
      entry.note,
      entry.plan,
      entry.close_reason_label,
      entry.knowledge_label,
      entry.resolution?.actual_root_cause,
      entry.resolution?.actual_action,
      entry.resolution?.verification,
      entry.requirement ? '需求' : '告警',
      CATEGORY_META[categoryOf(entry)]?.label ?? '',
    ]
      .map((value) => String(value ?? ''))
      .join('\n')
      .toLowerCase()
    return haystack.includes(q)
  })
  // ---- 监控系统维度：筛选下拉的动态选项与计数（按当前页签数据的系统计数降序）----
  const systemCounts = new Map<string, number>()
  for (const entry of items) {
    const label = systemLabelOf(entry)
    systemCounts.set(label, (systemCounts.get(label) ?? 0) + 1)
  }
  const systemPairs = [...systemCounts.entries()].sort(
    (a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh'),
  )
  // ---- 处置分类维度：分类筛选项与计数（按当前页签数据；只列出实际出现的分类）----
  const categoryCounts = new Map<string, number>()
  for (const entry of items) {
    const key = categoryOf(entry)
    categoryCounts.set(key, (categoryCounts.get(key) ?? 0) + 1)
  }
  const categoryPairs = [...categoryCounts.entries()].sort(
    (a, b) => b[1] - a[1] || categoryLabelOf(a[0]).localeCompare(categoryLabelOf(b[0]), 'zh'),
  )
  /** 默认排序的系统分块次序（与下拉选项同序：条数降序，切换筛选时保持稳定）。 */
  const systemRank = new Map(systemPairs.map(([label], index) => [label, index] as const))
  const sorted = [...matched].sort((a, b) => {
    if (sort.key !== 'default') return compareByColumn(a, b, sort.key, sort.dir)
    const bySystem =
      (systemRank.get(systemLabelOf(a)) ?? 0) - (systemRank.get(systemLabelOf(b)) ?? 0)
    return bySystem || compareUrgency(a, b)
  })
  const overdueCount = items.filter((entry) => entry.overdue).length

  const hasConditions = Boolean(
    q || filterSystem || filterCategory || filterSource || onlyOverdue || sort.key !== 'default',
  )

  // ---- 分页：状态页签 / 条件变化回到第 1 页（越界页码自动夹取）----
  const pg = usePagination(sorted.length, 10)
  const { setPage } = pg
  useEffect(() => {
    setPage(1)
  }, [status, query, filterSystem, filterCategory, filterSource, onlyOverdue, sort, setPage])
  const paged = sorted.slice(pg.start, pg.start + pg.pageSize)

  /** 页签计数（stats 为全量口径，不随 status 变化）。 */
  const tabCount = (tab: StatusFilter): number => {
    const stats = data?.stats
    if (!stats) return 0
    if (tab === 'open') return stats.open
    if (tab === 'assigned') return stats.assigned
    if (tab === 'closed') return stats.closed
    return stats.total
  }

  const openRetry = (entry: EscalationEntry) => {
    write.open({
      title: '确认重试修复？',
      confirmLabel: '启动新流程',
      detail: (
        <div className="space-y-2 text-sm">
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
            <div>服务：{entry.service || '—'}</div>
            <div>原告警：{entry.alert_id || '—'}</div>
            <div className="col-span-2">
              工作流：<span className="font-mono text-xs">{entry.wf_id}</span>
            </div>
          </div>
          <p className="text-xs text-idle">
            {entry.requirement
              ? '需求来源待办：以新幂等键重启需求修复链路（批准版方案 → 补丁 → 沙箱 → 审批 → 公告 → 发布），不重走日志诊断；启动成功后本待办自动关闭。'
              : '以新幂等键（自动追加 retry 序号）重启完整修复链路：诊断 → 补丁 → 沙箱 → 审批 → 发布；启动成功后本待办自动关闭。'}
          </p>
        </div>
      ),
      run: () => api.retryEscalation(entry.id),
      success: '已启动重试修复流程',
    })
  }

  return (
    <div>
      <h1 className="text-lg font-medium">转人工待办</h1>
      <p className="mt-1 text-xs text-muted">
        升级人工后的处置闭环（3 秒自动刷新）：指派责任人 → 处置关闭或重试修复；超时未处置将自动再升级
      </p>

      {/* 状态页签（计数为全量口径）+ SLA 说明 */}
      <div className="mt-4 flex flex-wrap items-center gap-1.5">
        {STATUS_TABS.map((tab) => (
          <button
            key={tab.value}
            type="button"
            onClick={() => setStatus(tab.value)}
            className={cn(
              'rounded-lg px-3 py-1 text-xs transition-colors',
              status === tab.value
                ? 'bg-elevated text-ink'
                : 'text-muted hover:bg-elevated hover:text-ink',
            )}
          >
            {tab.label}
            <span className={cn('ml-1 font-mono', status === tab.value ? 'text-accent' : 'text-idle')}>
              {tabCount(tab.value)}
            </span>
          </button>
        ))}
        {data && (
          <span className="ml-auto flex items-center gap-3 text-xs text-idle">
            {(data.stats.knowledge?.draft ?? 0) > 0 && (
              <button
                type="button"
                onClick={() => setStatus('closed')}
                title="已关闭且提交知识库候选、等待管理员审核的记录"
                className="rounded border border-warn/40 px-1.5 py-0.5 text-[10px] text-warn transition-colors hover:bg-warn/10"
              >
                知识待审核 {data.stats.knowledge?.draft}
              </button>
            )}
            <span>
              SLA {data.stats.sla_minutes} 分钟：超时未处置自动再升级一次（每单一次，防轰炸）
            </span>
          </span>
        )}
      </div>

      {data && (
        <>
          {/* 工具条：搜索 + 来源筛选 + 仅看超时 + 排序 + 计数 */}
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search
                size={13}
                className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-idle"
              />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Escape') setQuery('')
                }}
                placeholder="搜索服务 / 原因 / 告警 / 工作流 / 责任人"
                className="w-72 rounded-lg border border-line bg-panel py-1.5 pl-8 pr-7 text-xs text-ink placeholder:text-idle focus:border-accent/50 focus:outline-none"
              />
              {query && (
                <button
                  type="button"
                  onClick={() => setQuery('')}
                  title="清除搜索"
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-idle transition-colors hover:text-ink"
                >
                  <X size={13} />
                </button>
              )}
            </div>
            {systemPairs.length > 0 && (
              <select
                value={filterSystem}
                onChange={(event) => setFilterSystem(event.target.value)}
                className={SELECT_CLS}
                title="按监控系统筛选：选项随当前待办动态生成（未纳管服务回退服务名）"
              >
                <option value="">全部监控系统</option>
                {systemPairs.map(([label, count]) => (
                  <option key={label} value={label}>
                    {label}（{count}）
                  </option>
                ))}
              </select>
            )}
            <select
              value={filterSource}
              onChange={(event) => setFilterSource(event.target.value)}
              className={SELECT_CLS}
            >
              <option value="">全部来源</option>
              <option value="alert">告警来源</option>
              <option value="requirement">需求来源</option>
            </select>
            {categoryPairs.length > 0 && (
              <select
                value={filterCategory}
                onChange={(event) => setFilterCategory(event.target.value)}
                className={SELECT_CLS}
                title="按处置分类筛选：观测异常 / 证据不足与业务闸门分开统计"
              >
                <option value="">全部处置分类</option>
                {categoryPairs.map(([key, count]) => (
                  <option key={key} value={key}>
                    {categoryLabelOf(key)}（{count}）
                  </option>
                ))}
              </select>
            )}
            {overdueCount > 0 && (
              <button
                type="button"
                onClick={() => setOnlyOverdue((value) => !value)}
                title="只看已超 SLA 未处置的待办"
                className={cn(
                  'rounded-lg border px-2 py-1.5 text-xs transition-colors',
                  onlyOverdue
                    ? 'border-danger/40 bg-danger/10 text-danger'
                    : 'border-line text-muted hover:bg-elevated hover:text-ink',
                )}
              >
                仅看超时（{overdueCount}）
              </button>
            )}
            {hasConditions && (
              <button
                type="button"
                onClick={resetConditions}
                className="rounded-lg border border-line px-2 py-1.5 text-xs text-muted transition-colors hover:bg-elevated hover:text-ink"
              >
                重置条件
              </button>
            )}
            <span className="ml-auto text-xs text-muted">
              显示 <span className="text-ink">{sorted.length}</span> / 共 {items.length} 条
            </span>
          </div>
        </>
      )}

      {isError && (
        <div className="mt-5">
          <EmptyState title="无法加载待办列表" hint={describeError(error)} />
        </div>
      )}

      {data &&
        (items.length ? (
          sorted.length === 0 ? (
            <div className="mt-5">
              <EmptyState
                title="没有匹配的待办"
                hint="试试调整关键词，或重置筛选条件查看全部待办"
              />
              <div className="mt-3 flex justify-center">
                <button
                  type="button"
                  onClick={resetConditions}
                  className="rounded-lg border border-accent/40 px-3.5 py-1.5 text-xs font-medium text-accent transition-colors hover:bg-accent/10"
                >
                  重置条件
                </button>
              </div>
            </div>
          ) : (
          <>
          <div className="mt-5">
            <table className="w-full table-fixed text-xs">
              <colgroup>
                <col className="w-[14%]" />
                <col className="w-[11%]" />
                <col className="w-[14%]" />
                <col className="w-[15%]" />
                <col className="w-[8%]" />
                <col className="w-[12%]" />
                <col className="w-[10%]" />
                <col className="w-[16%]" />
              </colgroup>
              <thead>
                <tr className="border-b border-line text-left text-muted">
                  <SortableTh
                    label="监控系统"
                    colKey="system"
                    sort={sort}
                    onToggle={toggleSort}
                    padding="px-2.5 py-2"
                    className="sticky top-12 z-10 bg-canvas"
                  />
                  <SortableTh
                    label="状态"
                    colKey="status"
                    sort={sort}
                    onToggle={toggleSort}
                    padding="px-2.5 py-2"
                    className="sticky top-12 z-10 bg-canvas"
                  />
                  <SortableTh
                    label="升级原因"
                    colKey="reason"
                    sort={sort}
                    onToggle={toggleSort}
                    padding="px-2.5 py-2"
                    className="sticky top-12 z-10 bg-canvas"
                  />
                  <th className="sticky top-12 z-10 whitespace-nowrap bg-canvas px-2.5 py-2 font-medium">
                    告警 / 工作流
                  </th>
                  <SortableTh
                    label="责任人"
                    colKey="assignee"
                    sort={sort}
                    onToggle={toggleSort}
                    padding="px-2.5 py-2"
                    className="sticky top-12 z-10 bg-canvas"
                  />
                  <SortableTh
                    label="登记 / SLA"
                    colKey="escalated_at"
                    sort={sort}
                    onToggle={toggleSort}
                    padding="px-2.5 py-2"
                    className="sticky top-12 z-10 bg-canvas"
                  />
                  <th className="sticky top-12 z-10 whitespace-nowrap bg-canvas px-2.5 py-2 font-medium">
                    日志证据
                  </th>
                  <th className="sticky top-12 z-10 whitespace-nowrap bg-canvas px-2.5 py-2 text-right font-medium">
                    操作
                  </th>
                </tr>
              </thead>
              <tbody>
                {paged.map((entry) => {
                  const meta = STATUS_META[entry.status]
                  const category = categoryOf(entry)
                  const categoryMeta = CATEGORY_META[category] ?? CATEGORY_META.other
                  const qualityHint = (
                    entry.evidence?.quality_status && entry.evidence.quality_status !== 'OK'
                      ? EVIDENCE_HINT[entry.evidence.quality_status]
                      : undefined
                  )
                  const open = entry.status !== 'closed'
                  return (
                    <tr
                      key={entry.id}
                      className="border-b border-line/60 last:border-0 hover:bg-elevated/40"
                    >
                      <td className="px-2.5 py-2.5 align-top">
                        <div className="flex min-w-0 items-center gap-1.5">
                          <span
                            className="truncate font-medium text-ink"
                            title={`监控系统：${systemLabelOf(entry)}`}
                          >
                            {systemLabelOf(entry)}
                          </span>
                          {!entry.system_managed && (
                            <span
                              className="shrink-0 rounded border border-line px-1 py-0.5 text-[10px] text-idle"
                              title="未在「被监控系统」清单中纳管"
                            >
                              未纳管
                            </span>
                          )}
                        </div>
                        {entry.service && entry.service !== systemLabelOf(entry) && (
                          <div
                            className="mt-0.5 truncate font-mono text-[10px] text-idle"
                            title={`服务标识（告警 service）：${entry.service}`}
                          >
                            {entry.service}
                          </div>
                        )}
                      </td>
                      <td className="px-2.5 py-2.5 align-top">
                        <div className="flex flex-wrap items-center gap-1">
                          <span
                            className={cn(
                              'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px]',
                              meta.cls,
                            )}
                          >
                            {meta.label}
                            {entry.re_escalated && (
                              <span
                                className="inline-flex"
                                title="已超 SLA 未处置，BFF 巡检已自动再升级一次"
                              >
                                <RefreshCw size={10} />
                              </span>
                            )}
                          </span>
                          {entry.requirement && (
                            <span className="rounded border border-accent/40 px-1.5 py-0.5 text-[10px] text-accent">
                              需求
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-2.5 py-2.5 align-top">
                        {category !== 'requirement' && (
                          <span
                            className={cn(
                              'mb-0.5 inline-flex rounded border px-1 py-0.5 text-[10px]',
                              categoryMeta.cls,
                            )}
                            title={`处置分类：${categoryMeta.label}`}
                          >
                            {categoryMeta.label}
                          </span>
                        )}
                        <div className="truncate text-ink" title={entry.auto_reason}>
                          {entry.auto_reason || '—'}
                        </div>
                        {open && entry.plan && (
                          <div
                            className="mt-0.5 truncate text-[10px] text-accent"
                            title={`处置计划：${entry.plan}`}
                          >
                            计划：{entry.plan}
                          </div>
                        )}
                        {!open && entry.note && (
                          <div className="mt-0.5 truncate text-[10px] text-idle" title={entry.note}>
                            备注：{entry.note}
                          </div>
                        )}
                        {!open && (entry.close_reason_label || entry.resolution?.knowledge_candidate) && (
                          <div className="mt-1 flex flex-wrap items-center gap-1">
                            {entry.close_reason_label && (
                              <span
                                className="rounded border border-line px-1 py-0.5 text-[10px] text-muted"
                                title="结构化关闭原因"
                              >
                                {entry.close_reason_label}
                              </span>
                            )}
                            {entry.resolution?.knowledge_candidate && (
                              <span
                                className={cn(
                                  'rounded border px-1 py-0.5 text-[10px]',
                                  KNOWLEDGE_META[(entry.knowledge_status ?? '') as KnowledgeStatus].cls,
                                )}
                                title={
                                  entry.resolution.knowledge_reject_reason
                                    ? `知识审核：${entry.knowledge_label ?? '未提交'}（${entry.resolution.knowledge_reject_reason}）`
                                    : `知识审核：${entry.knowledge_label ?? '未提交'}`
                                }
                              >
                                {entry.knowledge_label ?? '未提交'}
                              </span>
                            )}
                          </div>
                        )}
                      </td>
                      <td className="px-2.5 py-2.5 align-top">
                        <div className="space-y-0.5 text-muted">
                          <div className="flex min-w-0 items-baseline gap-1">
                            <span className="shrink-0 text-[10px] text-idle">告警</span>
                            <CopyableId value={entry.alert_id || '—'} className="text-[11px]" />
                          </div>
                          <div className="flex min-w-0 items-baseline gap-1">
                            <span className="shrink-0 text-[10px] text-idle">流程</span>
                            <Link
                              to={`/flows/${encodeURIComponent(entry.wf_id)}`}
                              title={`查看流程详情：${entry.wf_id}`}
                              className="truncate font-mono text-[11px] text-accent hover:underline"
                            >
                              {entry.wf_id}
                            </Link>
                          </div>
                        </div>
                      </td>
                      <td className="px-2.5 py-2.5 align-top">
                        {entry.assignee ? (
                          <span className="block truncate text-ink" title={entry.assignee}>
                            {entry.assignee}
                          </span>
                        ) : (
                          <span className="text-idle">未指派</span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-2.5 py-2.5 align-top">
                        <div className="text-muted">{fmtDateTime(entry.escalated_at)}</div>
                        <div className="mt-0.5">
                          {!open ? (
                            <span className="text-[10px] text-idle">已关闭</span>
                          ) : entry.overdue ? (
                            <span
                              className="rounded border border-danger/40 bg-danger/10 px-1 py-0.5 text-[10px] text-danger"
                              title="已超 SLA 未处置，BFF 巡检已自动再升级一次"
                            >
                              已超时
                            </span>
                          ) : (
                            <span className="text-[10px] text-ok">SLA 正常</span>
                          )}
                        </div>
                      </td>
                      <td className="px-2.5 py-2.5 align-top">
                        {entry.evidence?.available ? (
                          <>
                            <button
                              type="button"
                              onClick={() => setEvidenceTarget(entry)}
                              title={`查看 TRIAGING 采集的原始日志与模板聚类：共 ${entry.evidence.total_lines} 行，归档 ${entry.evidence.stored_lines} 行${entry.evidence.truncated ? '（超出上限已截断）' : ''}，${entry.evidence.template_count} 个模板`}
                              className="inline-flex max-w-full items-center gap-1 rounded border border-accent/40 px-1 py-0.5 text-[10px] text-accent transition-colors hover:bg-accent/10"
                            >
                              <FileText size={10} className="shrink-0" />
                              <span className="truncate">
                                {entry.evidence.stored_lines}
                                {entry.evidence.truncated ? '+' : ''} 行 / {entry.evidence.template_count} 模板
                              </span>
                            </button>
                            {qualityHint && (
                              <span
                                className={cn(
                                  'mt-0.5 inline-flex rounded border px-1 py-0.5 text-[10px]',
                                  qualityHint.cls,
                                )}
                                title={entry.evidence.quality_detail || '证据质量预检结果'}
                              >
                                {qualityHint.label}
                              </span>
                            )}
                          </>
                        ) : (
                          <span
                            className="block truncate text-[10px] text-idle"
                            title={
                              entry.requirement
                                ? '需求来源待办不采集运行日志'
                                : '该流程未归档日志证据（仅新告警触发的流程会采集）'
                            }
                          >
                            {entry.requirement ? '需求来源' : '未归档'}
                          </span>
                        )}
                      </td>
                      <td className="px-2.5 py-2.5 align-top">
                        <div className="flex flex-wrap items-center justify-end gap-1.5">
                          {open ? (
                            <PermissionGate
                              require="operator"
                              fallback={<span className="text-[10px] text-idle">只读</span>}
                            >
                              <ActionButton size="sm" onClick={() => setAssignTarget(entry)}>
                                {entry.assignee ? '改派' : '指派'}
                              </ActionButton>
                              <ActionButton size="sm" onClick={() => setPlanTarget(entry)}>
                                计划
                              </ActionButton>
                              <ActionButton size="sm" onClick={() => setCloseTarget(entry)}>
                                关闭
                              </ActionButton>
                              <ActionButton size="sm" tone="accent" onClick={() => openRetry(entry)}>
                                重试
                              </ActionButton>
                            </PermissionGate>
                          ) : (
                            <>
                              <ActionButton size="sm" onClick={() => setDetailTarget(entry)}>
                                详情
                              </ActionButton>
                              {entry.knowledge_status === 'DRAFT' && (
                                <PermissionGate require="admin" fallback={null}>
                                  <ActionButton
                                    size="sm"
                                    tone="accent"
                                    onClick={() => setDetailTarget(entry)}
                                  >
                                    审核
                                  </ActionButton>
                                </PermissionGate>
                              )}
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <Pagination pg={pg} />
          </>
          )
        ) : (
          <div className="mt-5">
            <EmptyState
              title="当前没有转人工待办"
              hint="根因不确定 / 审批驳回 / 测试回炉耗尽 / 金丝雀劣化回滚的流程会自动登记到这里"
            />
          </div>
        ))}

      {assignTarget && (
        <AssignDialog
          key={assignTarget.id}
          entry={assignTarget}
          onClose={() => setAssignTarget(null)}
        />
      )}
      {closeTarget && (
        <CloseDialog key={closeTarget.id} entry={closeTarget} onClose={() => setCloseTarget(null)} />
      )}
      {planTarget && (
        <PlanDialog key={planTarget.id} entry={planTarget} onClose={() => setPlanTarget(null)} />
      )}
      {detailTarget && (
        <DetailDialog
          key={detailTarget.id}
          entry={detailTarget}
          onClose={() => setDetailTarget(null)}
        />
      )}
      {evidenceTarget && (
        <Modal
          open
          size="lg"
          title={`日志证据 · ${evidenceTarget.service || '未知服务'}`}
          onClose={() => setEvidenceTarget(null)}
        >
          <div className="mb-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
            <span>
              告警 <span className="font-mono">{evidenceTarget.alert_id || '—'}</span>
            </span>
            <span>
              工作流 <span className="font-mono">{evidenceTarget.wf_id}</span>
            </span>
            <span className="min-w-0 truncate" title={evidenceTarget.auto_reason}>
              升级原因：{evidenceTarget.auto_reason}
            </span>
          </div>
          <LogEvidence wfId={evidenceTarget.wf_id} />
        </Modal>
      )}

      <ConfirmDialog
        open={write.spec !== null}
        title={write.spec?.title ?? ''}
        tone={write.spec?.tone}
        confirmLabel={write.spec?.confirmLabel}
        busy={write.busy}
        error={write.error}
        onConfirm={write.confirm}
        onClose={write.close}
      >
        {write.spec?.detail}
      </ConfirmDialog>
      <Toast message={write.toast} />
    </div>
  )
}
