/** 流程详情：状态头 + 条件操作区（审批 / 窗口）+ 概览 / 事件 / 审批记录 / 留痕（设计方案 7.3）。 */

import type { ReactNode } from 'react'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, describeError, saveBlob } from '../lib/api'
import { cn, downloadJson, fmtDateTime, fmtDuration, shortHash } from '../lib/format'
import { useWriteAction } from '../lib/actions'
import { ActionButton } from '../components/ActionButton'
import { ApprovalDialog, useApprovalDialog } from '../components/ApprovalDialog'
import type { ApprovalHandle } from '../components/ApprovalDialog'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { CopyableId } from '../components/CopyableId'
import { CountdownText } from '../components/CountdownText'
import { DiffViewer } from '../components/DiffViewer'
import { EmptyState } from '../components/EmptyState'
import { EventTimeline } from '../components/EventTimeline'
import { FlowStateMachine } from '../components/charts/FlowStateMachine'
import { ReleasePipeline } from '../components/charts/ReleasePipeline'
import { JsonBlock } from '../components/JsonBlock'
import { LogEvidence } from '../components/LogEvidence'
import { PermissionGate } from '../components/PermissionGate'
import { QueuePatchDialog } from '../components/QueuePatchDialog'
import { StageBadge } from '../components/StageBadge'
import { Toast } from '../components/Toast'
import type { ResultPayload, StatusPayload, TimelineItem } from '../lib/types'

const TABS = [
  { key: 'overview', label: '概览' },
  { key: 'evidence', label: '日志证据' },
  { key: 'timeline', label: '事件时间线' },
  { key: 'approvals', label: '审批记录' },
  { key: 'artifacts', label: '留痕文件' },
] as const

type TabKey = (typeof TABS)[number]['key']

type WriteHandle = ReturnType<typeof useWriteAction>

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-3 text-sm">
      <span className="w-24 shrink-0 text-muted">{label}</span>
      <span className="min-w-0 flex-1">{children}</span>
    </div>
  )
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-line bg-panel p-5">
      <h3 className="mb-3 text-sm font-medium">{title}</h3>
      {children}
    </section>
  )
}

/** 确认对话框摘要（审批 / 窗口操作共用）。 */
function ActionSummary({
  wfId,
  service,
  alertId,
  decision,
}: {
  wfId: string
  service?: string
  alertId?: string
  decision: string
}) {
  return (
    <div className="space-y-1.5">
      <div>
        工作流：<span className="font-mono text-xs">{wfId}</span>
      </div>
      <div>服务：{service ?? '—'}</div>
      <div>
        告警：<span className="font-mono text-xs">{alertId ?? '—'}</span>
      </div>
      <div className="text-ink">决策：{decision}</div>
    </div>
  )
}

/** 审批入口：统一构造请求（证据绑定 + 职责分离提示），意见输入与提交由 ApprovalDialog 负责。 */
function openApprovalDecision(
  approval: ApprovalHandle,
  status: StatusPayload,
  wfId: string,
  gate: 'approval' | 'second_approval',
  decision: 'approve' | 'reject',
) {
  const first = gate === 'approval'
  approval.open({
    wfId,
    gate,
    decision,
    binding: status.evidence_binding ?? null,
    caseId: status.case_id || status.alert?.alert_id || '',
    summary: (
      <div className="space-y-1.5">
        <ActionSummary
          wfId={wfId}
          service={status.alert?.service}
          alertId={status.alert?.alert_id}
          decision={
            first
              ? decision === 'approve'
                ? '批准（进入公告窗口）'
                : '驳回（转人工处理）'
              : decision === 'approve'
                ? '二级批准（受保护目录变更）'
                : '二级驳回（转人工处理）'
          }
        />
        {status.requested_by && (
          <div className="text-[11px] text-idle">
            发起人：{status.requested_by}（职责分离：发起人不得审批自己的变更）
          </div>
        )}
      </div>
    ),
  })
}

