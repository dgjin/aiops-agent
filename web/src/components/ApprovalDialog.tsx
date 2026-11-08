/** 审批确认层（approval-evidence-v1）：意见输入 + 当前证据绑定 + 送达语义回执。 */

import type { ReactNode } from 'react'
import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { shortHash } from '../lib/format'
import { ConfirmDialog } from './ConfirmDialog'
import type { EvidenceBinding, WriteResp } from '../lib/types'

export interface ApprovalRequest {
  wfId: string
  gate: 'approval' | 'second_approval'
  decision: 'approve' | 'reject'
  /** 业务摘要（服务 / 补丁 / 测试结论等，由调用方拼装） */
  summary: ReactNode
  /** 当前页面展示的证据绑定：提交时回传用于拒绝过期页面（旧流程为空） */
  binding?: EvidenceBinding | null
  /** 案件 ID（旧流程无绑定时以告警 / 需求 ID 兜底） */
  caseId?: string
}

const GATE_LABELS: Record<ApprovalRequest['gate'], string> = {
  approval: '一级审批',
  second_approval: '二级审批',
}

const DECISION_LABELS: Record<ApprovalRequest['decision'], string> = {
  approve: '批准',
  reject: '驳回',
}

/** 送达语义 → 操作提示：只有 accepted 表示工作流已受理并记录。 */
function deliveryMessage(
  response: WriteResp,
  gate: ApprovalRequest['gate'],
  decision: ApprovalRequest['decision'],
): string {
  const label = `${GATE_LABELS[gate]}${DECISION_LABELS[decision]}`
  if (response.delivery === 'accepted') return `${label}已受理并记录`
  if (response.command_id) return `${label}命令已提交；业务结果以流程状态为准`
  return `${label}命令已提交`
}

/** 审批对话框状态（由页面持有，按钮负责 open，对话框负责提交与回执）。 */
export function useApprovalDialog() {
  const [request, setRequest] = useState<ApprovalRequest | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  const open = (next: ApprovalRequest) => setRequest(next)
  const close = () => setRequest(null)
  const done = (message: string) => {
    setToast(message)
    setTimeout(() => setToast(null), 5000)
  }
  return { request, open, close, done, toast }
}

export type ApprovalHandle = ReturnType<typeof useApprovalDialog>

export function ApprovalDialog({
  request,
  onClose,
  onDone,
}: {
  request: ApprovalRequest | null
  onClose: () => void
  onDone: (message: string) => void
}) {
  const queryClient = useQueryClient()
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 每次打开新审批时重置意见与错误（同一弹层复用）。
  useEffect(() => {
    setComment('')
    setError(null)
  }, [request])

  if (!request) return null

  const current = request
  const reject = current.decision === 'reject'
  const binding = current.binding ?? null
  const gateLabel = GATE_LABELS[current.gate]
  const decisionLabel = DECISION_LABELS[current.decision]

  const close = () => {
    if (!busy) onClose()
  }

  const submit = async () => {
    if (reject && !comment.trim()) {
      setError('驳回必须填写审批意见')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const send = current.gate === 'second_approval' ? api.secondApproval : api.approval
      const response = await send(current.wfId, {
        decision: current.decision,
        comment: comment.trim(),
        expected_case_id: current.caseId || binding?.case_id || '',
        expected_diff_hash: binding?.diff_hash || '',
        expected_run_id: binding?.run_id || '',
      })
      onClose()
      onDone(deliveryMessage(response, current.gate, current.decision))
      await queryClient.invalidateQueries()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <ConfirmDialog
      open
      title={`${gateLabel}：确认${decisionLabel}？`}
      tone={reject ? 'danger' : 'default'}
      confirmLabel={`${decisionLabel}并记录`}
      busy={busy}
      error={error}
      confirmDisabled={reject && !comment.trim()}
      onConfirm={submit}
      onClose={close}
    >
      <div className="space-y-3">
        {current.summary}

        <div className="rounded-lg border border-line bg-canvas p-2.5">
          <div className="text-[11px] text-ink">审批证据绑定（approval-evidence-v1）</div>
          {binding ? (
            <div className="mt-1.5 grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-[11px] text-muted">
              <span>案件 {shortHash(binding.case_id, 18)}</span>
              <span>补丁 {shortHash(binding.patch_id, 18)}</span>
              <span>diff {shortHash(binding.diff_hash)}</span>
              <span>测试 {shortHash(binding.test_report_hash)}</span>
              <span>策略 {shortHash(binding.policy_sha256)}</span>
              <span>run {shortHash(binding.run_id, 18)}</span>
            </div>
          ) : (
            <div className="mt-1.5 text-[11px] text-idle">
              旧协议流程未提供证据绑定：本次审批只按流程 ID 与当前阶段校验。
            </div>
          )}
        </div>

        <label className="block">
          <span className="text-xs text-muted">
            审批意见{reject ? '（必填，将写入审计记录）' : '（选填）'}
          </span>
          <textarea
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            rows={3}
            maxLength={2000}
            placeholder={reject ? '请说明驳回原因，例如：缺少回滚验证 / 影响面过大' : '可补充审批依据'}
            className="mt-1 w-full resize-y rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink outline-none placeholder:text-muted focus:border-accent"
          />
          <span className="mt-1 block text-right text-[10px] text-muted">{comment.length}/2000</span>
        </label>

        <div className="text-ink">
          本次决策：{decisionLabel}（{gateLabel}）
        </div>
      </div>
    </ConfirmDialog>
  )
}
