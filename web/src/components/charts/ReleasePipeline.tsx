/**
 * 发布管道（交互式 SVG）：审批通过 → 公告倒计时 → 金丝雀 5% → 全量发布 → 观察期。
 *
 * - 当前阶段高亮，已完成阶段填充；
 * - 「公告倒计时」阶段叠加 SVG 环形进度（依据 deadline 与策略窗口长度，无窗口长度时不画进度、只显示剩余秒数）；
 * - 悬停阶段显示说明；支持发布结局（晋级 / 回滚）分支展示。
 */

import { useState } from 'react'
import { useNowTick } from '../../lib/hooks'
import { serverNow } from '../../lib/api'

const STAGES = [
  { key: 'APPROVED', label: '审批通过', hint: '闸门 2：运维审批放行（受保护目录含二级审批）' },
  { key: 'NOTIFYING', label: '公告倒计时', hint: '闸门 3：用户公告 + 延迟发布；到期自动进入金丝雀' },
  { key: 'CANARY', label: '金丝雀 5%', hint: '小流量真机观测：错误率 / P99 对比 SLO' },
  { key: 'ROLLING_OUT', label: '全量发布', hint: '健康达标后全量滚动；就绪前保留金丝雀回退' },
  { key: 'OBSERVING', label: '观察期', hint: '观察新版本线上表现，异常自动关联本次变更' },
] as const

const CANARY_TO_ROLL = ['CANARY']
const ROLL_TO_OBSERVE = ['ROLLING_OUT', 'DONE']

/** 由流程阶段推导管道进度位置。 */
function positionOf(stage: string | null): number {
  const key = (stage ?? '').toUpperCase()
  if (ROLL_TO_OBSERVE.includes(key)) return 4
  if (CANARY_TO_ROLL.includes(key)) return 2
  if (key === 'NOTIFYING') return 1
  return 0
}

export function ReleasePipeline({
  stage,
  deadlineAt,
  windowSeconds,
  rolledBack,
  version,
  className,
}: {
  stage: string | null
  deadlineAt?: string | null
  /** 公告窗口总长度（秒）；缺省则不画进度环 */
  windowSeconds?: number | null
  /** 发布结局：true 表示已回滚 */
  rolledBack?: boolean
  version?: string | null
  className?: string
}) {
  useNowTick()
  const [hover, setHover] = useState<string | null>(null)

  const pos = positionOf(stage)
  const failed = rolledBack === true

  const remainingMs = deadlineAt ? Math.max(0, Date.parse(deadlineAt) - serverNow()) : 0
  const remainingSec = Math.ceil(remainingMs / 1000)
  const progress =
    windowSeconds && windowSeconds > 0 ? Math.min(1, Math.max(0, 1 - remainingMs / (windowSeconds * 1000))) : null

  const CH_W = 128
  const CH_H = 40
  const GAP = 6
  const X0 = 26
  const Y = 34
  const WIDTH = X0 * 2 + STAGES.length * CH_W + (STAGES.length - 1) * GAP
  const HEIGHT = failed ? 116 : 96

  const chevron = (x: number) =>
    `M ${x} ${Y - CH_H / 2} L ${x + CH_W - 12} ${Y - CH_H / 2} L ${x + CH_W} ${Y} ` +
    `L ${x + CH_W - 12} ${Y + CH_H / 2} L ${x} ${Y + CH_H / 2} L ${x + 12} ${Y} Z`

  return (
    <div className={className}>
      <div className="overflow-x-auto">
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="h-auto w-full min-w-[680px]" role="img" aria-label="发布管道">
          {STAGES.map((item, index) => {
            const x = X0 + index * (CH_W + GAP)
            const done = index < pos
            const active = index === pos
            const tone = failed && active ? 'text-danger' : active ? 'text-accent' : done ? 'text-ok' : 'text-muted'
            return (
              <g
                key={item.key}
                className={`cursor-pointer ${tone}`}
                onMouseEnter={() => setHover(item.key)}
                onMouseLeave={() => setHover(null)}
              >
                <path
                  d={chevron(x)}
                  fill="currentColor"
                  fillOpacity={active ? 0.18 : done ? 0.1 : 0.05}
                  stroke="currentColor"
                  strokeWidth={hover === item.key || active ? 2 : 1}
                />
                <text x={x + CH_W / 2 + 2} y={Y + 4} textAnchor="middle" className="fill-ink text-[11px]" style={{ pointerEvents: 'none' }}>
                  {item.label}
                </text>
              </g>
            )
          })}

          {/* 公告阶段的倒计时环形进度（挂在第 2 段上方） */}
          {pos === 1 && (
            <g transform={`translate(${X0 + (CH_W + GAP) + CH_W / 2}, ${Y - CH_H / 2 - 26})`}>
              <circle r={18} fill="none" strokeWidth={4} className="stroke-line" />
              <circle
                r={18}
                fill="none"
                strokeWidth={4}
                className="stroke-warn"
                strokeDasharray={`${2 * Math.PI * 18} ${2 * Math.PI * 18}`}
                strokeDashoffset={progress === null ? 0 : 2 * Math.PI * 18 * progress}
                transform="rotate(-90)"
                strokeLinecap="round"
                opacity={progress === null ? 0.35 : 1}
              />
              <text y={4} textAnchor="middle" className="fill-warn text-[11px] font-semibold">
                {remainingSec}s
              </text>
            </g>
          )}

          {/* 结局分支 */}
          {failed && (
            <g>
              <path
                d={`M ${X0 + (CH_W + GAP) * 2 + CH_W / 2} ${Y + CH_H / 2} C ${X0 + (CH_W + GAP) * 2 + CH_W / 2} ${Y + 46}, 120 ${Y + 40}, 120 ${Y + 58}`}
                fill="none"
                strokeWidth={1.5}
                strokeDasharray="4 3"
                className="stroke-danger"
              />
              <rect x={40} y={Y + 58} width={220} height={26} rx={7} className="fill-danger" fillOpacity={0.12} stroke="var(--color-danger)" strokeWidth={1} />
              <text x={150} y={Y + 75} textAnchor="middle" className="fill-danger text-[11px]">
                金丝雀劣化 → 自动回滚（稳定版不变）
              </text>
            </g>
          )}

          {version && (
            <text x={WIDTH - X0} y={Y + CH_H / 2 + 26} textAnchor="end" className="fill-muted text-[10px]">
              目标版本 {version}
            </text>
          )}
        </svg>
      </div>

      <div className="mt-2 min-h-[2.25rem] rounded-lg border border-line bg-canvas px-3 py-1.5 text-xs">
        {hover ? (
          <span>
            <span className="font-mono text-accent">{hover}</span>
            <span className="ml-2 text-muted">{STAGES.find((s) => s.key === hover)?.hint}</span>
          </span>
        ) : (
          <span className="text-idle">
            当前阶段：<span className="font-mono">{stage ?? '—'}</span>
            {progress !== null && pos === 1 && <span className="ml-2">倒计时进度 {(progress * 100).toFixed(0)}%</span>}
          </span>
        )}
      </div>
    </div>
  )
}
