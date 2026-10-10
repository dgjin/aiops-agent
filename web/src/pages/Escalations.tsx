/** 转人工待办（P1-3）：ESCALATED 处置闭环——指派 / 处置关闭 / 重试修复。
 *
 * - 列表 3 秒轮询；读侧自动幂等登记（ESCALATED 流程 → open 待办），已关闭不重开；
 * - 超 SLA（默认 30 分钟）未处置的条目由 BFF 后台巡检经通知渠道再升级一次，
 *   前端以「已超时未处置 / 已再升级」标记提示值班同学；
 * - 处置路径：指派责任人 → 手动关闭（备注留档）或「重试修复」（新幂等键重启完整修复链路）；
 *   需求来源的待办（entry.requirement）重试时重启需求修复流（批准版方案 → 沙箱 → 审批 → 发布）。
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, describeError } from '../lib/api'
import { cn, fmtDateTime } from '../lib/format'
import { useWriteAction } from '../lib/actions'
import { ActionButton } from '../components/ActionButton'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { CopyableId } from '../components/CopyableId'
import { EmptyState } from '../components/EmptyState'
import {
  Modal,
  fieldClass,
  primaryButtonClass,
  secondaryButtonClass,
} from '../components/Modal'
import { PermissionGate } from '../components/PermissionGate'
import { Toast } from '../components/Toast'
import type { EscalationEntry } from '../lib/types'

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

/** 处置关闭：备注写入处置轨迹（history）与操作审计。 */
function CloseDialog({ entry, onClose }: { entry: EscalationEntry; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const close = () => {
    if (busy) return
    onClose()
  }

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.closeEscalation(entry.id, note.trim())
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
      title="处置关闭"
      onClose={close}
      footer={
        <>
          <button type="button" className={secondaryButtonClass} onClick={close}>
            取消
          </button>
          <button type="button" className={primaryButtonClass} disabled={busy} onClick={submit}>
            {busy ? '提交中…' : '确认关闭'}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted">
          <div>服务：{entry.service || '—'}</div>
          <div className="truncate" title={entry.auto_reason}>
            原因：{entry.auto_reason || '—'}
          </div>
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">
            处置备注（写入处置轨迹与操作审计）
          </label>
          <textarea
            rows={3}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="例如：已人工回滚发布，错误率恢复；或已知问题，无需自动修复"
            className={fieldClass}
          />
        </div>
        {error && <p className="text-xs text-danger">{error}</p>}
      </div>
    </Modal>
  )
}

export function Escalations() {
  const write = useWriteAction()
  const [status, setStatus] = useState<StatusFilter>('all')
  const [assignTarget, setAssignTarget] = useState<EscalationEntry | null>(null)
  const [closeTarget, setCloseTarget] = useState<EscalationEntry | null>(null)

  const { data, isError, error } = useQuery({
    queryKey: ['escalations', status],
    queryFn: () => api.escalations(status),
    refetchInterval: 3000,
  })

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

      {data && (
        <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
          <span>共 {data.stats.total} 条</span>
          <span className="text-danger">待指派 {data.stats.open}</span>
          <span className="text-warn">已指派 {data.stats.assigned}</span>
          <span className="text-ok">已关闭 {data.stats.closed}</span>
          <span className="text-idle">
            SLA {data.stats.sla_minutes} 分钟：超时未处置自动再升级一次（每单一次，防轰炸）
          </span>
        </div>
      )}

      <div className="mt-4 flex gap-1.5">
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
          </button>
        ))}
      </div>

      {isError && (
        <div className="mt-5">
          <EmptyState title="无法加载待办列表" hint={describeError(error)} />
        </div>
      )}

      {data &&
        (data.escalations.length ? (
          <div className="mt-5 space-y-3">
            {data.escalations.map((entry) => {
              const meta = STATUS_META[entry.status]
              const open = entry.status !== 'closed'
              return (
                <div key={entry.id} className="rounded-xl border border-line bg-panel p-4">
                  <div className="flex flex-wrap items-start justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-3">
                        <span className="text-sm font-medium">{entry.service || '未知服务'}</span>
                        <span className={cn('rounded border px-1.5 py-0.5 text-[10px]', meta.cls)}>
                          {meta.label}
                        </span>
                        {entry.requirement && (
                          <span className="rounded border border-accent/40 px-1.5 py-0.5 text-[10px] text-accent">
                            需求
                          </span>
                        )}
                        {open && entry.overdue && (
                          <span className="rounded border border-danger/40 bg-danger/10 px-1.5 py-0.5 text-[10px] text-danger">
                            已超时未处置
                          </span>
                        )}
                        {entry.re_escalated && (
                          <span className="rounded border border-line px-1.5 py-0.5 text-[10px] text-idle">
                            已再升级
                          </span>
                        )}
                      </div>
                      <div className="mt-2 text-xs text-muted">
                        升级原因：<span className="text-ink">{entry.auto_reason}</span>
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted">
                        <span>
                          告警 <CopyableId value={entry.alert_id || '—'} />
                        </span>
                        <span>
                          工作流 <CopyableId value={entry.wf_id} />
                        </span>
                        <span>登记 {fmtDateTime(entry.escalated_at)}</span>
                        {entry.assignee && (
                          <span>
                            责任人 <span className="text-ink">{entry.assignee}</span>
                          </span>
                        )}
                        {entry.status === 'closed' && entry.note && (
                          <span>备注：{entry.note}</span>
                        )}
                      </div>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Link
                        to={`/flows/${encodeURIComponent(entry.wf_id)}`}
                        className="text-xs text-accent hover:underline"
                      >
                        流程详情 →
                      </Link>
                      {open && (
                        <PermissionGate require="operator">
                          <ActionButton onClick={() => setAssignTarget(entry)}>
                            {entry.assignee ? '改派' : '指派'}
                          </ActionButton>
                          <ActionButton onClick={() => setCloseTarget(entry)}>处置关闭</ActionButton>
                          <ActionButton tone="accent" onClick={() => openRetry(entry)}>
                            重试修复
                          </ActionButton>
                        </PermissionGate>
                      )}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
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
