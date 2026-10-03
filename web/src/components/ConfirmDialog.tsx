import type { ReactNode } from 'react'
import { cn } from '../lib/format'

interface ConfirmDialogProps {
  open: boolean
  title: string
  tone?: 'default' | 'danger'
  confirmLabel?: string
  busy?: boolean
  error?: string | null
  onConfirm: () => void
  onClose: () => void
  children?: ReactNode
}

/** 操作确认层：常规（青色主按钮）/ 危险（红色主按钮）两级样式（设计方案 7.3）。 */
export function ConfirmDialog({
  open,
  title,
  tone = 'default',
  confirmLabel = '确认',
  busy = false,
  error,
  onConfirm,
  onClose,
  children,
}: ConfirmDialogProps) {
  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-xl border border-line bg-panel shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div
          className={cn(
            'border-b border-line px-5 py-3.5 text-sm font-medium',
            tone === 'danger' && 'text-danger',
          )}
        >
          {title}
        </div>
        <div className="max-h-[60vh] overflow-y-auto px-5 py-4 text-sm text-muted">{children}</div>
        {error && (
          <div className="mx-5 mb-3 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
            {error}
          </div>
        )}
        <div className="flex justify-end gap-2 border-t border-line px-5 py-3.5">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-line px-3.5 py-1.5 text-sm text-muted transition-colors hover:bg-elevated hover:text-ink"
          >
            取消
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className={cn(
              'rounded-lg px-3.5 py-1.5 text-sm font-medium text-canvas transition-colors disabled:opacity-50',
              tone === 'danger' ? 'bg-danger/90 hover:bg-danger' : 'bg-accent/90 hover:bg-accent',
            )}
          >
            {busy ? '提交中…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
