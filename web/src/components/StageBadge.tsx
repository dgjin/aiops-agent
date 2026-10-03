import { cn } from '../lib/format'

interface StageMeta {
  label: string
  tone: 'info' | 'warn' | 'ok' | 'danger' | 'idle'
  pulse?: boolean
}

/** stage → 视觉映射（设计方案 7.2）。 */
const STAGE_META: Record<string, StageMeta> = {
  DETECTED: { label: '已接入', tone: 'info' },
  TRIAGING: { label: '分析中', tone: 'info', pulse: true },
  FIXING: { label: '修复中', tone: 'info', pulse: true },
  TESTING: { label: '测试中', tone: 'info', pulse: true },
  WAIT_APPROVAL: { label: '待审批', tone: 'warn', pulse: true },
  NOTIFYING: { label: '公告倒计时', tone: 'warn', pulse: true },
  CANARY: { label: '金丝雀观测', tone: 'info', pulse: true },
  ROLLING_OUT: { label: '发布中', tone: 'info', pulse: true },
  DONE: { label: '已完成', tone: 'ok' },
  ESCALATED: { label: '已转人工', tone: 'danger' },
  CANCELLED: { label: '已取消', tone: 'idle' },
}

const TONE_CLS: Record<StageMeta['tone'], string> = {
  info: 'border-info/30 bg-info/10 text-info',
  warn: 'border-warn/30 bg-warn/10 text-warn',
  ok: 'border-ok/30 bg-ok/10 text-ok',
  danger: 'border-danger/30 bg-danger/10 text-danger',
  idle: 'border-idle/30 bg-idle/10 text-idle',
}

const DOT_CLS: Record<StageMeta['tone'], string> = {
  info: 'bg-info',
  warn: 'bg-warn',
  ok: 'bg-ok',
  danger: 'bg-danger',
  idle: 'bg-idle',
}

export function StageBadge({ stage, className }: { stage: string | null; className?: string }) {
  const meta = stage ? STAGE_META[stage] : undefined
  if (!meta) {
    return (
      <span
        className={cn(
          'inline-flex items-center gap-1.5 rounded-full border border-line bg-elevated px-2.5 py-0.5 text-xs text-muted',
          className,
        )}
      >
        {stage ?? '未知'}
      </span>
    )
  }
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium',
        TONE_CLS[meta.tone],
        className,
      )}
    >
      <span
        className={cn('h-1.5 w-1.5 rounded-full', DOT_CLS[meta.tone], meta.pulse && 'animate-pulse')}
      />
      {meta.label}
      <span className="font-mono text-[10px] opacity-60">{stage}</span>
    </span>
  )
}
