import { useState } from 'react'
import { cn } from '../lib/format'

/** 可点击复制的等宽标识（wf-id / alert_id / patch_id）。 */
export function CopyableId({ value, className }: { value: string; className?: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      title="点击复制"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value)
          setCopied(true)
          setTimeout(() => setCopied(false), 1200)
        } catch {
          /* 剪贴板不可用时忽略 */
        }
      }}
      className={cn(
        'inline-flex items-center gap-1 font-mono text-xs text-muted transition-colors hover:text-ink',
        className,
      )}
    >
      {value}
      {copied && <span className="text-accent">已复制</span>}
    </button>
  )
}
