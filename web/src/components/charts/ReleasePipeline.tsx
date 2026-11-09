/**
 * 发布管道（交互式 SVG）：审批通过 → 公告倒计时 → 金丝雀 5% → 全量发布 → 观察期。
 *
 * 交互：
 * - 悬停 / Tab 聚焦 / 点击选中三个阶段态；聚焦节点时其余节点淡出；
 * - 点击阶段在下方详情条显示说明、完成情况与（公告期）剩余秒数；
 * - 键盘可达（Enter / Space 选中，Esc 取消）。
 *
 * 事实：「公告倒计时」阶段叠加环形进度（依据 deadline 与策略窗口长度；
 * 无窗口长度时不画进度、只显示剩余秒数）；回滚结局只在真实回滚时出现。
 */

import { useState } from 'react'
import { useNowTick } from '../../lib/hooks'
import { serverNow } from '../../lib/api'

const STAGES = [
  { key: 'APPROVED', step: '01', label: '审批通过', hint: '闸门 2：运维审批放行（受保护目录含二级审批）' },
  { key: 'NOTIFYING', step: '02', label: '公告倒计时', hint: '闸门 3：用户公告 + 延迟发布；到期自动进入金丝雀' },
  { key: 'CANARY', step: '03', label: '金丝雀 5%', hint: '小流量真机观测：错误率 / P99 对比 SLO' },
  { key: 'ROLLING_OUT', step: '04', label: '全量发布', hint: '健康达标后全量滚动；就绪前保留金丝雀回退' },
  { key: 'OBSERVING', step: '05', label: '观察期', hint: '观察新版本线上表现，异常自动关联本次变更' },
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

const CH_W = 132
const CH_H = 44
const GAP = 12
const X0 = 24
const Y = 56
const WIDTH = X0 * 2 + STAGES.length * CH_W + (STAGES.length - 1) * GAP

const STATE_LABEL = { done: '已完成', active: '当前进行中', pending: '未到达' } as const

export function ReleasePipeline({
  stage,
  deadlineAt,
  windowSeconds,
  rolledBack,
  version,
  onSelectStage,
  className,
}: {
  stage: string | null
  deadlineAt?: string | null
  /** 公告窗口总长度（秒）；缺省则不画进度环 */
  windowSeconds?: number | null
  /** 发布结局：true 表示已回滚 */
  rolledBack?: boolean
  version?: string | null
  onSelectStage?: (stage: string) => void
  className?: string
}) {
  useNowTick()
  const [hover, setHover] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)

  const pos = positionOf(stage)
  const failed = rolledBack === true
  const focus = hover ?? selected

  const remainingMs = deadlineAt ? Math.max(0, Date.parse(deadlineAt) - serverNow()) : 0
  const remainingSec = Math.ceil(remainingMs / 1000)
  const progress =
    windowSeconds && windowSeconds > 0 ? Math.min(1, Math.max(0, 1 - remainingMs / (windowSeconds * 1000))) : null

  const HEIGHT = failed ? 152 : 124

  const chevron = (x: number) =>
    `M ${x} ${Y - CH_H / 2} L ${x + CH_W - 12} ${Y - CH_H / 2} L ${x + CH_W} ${Y} ` +
    `L ${x + CH_W - 12} ${Y + CH_H / 2} L ${x} ${Y + CH_H / 2} L ${x + 12} ${Y} Z`

  const stateOf = (index: number): 'done' | 'active' | 'pending' => {
    if (index < pos) return 'done'
    if (index === pos) return 'active'
    return 'pending'
  }

  const toggle = (key: string) => {
    setSelected((prev) => (prev === key ? null : key))
    onSelectStage?.(key)
  }

  const focusStage = focus ? STAGES.find((item) => item.key === focus) : undefined
  const focusIndex = focusStage ? STAGES.indexOf(focusStage) : -1
  const focusState = focusIndex >= 0 ? stateOf(focusIndex) : null

  return (
    <div className={className}>
      <div className="overflow-x-auto">
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          className="h-auto w-full min-w-[700px]"
          role="img"
          aria-label={`发布管道，当前阶段 ${stage ?? '未知'}${failed ? '，已回滚' : ''}`}
        >
          <defs>
            <marker id="rp-arrow-danger" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
              <path d="M0,0 L7,3.5 L0,7 z" className="fill-danger" />
            </marker>
          </defs>
          {STAGES.map((item, index) => {
            const x = X0 + index * (CH_W + GAP)
            const state = stateOf(index)
            const isFocus = focus === item.key
            const isSelected = selected === item.key
            const dim = focus !== null && !isFocus
            const tone =
              failed && state === 'active'
                ? 'text-danger'
                : state === 'active'
                  ? item.key === 'NOTIFYING'
                    ? 'text-warn'
                    : 'text-accent'
                  : state === 'done'
                    ? 'text-ok'
                    : 'text-muted'
            return (
              <g
                key={item.key}
                className={`${tone} cursor-pointer outline-none transition-opacity`}
                opacity={dim ? 0.45 : 1}
                role="button"
                tabIndex={0}
                aria-pressed={isSelected}
                aria-label={`${item.step} ${item.label} ${item.key}：${STATE_LABEL[state]}。${item.hint}`}
                onMouseEnter={() => setHover(item.key)}
                onMouseLeave={() => setHover(null)}
                onFocus={() => setHover(item.key)}
                onBlur={() => setHover(null)}
                onClick={() => toggle(item.key)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    toggle(item.key)
                  }
                  if (event.key === 'Escape') setSelected(null)
                }}
              >
                {/* 当前阶段外环（脉冲） */}
                {(isFocus || state === 'active') && (
                  <path
                    d={chevron(x)}
                    fill="none"
                    stroke="currentColor"
                    strokeOpacity={isSelected ? 0.5 : 0.3}
                    strokeWidth={4}
                    className={state === 'active' && !isSelected ? 'animate-pulse' : undefined}
                    style={{ pointerEvents: 'none' }}
                  />
                )}
                <path
                  d={chevron(x)}
                  fill="currentColor"
                  fillOpacity={state === 'active' ? 0.2 : state === 'done' ? 0.11 : 0.05}
                  stroke="currentColor"
                  strokeOpacity={isFocus ? 1 : 0.75}
                  strokeWidth={isFocus ? 2.5 : 1}
                />
                <text
                  x={x + CH_W / 2 + 2}
                  y={Y - 3}
                  textAnchor="middle"
                  className="fill-ink text-[11px] font-medium"
                  style={{ pointerEvents: 'none' }}
                >
                  {item.label}
                </text>
                <text
                  x={x + CH_W / 2 + 2}
                  y={Y + 12}
                  textAnchor="middle"
                  className="fill-muted text-[9px]"
                  style={{ pointerEvents: 'none' }}
                >
                  {item.step} · {item.key}
                </text>
              </g>
            )
          })}

          {/* 公告阶段的倒计时环（挂在第 2 段上方） */}
          {pos === 1 && (
            <g transform={`translate(${X0 + (CH_W + GAP) + CH_W / 2}, 17)`} style={{ pointerEvents: 'none' }}>
              <circle r={15} fill="none" strokeWidth={3.5} className="stroke-line" />
              <circle
                r={15}
                fill="none"
                strokeWidth={3.5}
                className="stroke-warn"
                strokeDasharray={`${2 * Math.PI * 15} ${2 * Math.PI * 15}`}
                strokeDashoffset={progress === null ? 0 : 2 * Math.PI * 15 * progress}
                transform="rotate(-90)"
                strokeLinecap="round"
                opacity={progress === null ? 0.35 : 1}
              />
              <text y={3.5} textAnchor="middle" className="fill-warn text-[10px] font-semibold">
                {remainingSec}s
              </text>
            </g>
          )}

          {/* 结局分支：只在真实回滚时出现（终点落在胶囊上沿，箭头可见） */}
          {failed && (
            <g>
              <path
                d={`M ${X0 + (CH_W + GAP) * 2 + CH_W / 2} ${Y + CH_H / 2} C ${X0 + (CH_W + GAP) * 2 + CH_W / 2} ${
                  Y + 42
                }, ${X0 + 118} ${Y + 40}, ${X0 + 118} ${Y + 50}`}
                fill="none"
                strokeWidth={1.5}
                strokeDasharray="4 3"
                className="stroke-danger"
                markerEnd="url(#rp-arrow-danger)"
              />
              <rect
                x={X0}
                y={Y + 52}
                width={236}
                height={28}
                rx={9}
                className="fill-danger"
                fillOpacity={0.12}
                stroke="var(--color-danger)"
                strokeWidth={1}
              />
              <text x={X0 + 118} y={Y + 70} textAnchor="middle" className="fill-danger text-[11px]">
                金丝雀劣化 → 自动回滚（稳定版不变）
              </text>
            </g>
          )}

          {version && (
            <text x={WIDTH - X0} y={Y - CH_H / 2 - 8} textAnchor="end" className="fill-muted text-[10px]">
              目标版本 {version}
            </text>
          )}
        </svg>
      </div>

      {/* 详情条：聚焦阶段的说明与完成情况 */}
      <div
        className="mt-2 min-h-[3rem] rounded-lg border border-line bg-canvas px-3 py-2 text-xs"
        aria-live="polite"
      >
        {focusStage && focusState ? (
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-ink">{focusStage.label}</span>
              <span className="font-mono text-[10px] text-muted">{focusStage.key}</span>
              <span
                className={`rounded px-1.5 py-0.5 text-[10px] ${
                  focusState === 'active'
                    ? failed
                      ? 'bg-danger/10 text-danger'
                      : focusStage.key === 'NOTIFYING'
                        ? 'bg-warn/10 text-warn'
                        : 'bg-accent/10 text-accent'
                    : focusState === 'done'
                      ? 'bg-ok/10 text-ok'
                      : 'bg-elevated text-muted'
                }`}
              >
                {STATE_LABEL[focusState]}
              </span>
              {focusStage.key === 'NOTIFYING' && (
                <span className="rounded bg-warn/10 px-1.5 py-0.5 font-mono text-[10px] text-warn">
                  剩余 {remainingSec}s
                </span>
              )}
            </div>
            <div className="text-muted">{focusStage.hint}</div>
          </div>
        ) : (
          <span className="text-idle">
            当前阶段：<span className="font-mono">{stage ?? '—'}</span>
            {progress !== null && pos === 1 && <span className="ml-2">倒计时进度 {(progress * 100).toFixed(0)}%</span>}
            <span className="ml-2">· 悬停、Tab 聚焦或点击阶段查看说明</span>
          </span>
        )}
      </div>
    </div>
  )
}
