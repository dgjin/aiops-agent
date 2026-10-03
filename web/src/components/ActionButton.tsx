import type { ReactNode } from 'react'
import { cn } from '../lib/format'

/** 操作按钮：accent 主操作 / danger 危险操作 / default 次操作。 */
export function ActionButton({
  tone = 'default',
  onClick,
  children,
}: {
  tone?: 'accent' | 'danger' | 'default'
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'rounded-lg px-3.5 py-1.5 text-sm font-medium transition-colors',
        tone === 'accent' && 'bg-accent/90 text-canvas hover:bg-accent',
        tone === 'danger' && 'bg-danger/90 text-canvas hover:bg-danger',
        tone === 'default' && 'border border-line text-muted hover:bg-elevated hover:text-ink',
      )}
    >
      {children}
    </button>
  )
}
