import { cn } from '../lib/format'

/** 事件 → 颜色（先进先出匹配：失败类优先）。 */
function eventTone(event: string): { text: string; dot: string } {
  const danger = ['blocked', 'failed', 'reject', 'degraded', 'timeout']
  const idle = ['cancelled']
  const ok = ['approved', 'passed', 'healthy']
  const warn = ['gate3:', 'gate2:']
  const info = ['canary:', 'mr:', 'queue:']
  if (danger.some((key) => event.includes(key))) return { text: 'text-danger', dot: 'bg-danger' }
  if (idle.some((key) => event.includes(key))) return { text: 'text-idle', dot: 'bg-idle' }
  if (ok.some((key) => event.includes(key))) return { text: 'text-ok', dot: 'bg-ok' }
  if (warn.some((key) => event.startsWith(key))) return { text: 'text-warn', dot: 'bg-warn' }
  if (info.some((key) => event.startsWith(key))) return { text: 'text-info', dot: 'bg-info' }
  return { text: 'text-muted', dot: 'bg-idle' }
}

/** gate_events 时间轴（终态完整事件流；运行中仅提示）。 */
export function EventTimeline({ events }: { events: string[] }) {
  if (!events.length) {
    return <div className="text-sm text-muted">暂无事件记录</div>
  }
  return (
    <ol className="relative ml-1 border-l border-line">
      {events.map((event, index) => {
        const tone = eventTone(event)
        return (
          <li key={`${index}-${event}`} className="relative flex items-center gap-3 py-1.5 pl-5">
            <span
              className={cn(
                'absolute -left-[5px] h-[9px] w-[9px] rounded-full border-2 border-panel',
                tone.dot,
              )}
            />
            <span className={cn('font-mono text-xs leading-6', tone.text)}>{event}</span>
          </li>
        )
      })}
    </ol>
  )
}