/** 待审批操作面板（一级 / 二级）。 */
function ApprovalPanel({
  wfId,
  status,
  approval,
}: {
  wfId: string
  status: StatusPayload
  approval: ApprovalHandle
}) {
  const first = status.approval
  const second = status.second_approval

  return (
    <section className="mt-4 rounded-xl border border-warn/30 bg-warn/5 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-sm">
          <span className="text-warn">待审批</span>
          <span className="ml-3 text-xs text-muted">
            一级：{first ?? '待提交'} ·{' '}
            {status.needs_second ? `二级：${second ?? '待提交'}` : '无需二级审批'} · 剩余{' '}
            <CountdownText at={status.deadline?.at} />
          </span>
        </div>
        <div className="flex items-center gap-2">
          {first === null && (
            <PermissionGate require="operator">
              <ActionButton
                tone="accent"
                onClick={() => openApprovalDecision(approval, status, wfId, 'approval', 'approve')}
              >
                批准
              </ActionButton>
              <ActionButton
                tone="danger"
                onClick={() => openApprovalDecision(approval, status, wfId, 'approval', 'reject')}
              >
                驳回
              </ActionButton>
            </PermissionGate>
          )}
          {first === 'approve' && status.needs_second && second === null && (
            <PermissionGate require="operator">
              <ActionButton
                tone="accent"
                onClick={() => openApprovalDecision(approval, status, wfId, 'second_approval', 'approve')}
              >
                二级批准
              </ActionButton>
              <ActionButton
                tone="danger"
                onClick={() => openApprovalDecision(approval, status, wfId, 'second_approval', 'reject')}
              >
                二级驳回
              </ActionButton>
            </PermissionGate>
          )}
          {first === 'reject' && <span className="text-xs text-danger">一级已驳回，等待流程响应</span>}
          {first === 'approve' && (!status.needs_second || second !== null) && (
            <span className="text-xs text-ok">审批已完成，等待流程推进</span>
          )}
        </div>
      </div>
    </section>
  )
}

/** 公告窗口操作面板（deploy-now / cancel / 排队补丁）。 */
function WindowPanel({
  wfId,
  status,
  write,
  onQueue,
}: {
  wfId: string
  status: StatusPayload
  write: WriteHandle
  onQueue: () => void
}) {
  const service = status.alert?.service
  const alertId = status.alert?.alert_id
  return (
    <section className="mt-4 rounded-xl border border-warn/30 bg-warn/5 p-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="text-xs text-muted">公告期剩余</div>
          <CountdownText at={status.deadline?.at} className="mt-1 block text-3xl font-semibold" />
          <div className="mt-1 text-xs text-muted">到期后自动进入金丝雀发布</div>
        </div>
        <div className="flex items-center gap-2">
          <PermissionGate require="operator">
            <ActionButton
              tone="accent"
              onClick={() =>
                write.open({
                  title: '确认立即发布？',
                  tone: 'danger',
                  confirmLabel: '立即发布',
                  detail: (
                    <ActionSummary wfId={wfId} service={service} alertId={alertId} decision="跳过剩余公告倒计时，直接进入金丝雀发布" />
                  ),
                  run: () => api.deployCommand(wfId, 'deploy_now'),
                  success: '已下发 deploy-now，流程将进入金丝雀发布',
                })
              }
            >
              立即发布
            </ActionButton>
            <ActionButton
              tone="danger"
              onClick={() =>
                write.open({
                  title: '确认取消本次发布？',
                  tone: 'danger',
                  confirmLabel: '取消公告',
                  detail: (
                    <ActionSummary wfId={wfId} service={service} alertId={alertId} decision="中止公告窗口，本次修复不发布上线" />
                  ),
                  run: () => api.deployCommand(wfId, 'cancel'),
                  success: '已下发 cancel 命令',
                })
              }
            >
              取消公告
            </ActionButton>
            <ActionButton onClick={onQueue}>排队补丁</ActionButton>
          </PermissionGate>
        </div>
      </div>
      {status.queued_patches.length > 0 && (
        <div className="mt-3 border-t border-line pt-3 text-xs text-muted">
          队列（FIFO）：
          {status.queued_patches.map((patch, index) => (
            <span key={`${index}-${patch.alert_id}`} className="ml-2 font-mono text-ink">
              {patch.alert_id}
            </span>
          ))}
        </div>
      )}
    </section>
  )
}

