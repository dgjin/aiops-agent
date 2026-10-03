/**
 * 交互式 SVG 环图：悬停高亮 + 中心读数 + 点击回调。
 * 用于看板「近 7 日终态分布」，替代原先的纯 CSS 占比条。
 */

import { useState } from 'react'

export interface DonutSegment {
  key: string
  label: string
  value: number
  /** Tailwind 文本色类（stroke/fill 走 currentColor），如 text-ok */
  tone: string
}

const R = 46
const CX = 60
const CY = 60
const C = 2 * Math.PI * R

export function Donut({
  segments,
  centerLabel = '共',
  onSelect,
}: {
  segments: DonutSegment[]
  centerLabel?: string
  onSelect?: (key: string) => void
}) {
  const [hover, setHover] = useState<string | null>(null)
  const total = segments.reduce((sum, seg) => sum + seg.value, 0)

  if (!total) {
    return <div className="py-6 text-center text-xs text-idle">暂无数据</div>
  }

  let acc = 0
  const arcs = segments
    .filter((seg) => seg.value > 0)
    .map((seg) => {
      const len = (seg.value / total) * C
      const arc = { ...seg, len, offset: acc }
      acc += len
      return arc
    })

  const hovered = segments.find((seg) => seg.key === hover)
  const centerValue = hovered ? hovered.value : total
  const centerText = hovered ? hovered.label : centerLabel

  return (
    <div className="flex flex-wrap items-center gap-6">
      <svg viewBox="0 0 120 120" className="h-36 w-36 shrink-0" role="img" aria-label="终态分布环图">
        <circle cx={CX} cy={CY} r={R} fill="none" strokeWidth={14} className="stroke-elevated" />
        {arcs.map((arc) => (
          <circle
            key={arc.key}
            cx={CX}
            cy={CY}
            r={R}
            fill="none"
            strokeWidth={hover === arc.key ? 19 : 14}
            strokeDasharray={`${arc.len} ${C - arc.len}`}
            strokeDashoffset={-arc.offset}
            transform={`rotate(-90 ${CX} ${CY})`}
            strokeLinecap="butt"
            fillOpacity={hover === arc.key ? 1 : 0.85}
            className={`cursor-pointer transition-[stroke-width] ${arc.tone} ${hover && hover !== arc.key ? 'opacity-40' : ''}`}
            stroke="currentColor"
            onMouseEnter={() => setHover(arc.key)}
            onMouseLeave={() => setHover(null)}
            onClick={() => onSelect?.(arc.key)}
            tabIndex={0}
          >
            <title>{`${arc.label}：${arc.value}（${((arc.value / total) * 100).toFixed(0)}%）`}</title>
          </circle>
        ))}
        <text x={CX} y={CY - 2} textAnchor="middle" className="fill-ink text-[16px] font-semibold">
          {centerValue}
        </text>
        <text x={CX} y={CY + 13} textAnchor="middle" className="fill-muted text-[9px]">
          {centerText}
        </text>
      </svg>

      <ul className="min-w-0 flex-1 space-y-1.5 text-xs">
        {segments.map((seg) => (
          <li
            key={seg.key}
            className={`flex cursor-pointer items-center gap-2 rounded px-1.5 py-0.5 ${
              hover === seg.key ? 'bg-elevated' : ''
            }`}
            onMouseEnter={() => setHover(seg.key)}
            onMouseLeave={() => setHover(null)}
            onClick={() => onSelect?.(seg.key)}
          >
            <span className={`h-2 w-2 shrink-0 rounded-full ${seg.tone.replace('text-', 'bg-')}`} />
            <span className="text-muted">{seg.label}</span>
            <span className="ml-auto font-mono text-ink">{seg.value}</span>
            <span className="w-10 text-right font-mono text-idle">
              {((seg.value / total) * 100).toFixed(0)}%
            </span>
          </li>
        ))}
        <li className="px-1.5 pt-1 text-idle">点击任一分类可跳转流程列表</li>
      </ul>
    </div>
  )
}
