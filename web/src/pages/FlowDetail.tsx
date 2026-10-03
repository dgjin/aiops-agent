/** 流程详情：状态头 + 条件操作区（审批 / 窗口）+ 概览 / 事件 / 审批记录 / 留痕（设计方案 7.3）。 */

import type { ReactNode } from 'react'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { cn, downloadJson, fmtDateTime, fmtDuration } from '../lib/format'
import { useWriteAction } from '../lib/actions'
import { ActionButton } from '../components/ActionButton'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { CopyableId } from '../components/CopyableId'
import { CountdownText } from '../components/CountdownText'
import { DiffViewer } from '../components/DiffViewer'
import { EmptyState } from '../components/EmptyState'
import { EventTimeline } from '../components/EventTimeline'
import { JsonBlock } from '../components/JsonBlock'
import { QueuePatchDialog } from '../components/QueuePatchDialog'
import { StageBadge } from '../components/StageBadge'
import { Toast } from '../components/Toast'
import type { ResultPayload, StatusPayload } from '../lib/types'

const TABS = [
  { key: 'overview', label: '概览' },
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

/** 待审批操作面板（一级 / 二级）。 */
function ApprovalPanel({
  wfId,
  status,
  write,
}: {
  wfId: string
  status: StatusPayload
  write: WriteHandle
}) {
  const service = status.alert?.service
  const alertId = status.alert?.alert_id
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
            <>
              <ActionButton
                tone="accent"
                onClick={() =>
                  write.open({
                    title: '确认批准该补丁？',
                    confirmLabel: '批准',
                    detail: (
                      <ActionSummary wfId={wfId} service={service} alertId={alertId} decision="批准（进入公告窗口）" />
                    ),
                    run: () => api.approval(wfId, 'approve'),
                    success: '一级审批已提交：批准',
                  })
                }
              >
                批准
              </ActionButton>
              <ActionButton
                tone="danger"
                onClick={() =>
                  write.open({
                    title: '确认驳回该补丁？',
                    tone: 'danger',
                    confirmLabel: '驳回',
                    detail: (
                      <ActionSummary wfId={wfId} service={service} alertId={alertId} decision="驳回（转人工处理）" />
                    ),
                    run: () => api.approval(wfId, 'reject'),
                    success: '一级审批已提交：驳回',
                  })
                }
              >
                驳回
              </ActionButton>
            </>
          )}
          {first === 'approve' && status.needs_second && second === null && (
            <>
              <ActionButton
                tone="accent"
                onClick={() =>
                  write.open({
                    title: '确认二级批准？',
                    confirmLabel: '二级批准',
                    detail: (
                      <ActionSummary wfId={wfId} service={service} alertId={alertId} decision="二级批准（受保护目录变更）" />
                    ),
                    run: () => api.secondApproval(wfId, 'approve'),
                    success: '二级审批已提交：批准',
                  })
                }
              >
                二级批准
              </ActionButton>
              <ActionButton
                tone="danger"
                onClick={() =>
                  write.open({
                    title: '确认二级驳回？',
                    tone: 'danger',
                    confirmLabel: '二级驳回',
                    detail: (
                      <ActionSummary wfId={wfId} service={service} alertId={alertId} decision="二级驳回（转人工处理）" />
                    ),
                    run: () => api.secondApproval(wfId, 'reject'),
                    success: '二级审批已提交：驳回',
                  })
                }
              >
                二级驳回
              </ActionButton>
            </>
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
      )}
      {!result.root_cause && !result.patch && !result.test_report && !result.deploy_result && (
        <EmptyState title="审计记录为空" hint="该流程在生成补丁前结束（转人工 / 取消）" />
      )}
    </div>
  )
}

export function FlowDetail() {
  const { wfId = '' } = useParams()
  const [tab, setTab] = useState<TabKey>('overview')
  const [queueOpen, setQueueOpen] = useState(false)
  const write = useWriteAction()

  const { data, isError, error } = useQuery({
    queryKey: ['flow', wfId],
    queryFn: () => api.flowDetail(wfId),
    refetchInterval: (query) => (query.state.data?.exec_status === 'RUNNING' ? 3000 : false),
  })

  if (isError) {
    return <EmptyState title="无法加载流程详情" hint={String(error)} />
  }
  if (!data) return <div className="text-sm text-muted">加载中…</div>

  const { status, result } = data
  const stage = status?.stage ?? result?.stage ?? null
  const service = status?.alert?.service ?? result?.service ?? '未知服务'
  const alertId = status?.alert?.alert_id ?? result?.alert_id ?? null

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
            </div>
          </div>
          <button
            type="button"
            onClick={() => downloadJson(`${wfId}.json`, data)}
            className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted transition-colors hover:bg-elevated hover:text-ink"
          >
            导出 JSON
          </button>
        </div>
        {data.errors.length > 0 && (
          <div className="mt-3 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
            {data.errors.join('；')}
          </div>
        )}
      </header>

      {stage === 'WAIT_APPROVAL' && status && <ApprovalPanel wfId={wfId} status={status} write={write} />}
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
        {tab === 'overview' &&
          (result ? <ResultSummary result={result} /> : status ? <StatusSummary status={status} /> : (
            <EmptyState title="暂无状态数据" hint="工作流状态读取失败" />
          ))}

        {tab === 'timeline' &&
          (result ? (
            <Panel title={`事件流（${result.gate_events.length} 条）`}>
              <EventTimeline events={result.gate_events} />
            </Panel>
          ) : (
            <EmptyState title="运行中暂无完整事件流" hint="流程到达终态后可查看完整时间线" />
          ))}

        {tab === 'approvals' &&
          (result ? (
            result.approvals.length ? (
              <Panel title="审批记录">
                <div className="space-y-2">
                  {result.approvals.map((approval, index) => (
                    <div key={index} className="flex items-center gap-3 text-sm">
                      <span className="w-32 shrink-0 font-mono text-xs text-muted">{approval.gate}</span>
                      <span
                        className={cn(
                          'rounded px-1.5 py-0.5 text-xs',
                          approval.decision === 'approve'
                            ? 'bg-ok/10 text-ok'
                            : 'bg-danger/10 text-danger',
                        )}
                      >
                        {approval.decision}
                      </span>
                    </div>
                  ))}
                </div>
              </Panel>
            ) : (
              <EmptyState title="无审批记录" hint="该流程未经过审批环节" />
            )
          ) : (
            <EmptyState title="运行中暂无审批记录" hint="审批动作完成后在这里沉淀" />
          ))}

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
      <Toast message={write.toast} />
    </div>
  )
}