/** 运行中状态摘要（终态前无 result）。 */
function StatusSummary({ status }: { status: StatusPayload }) {
  return (
    <Panel title="运行状态">
      {/* 全链路状态机（交互式 SVG）：当前阶段高亮、三道闸门与异常出口一目了然 */}
      <FlowStateMachine stage={status.stage} className="mb-4" />
      <div className="space-y-2">
        <Field label="阶段">
          <StageBadge stage={status.stage} />
        </Field>
        <Field label="服务 / 告警">
          {status.alert?.service ?? '—'}
          {status.alert?.alert_id && (
            <span className="ml-2 font-mono text-xs text-muted">{status.alert.alert_id}</span>
          )}
        </Field>
        <Field label="补丁 ID">
          {status.patch_id ? <CopyableId value={status.patch_id} /> : <span className="text-muted">尚未生成</span>}
        </Field>
        {status.case_id && (
          <Field label="案件 ID">
            <span className="font-mono text-xs">{status.case_id}</span>
          </Field>
        )}
        {status.requested_by && <Field label="发起人">{status.requested_by}</Field>}
        {status.parent_wf_id && (
          <Field label="重试来源">
            <span className="font-mono text-xs">
              {status.parent_wf_id}
              {status.parent_run_id ? ` · run ${shortHash(status.parent_run_id, 16)}` : ''}
            </span>
          </Field>
        )}
        {status.evidence_binding && (
          <Field label="证据绑定">
            <span className="font-mono text-[11px]">
              diff {shortHash(status.evidence_binding.diff_hash)} · 测试{' '}
              {shortHash(status.evidence_binding.test_report_hash)} · run{' '}
              {shortHash(status.evidence_binding.run_id, 16)}
            </span>
          </Field>
        )}
        <Field label="倒计时">
          {status.deadline ? <CountdownText at={status.deadline.at} /> : <span className="text-muted">无</span>}
        </Field>
        <Field label="二级审批">{status.needs_second ? '需要（受保护目录）' : '不需要'}</Field>
        {status.queued_patches.length > 0 && (
          <Field label="排队">
            <span className="font-mono text-xs">
              {status.queued_patches.map((p) => p.alert_id).join('、')}
            </span>
          </Field>
        )}
      </div>
      <div className="mt-4">
        <JsonBlock data={status} />
      </div>
    </Panel>
  )
}

/** 审批记录（运行中取 status、终态取 result；含证据绑定与被拒命令）。 */
function ApprovalsTab({
  result,
  status,
}: {
  result: ResultPayload | null
  status: StatusPayload | null
}) {
  const records = result?.approvals ?? status?.approvals ?? []
  const rejections = result?.approval_rejections ?? status?.approval_rejections ?? []
  const error = result?.approval_error ?? status?.approval_error ?? null
  const binding = result?.evidence_binding ?? status?.evidence_binding ?? null

  if (!records.length && !rejections.length) {
    return result ? (
      <EmptyState title="无审批记录" hint="该流程未经过审批环节" />
    ) : (
      <EmptyState title="运行中暂无审批记录" hint="审批动作完成后在这里沉淀" />
    )
  }

  const rejected = rejections.length ? rejections : error ? [error] : []

  return (
    <div className="space-y-4">
      <Panel title={`审批记录（${records.length} 条）`}>
        <div className="space-y-2">
          {records.map((record, index) => (
            <div
              key={record.command_id || index}
              className="rounded-lg border border-line bg-canvas px-3 py-2 text-sm"
            >
              <div className="flex flex-wrap items-center gap-3">
                <span className="w-32 shrink-0 font-mono text-xs text-muted">{record.gate}</span>
                <span
                  className={cn(
                    'rounded px-1.5 py-0.5 text-xs',
                    record.decision === 'approve' ? 'bg-ok/10 text-ok' : 'bg-danger/10 text-danger',
                  )}
                >
                  {record.decision}
                </span>
                {record.actor && <span className="text-xs text-muted">审批人 {record.actor}</span>}
                {record.channel && <span className="text-xs text-muted">通道 {record.channel}</span>}
                {record.received_at && (
                  <span className="text-xs text-muted">记录于 {fmtDateTime(record.received_at)}</span>
                )}
              </div>
              {record.comment && <div className="mt-1 text-xs text-ink">意见：{record.comment}</div>}
              {record.command_id && (
                <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 font-mono text-[10px] text-muted">
                  <span>command {record.command_id}</span>
                  {record.diff_hash && <span>diff {shortHash(record.diff_hash)}</span>}
                  {record.run_id && <span>run {record.run_id}</span>}
                </div>
              )}
            </div>
          ))}
        </div>
        {binding && (
          <div className="mt-3 border-t border-line pt-3">
            <div className="text-xs text-muted">证据绑定（approval-evidence-v1）</div>
            <div className="mt-1.5 grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-[11px] text-muted">
              <span>案件 {binding.case_id}</span>
              <span>补丁 {binding.patch_id}</span>
              <span>diff {shortHash(binding.diff_hash)}</span>
              <span>测试 {shortHash(binding.test_report_hash)}</span>
              <span>策略 {shortHash(binding.policy_sha256)}</span>
              <span>run {binding.run_id}</span>
            </div>
          </div>
        )}
      </Panel>

      {rejected.length > 0 && (
        <Panel title={`被拒绝的审批命令（${rejected.length}）`}>
          <div className="space-y-2">
            {rejected.map((entry, index) => (
              <div
                key={entry.command_id || index}
                className="rounded-lg border border-danger/30 bg-danger/5 px-3 py-2 text-xs"
              >
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-mono">{entry.gate}</span>
                  <span className="rounded bg-danger/10 px-1.5 py-0.5 text-danger">{entry.code}</span>
                  {entry.at && <span className="text-muted">{fmtDateTime(entry.at)}</span>}
                </div>
                <div className="mt-1 text-danger">{entry.reason}</div>
                {entry.command_id && (
                  <div className="mt-1 font-mono text-[10px] text-muted">command {entry.command_id}</div>
                )}
              </div>
            ))}
          </div>
        </Panel>
      )}
    </div>
  )
}

