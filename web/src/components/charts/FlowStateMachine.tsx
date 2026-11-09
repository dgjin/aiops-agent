/**
 * 流程状态机（交互式 SVG）：把「已接入 → 分析 → 修复 → 测试 → 审批 → 公告 → 发布」全链路
 * 与三道闸门、异常出口画成一张可点的图。
 *
 * 交互：
 * - 悬停 / Tab 聚焦 / 点击选中三个阶段态；聚焦节点时其余节点淡出，形成路径聚焦；
 * - 点击节点在下方详情条显示该阶段中文名、说明、完成情况与关联闸门事件条数；
 * - 键盘可达（Enter / Space 选中，Esc 取消），点击已选中节点再次取消。
 *
 * 事实：阶段顺序、闸门位置与中断点均取自流程自身的 stage + gate_events，
 * 不臆造顺序；终态流程复用列表行内流程图的同一套中断点推导（computeFlowProgress），
 * 保证两处对「停在哪一步」的判断一致。
 */

import { useState } from 'react'
import { computeFlowProgress } from '../FlowStageStrip'
import { STAGE_META } from '../StageBadge'

const MAIN_STAGES = [
  { key: 'DETECTED', label: '已接入', step: '01' },
  { key: 'TRIAGING', label: '分析中', step: '02' },
  { key: 'FIXING', label: '修复中', step: '03' },
  { key: 'TESTING', label: '测试中', step: '04' },
  { key: 'WAIT_APPROVAL', label: '待审批', step: '05' },
  { key: 'NOTIFYING', label: '公告倒计时', step: '06' },
  { key: 'CANARY', label: '金丝雀', step: '07' },
  { key: 'ROLLING_OUT', label: '全量发布', step: '08' },
  { key: 'DONE', label: '已完成', step: '09' },
] as const

type StageKey = (typeof MAIN_STAGES)[number]['key']

/** 阶段说明（悬停/选中详情条展示）。 */
const STAGE_HINT: Record<string, string> = {
  DETECTED: '告警接入，生成标准 Alert 事件',
  TRIAGING: '拉取日志证据 → Drain3 聚类 → LLM 根因判定',
  FIXING: 'Code RAG 检索 + 生成最小化补丁（应用 / 编译校验）',
  TESTING: '隔离沙箱执行单测 / 回归 / SAST，失败回炉重试',
  WAIT_APPROVAL: '闸门 2：运维审批（超时自动驳回；受保护目录需二级审批）',
  NOTIFYING: '闸门 3：用户公告 + 延迟发布倒计时（到期自动进入金丝雀）',
  CANARY: '金丝雀 5% 流量 + 真机观测（错误率 / P99）',
  ROLLING_OUT: '健康达标 → 全量滚动',
  DONE: '全量发布完成，进入观察期',
  ESCALATED: '转人工（置信度不足 / 测试耗尽 / 审批驳回 / 金丝雀劣化）',
  CANCELLED: '窗口内取消发布（队列保留，队首补丁开新周期）',
}

/** 闸门：锚定在主链某个阶段上方。 */
const GATES = [
  { index: 1, key: 'gate1', label: '闸门 1 置信度', tone: 'warn' },
  { index: 4, key: 'gate2', label: '闸门 2 运维审批', tone: 'danger' },
  { index: 5, key: 'gate3', label: '闸门 3 用户公告', tone: 'info' },
] as const

/** 可能触发异常的阶段：escalated（转人工）/ cancelled（取消）。 */
const EXITS: Record<number, 'escalated' | 'cancelled'> = {
  1: 'escalated',
  4: 'escalated',
  5: 'cancelled',
  6: 'escalated',
}

/** 阶段 → 关联闸门事件前缀（点击阶段时统计「这一步发生了什么」）。 */
const STAGE_EVENT_PREFIX: Record<StageKey, string[]> = {
  DETECTED: ['detected', 'alert'],
  TRIAGING: ['gate1:', 'triage'],
  FIXING: ['fix', 'patch'],
  TESTING: ['tests:', 'mr:', 'shadow:'],
  WAIT_APPROVAL: ['gate2:', 'gate2b:'],
  NOTIFYING: ['gate3:'],
  CANARY: ['canary:'],
  ROLLING_OUT: ['rollout', 'deploy:'],
  DONE: ['done'],
}

type NodeState = 'done' | 'active' | 'pending' | 'halted' | 'haltedIdle'

/** 节点态 → 色调 + 透明度：亮色实心会吞文字，故用低透明填充 + 色调描边。 */
const NODE_STYLE: Record<NodeState, { fillOpacity: number; strokeOpacity: number }> = {
  active: { fillOpacity: 0.2, strokeOpacity: 1 },
  done: { fillOpacity: 0.12, strokeOpacity: 0.75 },
  pending: { fillOpacity: 0.05, strokeOpacity: 0.55 },
  halted: { fillOpacity: 0.16, strokeOpacity: 0.95 },
  haltedIdle: { fillOpacity: 0.1, strokeOpacity: 0.6 },
}

