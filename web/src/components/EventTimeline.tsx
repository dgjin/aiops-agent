import { useState } from 'react'
import { EmptyState } from './EmptyState'

/** 事件 → 取色（先进先出匹配：失败类优先）。 */
function eventTone(event: string): { cls: string; label: string } {
  const danger = ['blocked', 'failed', 'reject', 'degraded', 'timeout']
  const idle = ['cancelled']
  const ok = ['approved', 'passed', 'healthy']
  const warn = ['gate3:', 'gate2:']
  const info = ['canary:', 'mr:', 'queue:']
  if (danger.some((key) => event.includes(key))) return { cls: 'text-danger', label: '异常/拦截' }
  if (idle.some((key) => event.includes(key))) return { cls: 'text-idle', label: '取消' }
  if (ok.some((key) => event.includes(key))) return { cls: 'text-ok', label: '通过' }
  if (warn.some((key) => event.startsWith(key))) return { cls: 'text-warn', label: '闸门' }
  if (info.some((key) => event.startsWith(key))) return { cls: 'text-info', label: '发布' }
  return { cls: 'text-muted', label: '其他' }
}

const PITCH = 116
const X0 = 46
const Y = 44

/**
 * 闸门事件时间线（**交互式 SVG**）：终态流程的 gate_events 按发生顺序铺在一条轨道上，
 * 节点按语义着色；悬停 / 点击节点在下方显示完整事件文案（可作用于超长事件串）。
 */
export function EventTimeline({ events }: { events: string[] }) {
  const [active, setActive] = useState<number | null>(null)

  if (!events.length) {
    return (
      <EmptyState
        title="暂无事件"
        hint="流程到达终态后会沉淀完整事件流（测试回炉 / MR / 审批 / 公告 / 金丝雀）"
      />
    )
  }

  const width = X0 * 2 + PITCH * (events.length - 1)
  const current = active ?? events.length - 1

  return (
    <div>
      <div className="overflow-x-auto">
        <svg
          viewBox={`0 0 ${Math.max(width, 360)} 92`}
          className="h-auto w-full min-w-[520px]"
          role="img"
          aria-label="闸门事件时间线"
        >
          {/* 轨道与已完成段 */}
          <line x1={X0} y1={Y} x2={Math.max(X0 + PITCH * (events.length - 1), X0)} y2={Y} strokeWidth={2} className="stroke-line" />
          <line x1={X0} y1={Y} x2={X0 + PITCH * current} y2={Y} strokeWidth={2} className="stroke-accent/60" />

          {events.map((event, index) => {
            const tone = eventTone(event)
            const x = X0 + index * PITCH
            const isActive = index === current
            return (
              <g
                key={`${index}-${event}`}
                className={`cursor-pointer ${tone.cls}`}
                onMouseEnter={() => setActive(index)}
                onClick={() => setActive(index)}
                tabIndex={0}
              >
                <text x={x} y={Y - 18} textAnchor="middle" className="fill-idle text-[9px]">
                  #{index + 1}
                </text>
                <circle cx={x} cy={Y} r={isActive ? 11 : 8} fill="currentColor" fillOpacity={0.15} />
                <circle cx={x} cy={Y} r={isActive ? 5.5 : 4} fill="currentColor" />
                <text
                  x={x}
                  y={Y + 26}
                  textAnchor="middle"
                  className="fill-muted text-[9px]"
                  style={{ pointerEvents: 'none' }}
                >
                  {tone.label}
                </text>
              </g>
            )
          })}
        </svg>
      </div>

      <div className="mt-2 rounded-lg border border-line bg-canvas px-3 py-2 text-xs">
        <span className="font-mono text-accent">#{current + 1}</span>
        <span className={`ml-2 font-mono ${eventTone(events[current]).cls}`}>{events[current]}</span>
        <span className="ml-2 text-idle">
          （第 {current + 1} / {events.length} 步）
        </span>
      </div>
    </div>
  )
}