/** 发布管道（交互式 SVG）：审批 → 公告 → 金丝雀 → 全量 → 观察；含回滚分支。 */
function ReleasePanel({ result, stage }: { result: ResultPayload; stage: string }) {
  return (
    <Panel title="发布管道">
      <ReleasePipeline
        stage={stage}
        rolledBack={result.deploy_result?.rolled_back}
        version={result.deploy_result?.version}
      />
    </Panel>
  )
}

/** 终态审计记录（root_cause / patch / test_report / deploy_result）。 */
function ResultSummary({ result }: { result: ResultPayload }) {
  return (
    <div className="space-y-4">
      {result.root_cause && (
        <Panel title="根因分析">
          <div className="space-y-2">
            <Field label="错误类型">
              <span className="rounded border border-danger/40 px-1.5 py-0.5 font-mono text-xs text-danger">
                {result.root_cause.error_type}
              </span>
            </Field>
            <Field label="置信度">
              <span className="font-mono">{result.root_cause.confidence.toFixed(2)}</span>
            </Field>
            <Field label="疑似文件">
              <span className="font-mono text-xs">{result.root_cause.suspect_files.join('、') || '—'}</span>
            </Field>
            <Field label="结论">{result.root_cause.summary}</Field>
          </div>
        </Panel>
      )}
      {result.patch && (
        <Panel title="补丁">
          <div className="space-y-2">
            <Field label="补丁 ID">
              <CopyableId value={result.patch.patch_id} />
            </Field>
            <Field label="文件">
              <span className="font-mono text-xs">{result.patch.files.join('、')}</span>
            </Field>
            <Field label="风险">
              <span
                className={cn(
                  'rounded px-1.5 py-0.5 text-xs',
                  result.patch.risk === 'high'
                    ? 'bg-danger/10 text-danger'
                    : result.patch.risk === 'medium'
                      ? 'bg-warn/10 text-warn'
                      : 'bg-ok/10 text-ok',
                )}
              >
                {result.patch.risk}
              </span>
            </Field>
            <Field label="模型">{result.patch.model_version}</Field>
            <Field label="说明">{result.patch.description}</Field>
          </div>
          {result.patch.diff && (
            <div className="mt-4">
              <div className="mb-2 text-xs text-muted">变更内容（unified diff）</div>
              <DiffViewer diff={result.patch.diff} />
            </div>
          )}
        </Panel>
      )}
      {result.test_report && (
        <Panel title="测试报告">
          <div className="space-y-2">
            <Field label="结论">
              <span className={result.test_report.passed ? 'text-ok' : 'text-danger'}>
                {result.test_report.passed ? '通过' : '未通过'}
              </span>
            </Field>
            <Field label="单元测试">
              <span className="font-mono text-xs">{result.test_report.unit_tests}</span>
            </Field>
            <Field label="回归测试">
              <span className="font-mono text-xs">{result.test_report.regression_tests}</span>
            </Field>
            <Field label="SAST">
              <span className="font-mono text-xs">{result.test_report.sast}</span>
            </Field>
            <Field label="明细">{result.test_report.details}</Field>
          </div>
        </Panel>
      )}
      {result.deploy_result && (
        <>
          <ReleasePanel result={result} stage={result.stage} />
          <Panel title="发布结果">
            <div className="space-y-2">
              <Field label="版本">
                <span className="font-mono text-accent">{result.deploy_result.version}</span>
              </Field>
              <Field label="回滚">
                <span className={result.deploy_result.rolled_back ? 'text-danger' : 'text-ok'}>
                  {result.deploy_result.rolled_back ? '已回滚' : '未回滚'}
                </span>
              </Field>
              <Field label="原因">{result.deploy_result.reason || '—'}</Field>
            </div>
          </Panel>
        </>
      )}
      {!result.root_cause && !result.patch && !result.test_report && !result.deploy_result && (
        <EmptyState title="审计记录为空" hint="该流程在生成补丁前结束（转人工 / 取消）" />
      )}
    </div>
  )
}

