/** 排队补丁表单（窗口内 FIFO 排队；new_wf_id 按 aiops-fix-{service}-{alert_id} 自动生成）。 */

import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { cn } from '../lib/format'
import { ConfirmDialog } from './ConfirmDialog'

const INPUT_CLS =
  'w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink placeholder:text-idle focus:border-accent/50 focus:outline-none'

export function QueuePatchDialog({
  open,
  wfId,
  onClose,
}: {
  open: boolean
  wfId: string
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [service, setService] = useState('')
  const [alertId, setAlertId] = useState('')
  const [description, setDescription] = useState('')
  const [customId, setCustomId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const autoId = service && alertId ? `aiops-fix-${service}-${alertId}` : ''
  const newWfId = customId ?? autoId

  const reset = () => {
    setService('')
    setAlertId('')
    setDescription('')
    setCustomId(null)
    setError(null)
  }

  const close = () => {
    if (busy) return
    reset()
    onClose()
  }

  const submit = async () => {
    if (!service || !alertId || !newWfId) {
      setError('服务名与告警 ID 为必填项')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.queuePatch(wfId, {
        new_wf_id: newWfId,
        service,
        alert_id: alertId,
        description,
      })
      await queryClient.invalidateQueries()
      reset()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <ConfirmDialog
      open={open}
      title="排队新补丁（窗口内 FIFO）"
      confirmLabel="加入队列"
      busy={busy}
      error={error}
      onConfirm={submit}
      onClose={close}
    >
      <div className="space-y-3">
        <div>
          <label className="mb-1 block text-xs text-muted">服务名 *</label>
          <input
            value={service}
            onChange={(event) => setService(event.target.value)}
            placeholder="例如 checkout-api"
            className={INPUT_CLS}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">告警 ID *</label>
          <input
            value={alertId}
            onChange={(event) => setAlertId(event.target.value)}
            placeholder="例如 alert-2026-1001"
            className={INPUT_CLS}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">工作流 ID（自动生成，可修改）</label>
          <input
            value={newWfId}
            onChange={(event) => setCustomId(event.target.value)}
            placeholder="aiops-fix-{service}-{alert_id}"
            className={cn(INPUT_CLS, 'font-mono text-xs')}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">描述</label>
          <input
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="可选"
            className={INPUT_CLS}
          />
        </div>
        <p className="text-xs text-idle">
          窗口内新补丁仅 FIFO 排队（不重置倒计时、不合并，锁定策略），随当前流程结束后处理。
        </p>
      </div>
    </ConfirmDialog>
  )
}
