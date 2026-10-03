import { serverNow } from '../lib/api'
import { cn, fmtRemain } from '../lib/format'
import { useNowTick } from '../lib/hooks'

/** 倒计时文本：服务端时间校准 + 本地秒级插值（设计方案 5.4）。 */
export function CountdownText({
  at,
  className,
}: {
  at: string | null | undefined
  className?: string
}) {
  useNowTick()
  if (!at) {
    return <span className={cn('font-mono text-muted', className)}>--:--</span>
  }
  const remainMs = Date.parse(at) - serverNow()
  if (Number.isNaN(remainMs)) {
    return <span className={cn('font-mono text-muted', className)}>--:--</span>
  }
  if (remainMs <= 0) {
    return <span className={cn('font-mono text-danger', className)}>已到期</span>
  }
  const urgent = remainMs < 60_000
  const warning = remainMs < 5 * 60_000
  return (
    <span
      className={cn(
        'font-mono tabular-nums',
        urgent ? 'animate-pulse text-danger' : warning ? 'text-warn' : '',
        className,
      )}
    >
      {fmtRemain(remainMs)}
    </span>
  )
}