const SOURCE_LABEL: Record<string, string> = {
  history: 'Temporal',
  status: '状态快照',
  result: '结果',
  audit: '操作审计',
  command: '命令投递',
  notification: '通知',
  escalation: '人工待办',
  release: '发布',
}

const SOURCE_CLS: Record<string, string> = {
  history: 'bg-info/10 text-info',
  command: 'bg-warn/10 text-warn',
  notification: 'bg-accent/10 text-accent',
  escalation: 'bg-danger/10 text-danger',
  audit: 'bg-elevated text-muted',
  status: 'bg-ok/10 text-ok',
}

/** 统一时间线单节点：时间 + 来源徽标 + 标题/摘要（估算时间用 ~ 标记）。 */
function TimelineRow({ item }: { item: TimelineItem }) {
  return (
    <li className="flex gap-3 rounded-lg border border-line bg-canvas px-3 py-2">
      <span
        className="w-32 shrink-0 font-mono text-[11px] text-muted"
        title={item.time_estimated ? '无真实事件时间：由状态快照推导' : undefined}
      >
        {fmtDateTime(item.time)}
        {item.time_estimated ? ' ~' : ''}
      </span>
      <span
        className={cn(
          'h-fit shrink-0 rounded px-1.5 py-0.5 text-[10px]',
          SOURCE_CLS[item.source] ?? 'bg-elevated text-muted',
        )}
      >
        {SOURCE_LABEL[item.source] ?? item.source}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-sm">{item.title}</div>
        {item.summary && (
          <div className="mt-0.5 break-words font-mono text-[11px] text-muted">{item.summary}</div>
        )}
      </div>
    </li>
  )
}

/**
 * 统一时间线（M4）：Temporal History + 闸门/审批 + 操作审计 + 命令投递 + 通知 + 人工待办。
 *
 * 运行中每 5s 刷新（History 投影有周期，status 快照直接补充最新阶段）；
 * 接口失败时回退到终态 gate_events，避免整页不可用。
 */