const STATE_LABEL: Record<NodeState, string> = {
  done: '已完成',
  active: '当前进行中',
  pending: '未到达',
  halted: '在此转人工',
  haltedIdle: '在此终止',
}

const NODE_W = 96
const NODE_H = 36
const PITCH = 110
const X0 = 18
const Y = 84
const WIDTH = X0 * 2 + PITCH * (MAIN_STAGES.length - 1) + NODE_W
const HEIGHT = 178

const centerOf = (index: number) => X0 + index * PITCH + NODE_W / 2

export function FlowStateMachine({
  stage,
  events,
  onSelectStage,
  className,
}: {
  stage: string | null
  /** 闸门事件链（gate_events）：用于精确定位中断点与统计各阶段事件 */
  events?: string[]
  onSelectStage?: (stage: string) => void
  className?: string
}) {
  const [hover, setHover] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)

  const current = (stage ?? '').toUpperCase()
  const progress = computeFlowProgress(stage, events)
  const focus = hover ?? selected

  /** 主链节点态（终态按 doneCount / stoppedAfter 精确落位）。 */
  const stateOf = (index: number): NodeState => {
    if (progress.kind === 'running') {
      if (index < progress.current) return 'done'
      if (index === progress.current) return 'active'
      return 'pending'
    }
    if (progress.kind === 'done' || progress.kind === 'shadowed') return 'done'
    if (index < progress.doneCount) return 'done'
    if (index === progress.stoppedAfter) {
      return progress.kind === 'escalated' ? 'halted' : 'haltedIdle'
    }
    return 'pending'
  }

  /** 主链是否在此中断（终态且中断点已知）。 */
  const stopAt = progress.kind === 'running' || progress.kind === 'done' || progress.kind === 'shadowed'
    ? null
    : progress.stoppedAfter

  const toneOf = (state: NodeState, key: StageKey): string => {
    if (state === 'active') return STAGE_META[key]?.tone === 'warn' ? 'text-warn' : 'text-accent'
    if (state === 'done') return 'text-ok'
    if (state === 'halted') return 'text-danger'
    if (state === 'haltedIdle') return 'text-idle'
    return 'text-idle'
  }

  const focusKey = focus
  const focusStage = focusKey ? MAIN_STAGES.find((item) => item.key === focusKey) : undefined
  const focusState = focusStage ? stateOf(MAIN_STAGES.indexOf(focusStage)) : null
  const focusPrefix = focusKey ? STAGE_EVENT_PREFIX[focusKey as StageKey] : undefined
  const focusEvents = focusPrefix && events?.length
    ? events.filter((event) => focusPrefix.some((prefix) => event.startsWith(prefix)))
    : []
  const focusHint = focusKey ? STAGE_HINT[focusKey] ?? '—' : null

  const toggle = (key: string) => {
    setSelected((prev) => (prev === key ? null : key))
    onSelectStage?.(key)
  }

  return (
    <div className={className}>
      <div className="overflow-x-auto">
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          className="h-auto w-full min-w-[880px]"
          role="img"
          aria-label={`流程状态机，当前阶段 ${stage ?? '未知'}`}
        >
          <defs>
            <marker id="fsm-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
              <path d="M0,0 L7,3.5 L0,7 z" className="fill-line" />
            </marker>
            <marker id="fsm-arrow-ok" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
              <path d="M0,0 L7,3.5 L0,7 z" className="fill-ok" />
            </marker>
            <marker id="fsm-arrow-danger" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
              <path d="M0,0 L7,3.5 L0,7 z" className="fill-danger" />
            </marker>
            <marker id="fsm-arrow-warn" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
              <path d="M0,0 L7,3.5 L0,7 z" className="fill-warn" />
            </marker>
          </defs>

          {/* 主链连线：已完成的段用流动虚线，强调事件沿链路推进 */}
          {MAIN_STAGES.slice(0, -1).map((item, index) => {
            const from = X0 + index * PITCH + NODE_W
            const to = X0 + (index + 1) * PITCH
            const lit = stateOf(index) === 'done' && stateOf(index + 1) !== 'pending'
            const dim = focusKey !== null && focusKey !== item.key && focusKey !== MAIN_STAGES[index + 1].key
            return (
              <line
                key={`link-${item.key}`}
                x1={from}
                y1={Y}
                x2={to - 3}
                y2={Y}
                strokeWidth={2}
                strokeDasharray={lit ? '5 5' : undefined}
                className={`${lit ? 'stroke-ok flow-dash' : 'stroke-line'} transition-opacity`}
                opacity={dim ? 0.35 : 1}
                markerEnd={lit ? 'url(#fsm-arrow-ok)' : 'url(#fsm-arrow)'}
              />
            )
          })}

          {/* 回炉重试环（TESTING → FIXING），悬停测试阶段时加亮 */}
          <path
            d={`M ${centerOf(3)} ${Y - NODE_H / 2 - 2} C ${centerOf(3)} ${Y - 44}, ${centerOf(2)} ${Y - 44}, ${centerOf(2)} ${Y - NODE_H / 2 - 2}`}
            fill="none"
            strokeWidth={1.5}
            strokeDasharray="4 3"
            className="stroke-warn transition-opacity"
            opacity={focusKey !== null && focusKey !== 'FIXING' && focusKey !== 'TESTING' ? 0.25 : 1}
            markerEnd="url(#fsm-arrow-warn)"
          />
          <text
            x={(centerOf(2) + centerOf(3)) / 2}
            y={Y - 48}
            textAnchor="middle"
            className="fill-warn text-[10px] transition-opacity"
            opacity={focusKey !== null && focusKey !== 'FIXING' && focusKey !== 'TESTING' ? 0.25 : 1}
          >
            失败回炉
          </text>

          {/* 闸门行：胶囊 + 落到节点的竖虚线 */}
          {GATES.map((gate) => {
            const cx = centerOf(gate.index)
            const tone = gate.tone === 'warn' ? 'text-warn' : gate.tone === 'danger' ? 'text-danger' : 'text-info'
            const dim = focusKey !== null && focusKey !== MAIN_STAGES[gate.index].key
            return (
              <g key={gate.key} className={`${tone} transition-opacity`} opacity={dim ? 0.3 : 1}>
                <rect
                  x={cx - 52}
                  y={12}
                  width={104}
                  height={22}
                  rx={11}
                  fill="currentColor"
                  fillOpacity={0.1}
                  stroke="currentColor"
                  strokeOpacity={0.45}
                />
                <text x={cx} y={27} textAnchor="middle" className="fill-ink text-[10px]">
                  {gate.label}
                </text>
                <line
                  x1={cx}
                  y1={34}
                  x2={cx}
                  y2={Y - NODE_H / 2 - 4}
                  strokeWidth={1}
                  strokeDasharray="2 3"
                  stroke="currentColor"
                  strokeOpacity={0.4}
                />
              </g>
            )
          })}

          {/* 主链节点 */}
          {MAIN_STAGES.map((item, index) => {
            const x = X0 + index * PITCH
            const state = stateOf(index)
            const tone = toneOf(state, item.key)
            const isFocus = focus === item.key
            const isSelected = selected === item.key
            const dim = focusKey !== null && !isFocus
            return (
              <g
                key={item.key}
                className={`${tone} cursor-pointer outline-none transition-opacity`}
                opacity={dim ? 0.45 : 1}
                role="button"
                tabIndex={0}
                aria-pressed={isSelected}
                aria-label={`${item.step} ${item.label} ${item.key}：${STATE_LABEL[state]}。${STAGE_HINT[item.key] ?? ''}`}
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
                {/* 选中光环 */}
                {(isFocus || state === 'active') && (
                  <rect
                    x={x - 4}
                    y={Y - NODE_H / 2 - 4}
                    width={NODE_W + 8}
                    height={NODE_H + 8}
                    rx={12}
                    fill="none"
                    stroke="currentColor"
                    strokeOpacity={isSelected ? 0.55 : 0.3}
                    strokeWidth={1.5}
                    className={state === 'active' && !isSelected ? 'animate-pulse' : undefined}
                  />
                )}
                <rect
                  x={x}
                  y={Y - NODE_H / 2}
                  width={NODE_W}
                  height={NODE_H}
                  rx={9}
                  fill="currentColor"
                  fillOpacity={NODE_STYLE[state].fillOpacity + (isFocus ? 0.06 : 0)}
                  stroke="currentColor"
                  strokeOpacity={isFocus ? 1 : NODE_STYLE[state].strokeOpacity}
                  strokeWidth={isFocus ? 2 : 1}
                />
                <text
                  x={x + NODE_W / 2}
                  y={Y - 3}
                  textAnchor="middle"
                  className="fill-ink text-[11px] font-medium"
                  style={{ pointerEvents: 'none' }}
                >
                  {item.label}
                </text>
                <text
                  x={x + NODE_W / 2}
                  y={Y + 11}
                  textAnchor="middle"
                  className="fill-muted text-[8px] tracking-tight"
                  style={{ pointerEvents: 'none' }}
                >
                  {item.step} {item.key}
                </text>
                {state === 'active' && (
                  <circle cx={x + NODE_W - 9} cy={Y - NODE_H / 2 + 9} r={3} className="fill-current animate-pulse" />
                )}
                {state === 'halted' && (
                  <circle cx={x + NODE_W - 9} cy={Y - NODE_H / 2 + 9} r={3} className="fill-danger" />
                )}
              </g>
            )
          })}

          {/* 异常出口基线：每个可能中断的阶段在节点下方留触发点，实际中断处落一条实线 */}
          <line
            x1={150}
            y1={148}
            x2={WIDTH - 150}
            y2={148}
            strokeWidth={1}
            strokeDasharray="3 4"
            className="stroke-line"
          />
          {Object.entries(EXITS).map(([index, kind]) => {
            const idx = Number(index)
            const cx = centerOf(idx)
            const active = stopAt === idx
            return (
              <g key={`exit-${index}`}>
                <circle
                  cx={cx}
                  cy={Y + NODE_H / 2 + 6}
                  r={active ? 3.2 : 1.8}
                  className={active ? (kind === 'escalated' ? 'fill-danger' : 'fill-idle') : 'fill-line'}
                  opacity={active ? 1 : 0.7}
                />
                {active && (
                  <line
                    x1={cx}
                    y1={Y + NODE_H / 2 + 9}
                    x2={cx}
                    y2={146}
                    strokeWidth={1.5}
                    strokeDasharray="4 3"
                    className={kind === 'escalated' ? 'stroke-danger' : 'stroke-idle'}
                  />
                )}
              </g>
            )
          })}

          {/* 异常出口胶囊 */}
          {([
            { x: X0, kind: 'escalated' as const, label: '转人工 ESCALATED' },
            { x: WIDTH - X0 - 118, kind: 'cancelled' as const, label: '已取消 CANCELLED' },
          ]).map((exit) => {
            const active = current === exit.kind.toUpperCase()
            const tone = exit.kind === 'escalated' ? 'text-danger' : 'text-idle'
            return (
              <g
                key={exit.kind}
                className={`${tone} cursor-pointer transition-opacity`}
                opacity={focusKey !== null && focusKey !== exit.kind.toUpperCase() ? 0.5 : 1}
                role="button"
                tabIndex={0}
                aria-label={`${exit.label}：${STAGE_HINT[exit.kind.toUpperCase()] ?? ''}`}
                onMouseEnter={() => setHover(exit.kind.toUpperCase())}
                onMouseLeave={() => setHover(null)}
                onFocus={() => setHover(exit.kind.toUpperCase())}
                onBlur={() => setHover(null)}
                onClick={() => toggle(exit.kind.toUpperCase())}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    toggle(exit.kind.toUpperCase())
                  }
                }}
              >
                <rect
                  x={exit.x}
                  y={134}
                  width={118}
                  height={28}
                  rx={9}
                  fill="currentColor"
                  fillOpacity={active ? 0.2 : 0.08}
                  stroke="currentColor"
                  strokeWidth={active ? 2 : 1}
                  strokeOpacity={active ? 0.95 : 0.5}
                />
                <text x={exit.x + 59} y={152} textAnchor="middle" className="fill-ink text-[10px]">
                  {exit.label}
                </text>
              </g>
            )
          })}
        </svg>
      </div>

      {/* 详情条：聚焦阶段的中文名 / 说明 / 状态 / 关联事件 */}
      <div
        className="mt-2 min-h-[2.75rem] rounded-lg border border-line bg-canvas px-3 py-1.5 text-[11px] leading-5"
        aria-live="polite"
      >
        {focusKey && focusHint ? (
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-ink">{STAGE_META[focusKey]?.label ?? focusKey}</span>
              <span className="font-mono text-[10px] text-muted">{focusKey}</span>
              {focusState && (
                <span
                  className={`rounded px-1.5 py-0.5 text-[10px] ${
                    focusState === 'active'
                      ? 'bg-accent/10 text-accent'
                      : focusState === 'done'
                        ? 'bg-ok/10 text-ok'
                        : focusState === 'halted'
                          ? 'bg-danger/10 text-danger'
                          : 'bg-elevated text-muted'
                  }`}
                >
                  {STATE_LABEL[focusState]}
                </span>
              )}
              {focusEvents.length > 0 && (
                <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] text-muted">
                  关联事件 {focusEvents.length} 条
                </span>
              )}
            </div>
            <div className="text-muted">{focusHint}</div>
            {focusEvents.length > 0 && (
              <div className="truncate font-mono text-[10px] text-idle" title={focusEvents[focusEvents.length - 1]}>
                最近：{focusEvents[focusEvents.length - 1]}
              </div>
            )}
          </div>
        ) : (
          <span className="text-idle">
            悬停、Tab 聚焦或点击阶段查看说明；点击阶段会标出它在闸门事件里的留痕条数。虚线为异常出口（转人工 / 取消）。
          </span>
        )}
      </div>
    </div>
  )
}
