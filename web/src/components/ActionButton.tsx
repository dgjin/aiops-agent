import type { ReactNode } from 'react'
import { cn } from '../lib/format'

/** 操作按钮：accent 主操作 / danger 危险操作 / default 次操作；size="sm" 用于表格行内等紧凑场景。 */
export function ActionButton({
  tone = 'default',
  size = 'md',
  onClick,
  children,
}: {
  tone?: 'accent' | 'danger' | 'default'
  size?: 'sm' | 'md'
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'rounded-lg font-medium transition-colors',
        size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3.5 py-1.5 text-sm',
        tone === 'accent' && 'btn-glow bg-accent/90 text-canvas hover:bg-accent',
        tone === 'danger' && 'bg-danger/90 text-canvas hover:bg-danger',
        tone === 'default' && 'border border-line text-muted hover:bg-elevated hover:text-ink',
      )}
    >
      {children}
    </button>
  )
}