function TimelineTab({ wfId, result }: { wfId: string; result: ResultPayload | null }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['flow-timeline', wfId],
    queryFn: () => api.flowTimeline(wfId),
    refetchInterval: (query) => (query.state.data?.exec_status === 'RUNNING' ? 5000 : false),
  })

  if (isLoading) return <div className="text-sm text-muted">加载统一时间线…</div>
  if (isError || !data) {
    return result ? (
      <Panel title={`事件流（${result.gate_events.length} 条，History 不可用时回退）`}>
        <EventTimeline events={result.gate_events} />
      </Panel>
    ) : (
      <EmptyState title="时间线不可用" hint={describeError(error)} />
    )
  }

  const history = data.history ?? {}
  return (
    <div className="space-y-4">
      <Panel title={`统一时间线（${data.items.length} 条）`}>
        <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
          <span>
            History 已同步 <span className="font-mono text-ink">{history.event_count ?? 0}</span> 条
          </span>
          <span>
            完整性：
            <span className={history.complete ? 'text-ok' : 'text-warn'}>
              {history.complete ? '已完整' : history.gap ? '断档待对账' : '投影中'}
            </span>
          </span>
          {history.lag_seconds != null && <span>延迟 {history.lag_seconds}s</span>}
          {data.has_more && <span className="text-warn">仅展示最近事件（可在 API 拉取全量）</span>}
        </div>
        {data.items.length ? (
          <ol className="space-y-2">
            {data.items.map((item) => (
              <TimelineRow key={item.id} item={item} />
            ))}
          </ol>
        ) : (
          <EmptyState title="暂无事件" hint="流程刚启动，History 投影可能尚未完成" />
        )}
      </Panel>
      <p className="text-[11px] text-idle">
        时间线按事件时间升序；标记 ~ 的节点无真实时间，由状态快照推导（不伪造时序）。
      </p>
    </div>
  )
}

export function FlowDetail() {
  const { wfId = '' } = useParams()
  const [tab, setTab] = useState<TabKey>('overview')
  const [queueOpen, setQueueOpen] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [exportToast, setExportToast] = useState<string | null>(null)
  const write = useWriteAction()
  const approval = useApprovalDialog()

  const { data, isError, error } = useQuery({
    queryKey: ['flow', wfId],
    queryFn: () => api.flowDetail(wfId),
    refetchInterval: (query) => (query.state.data?.exec_status === 'RUNNING' ? 3000 : false),
  })

  if (isError) {
    return <EmptyState title="无法加载流程详情" hint={describeError(error)} />
  }
  if (!data) return <div className="text-sm text-muted">加载中…</div>

  const { status, result } = data
  const stage = status?.stage ?? result?.stage ?? null
  const service = status?.alert?.service ?? result?.service ?? '未知服务'
  const alertId = status?.alert?.alert_id ?? result?.alert_id ?? null

  const exportPackage = async () => {
    setExporting(true)
    try {
      const file = await api.evidencePackage(wfId, data.run_id)
      saveBlob(file.blob, file.filename)
      setExportToast(
        `证据包已导出（审计 ${file.auditEventId || '—'}，root ${shortHash(file.rootSha256, 16)}）` +
          (file.historyComplete ? '' : '；History 未完整，不得视为最终历史'),
      )
    } catch (err) {
      setExportToast(`证据包导出失败：${describeError(err)}`)
    } finally {
      setExporting(false)
      window.setTimeout(() => setExportToast(null), 8000)
    }
  }

  return (
    <div>
      <Link to="/flows" className="text-xs text-muted transition-colors hover:text-ink">
        ← 返回流程列表
      </Link>

      <header className="mt-3 rounded-xl border border-line bg-panel p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-lg font-medium">{service}</h1>
              <StageBadge stage={stage} />
              {data.exec_status !== 'RUNNING' && (
                <span className="text-xs text-muted">{data.exec_status}</span>
              )}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted">
              <span>
                工作流 <CopyableId value={wfId} />
              </span>
              {alertId && (
                <span>
                  告警 <CopyableId value={alertId} />
                </span>
              )}
              <span>开始 {fmtDateTime(data.start_time)}</span>
              {data.close_time && <span>结束 {fmtDateTime(data.close_time)}</span>}
              {result && <span>耗时 {fmtDuration(result.duration_seconds)}</span>}
              {result?.confidence != null && <span>置信度 {result.confidence.toFixed(2)}</span>}
              {data.evidence && (
                <span>
                  日志证据 <span className="text-ink">{data.evidence.stored_lines}</span> 行
                  {data.evidence.loki_available ? '' : '（Loki 不可用）'}
                </span>
              )}
              {data.history && !data.history.error && (
                <span title="Temporal History 投影（完整事件流，不受 UI 200 条上限约束）">
                  History <span className="text-ink">{data.history.event_count ?? 0}</span> 条
                  <span className={data.history.complete ? 'text-ok' : 'text-warn'}>
                    {data.history.complete ? ' · 已完整' : data.history.gap ? ' · 断档待对账' : ' · 投影中'}
                  </span>
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <PermissionGate require="operator">
              <button
                type="button"
                disabled={exporting}
                onClick={exportPackage}
                className="rounded-lg border border-accent/40 px-3 py-1.5 text-xs text-accent transition-colors hover:bg-accent/10 disabled:opacity-50"
                title="自包含证据包（含 History 全量、审计、命令、通知与 manifest/SHA-256）"
              >
                {exporting ? '导出中…' : '证据包'}
              </button>
            </PermissionGate>
            <button
              type="button"
              onClick={() => downloadJson(`${wfId}.json`, data)}
              className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted transition-colors hover:bg-elevated hover:text-ink"
            >
              导出 JSON
            </button>
          </div>
        </div>
        {data.errors.length > 0 && (
          <div className="mt-3 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
            {data.errors.join('；')}
          </div>
        )}
      </header>

      {stage === 'WAIT_APPROVAL' && status && (
        <ApprovalPanel wfId={wfId} status={status} approval={approval} />
      )}
      {stage === 'NOTIFYING' && status && (
        <WindowPanel wfId={wfId} status={status} write={write} onQueue={() => setQueueOpen(true)} />
      )}

      <nav className="mt-6 flex gap-1 border-b border-line">
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setTab(item.key)}
            className={cn(
              '-mb-px border-b-2 px-4 py-2 text-sm transition-colors',
              tab === item.key
                ? 'border-accent text-accent'
                : 'border-transparent text-muted hover:text-ink',
            )}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <div className="mt-4">
        {tab === 'overview' && (
          <div className="space-y-4">
            {result ? (
              <>
                {/* 终态流程：状态机仍展示（可直观看到走到哪一步、在哪中断） */}
                <Panel title="流程状态机">
                  <FlowStateMachine stage={stage} />
                </Panel>
                <ResultSummary result={result} />
              </>
            ) : status ? (
              /* 运行中流程：状态机在「运行状态」面板内展示 */
              <StatusSummary status={status} />
            ) : (
              <EmptyState title="暂无状态数据" hint="工作流状态读取失败" />
            )}
          </div>
        )}

        {tab === 'evidence' && (
          <Panel title="日志证据（转人工核对）">
            <LogEvidence wfId={wfId} />
          </Panel>
        )}

        {tab === 'timeline' && <TimelineTab wfId={wfId} result={result} />}

        {tab === 'approvals' && <ApprovalsTab result={result} status={status} />}

        {tab === 'artifacts' &&
          (data.artifacts &&
          (data.artifacts.notify.length || data.artifacts.argocd.length || data.artifacts.sandbox_dir) ? (
            <Panel title="留痕文件">
              <div className="space-y-3 text-sm">
                {data.artifacts.sandbox_dir && (
                  <Field label="沙箱目录">
                    <span className="font-mono text-xs">{data.artifacts.sandbox_dir}</span>
                  </Field>
                )}
                <Field label="公告留痕">
                  {data.artifacts.notify.length ? (
                    <ul className="space-y-1">
                      {data.artifacts.notify.map((file) => (
                        <li key={file} className="font-mono text-xs">
                          {file}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </Field>
                <Field label="ArgoCD 留痕">
                  {data.artifacts.argocd.length ? (
                    <ul className="space-y-1">
                      {data.artifacts.argocd.map((file) => (
                        <li key={file} className="font-mono text-xs">
                          {file}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </Field>
              </div>
            </Panel>
          ) : (
            <EmptyState title="暂无留痕文件" hint="补丁生成后，公告 / ArgoCD / 沙箱留痕会在这里汇总" />
          ))}
      </div>

      <QueuePatchDialog open={queueOpen} wfId={wfId} onClose={() => setQueueOpen(false)} />
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
      <ApprovalDialog
        request={approval.request}
        onClose={approval.close}
        onDone={approval.done}
      />
      <Toast message={write.toast ?? approval.toast ?? exportToast} />
    </div>
  )
}
