/**
 * 流程状态机（交互式 SVG）：把「监控 → 分析 → 修复 → 测试 → 审批 → 公告 → 发布」全链路
 * 与三道闸门、异常出口画成一张可点的图，替代原先的纯文本/JSON 展示。
 *
 * 交互：悬停节点 → 提示；点击节点 → onSelectStage（调用方可联动下方面板/筛选）。
 */

import { useState } from 'react'

const MAIN_STAGES = [
  { key: 'DETECTED', label: '已接入' },
  { key: 'TRIAGING', label: '分析中' },
  { key: 'FIXING', label: '修复中' },
  { key: 'TESTING', label: '测试中' },
  { key: 'WAIT_APPROVAL', label: '待审批' },
  { key: 'NOTIFYING', label: '公告倒计时' },
  { key: 'CANARY', label: '金丝雀' },
  { key: 'ROLLING_OUT', label: '全量发布' },
  { key: 'DONE', label: '已完成' },
] as const

const STAGE_HINT: Record<string, string> = {
  DETECTED: '告警接入，生成标准 Alert 事件',
  TRIAGING: '拉取日志证据 → Drain3 聚类 → LLM 根因判定',
  FIXING: 'Code RAG 检索 + 生成最小化补丁（应用/编译校验）',
  TESTING: '隔离沙箱执行单测/回归/SAST（失败回炉重试）',
  WAIT_APPROVAL: '闸门 2：运维审批（超时自动驳回；受保护目录需二级审批）',
  NOTIFYING: '闸门 3：用户公告 + 延迟发布倒计时（到期自动进入金丝雀）',
  CANARY: '金丝雀 5% 流量 + 真实观测（错误率 / P99）',
  ROLLING_OUT: '健康达标 → 全量滚动',
  DONE: '全量发布完成，进入观察期',
  ESCALATED: '转人工（置信度不足 / 测试耗尽 / 审批驳回 / 金丝雀劣化）',
  CANCELLED: '窗口内取消发布（队列保留，队首补丁开新周期）',
}

const TERMINAL_NEGATIVE = new Set(['ESCALATED', 'CANCELLED'])

export function FlowStateMachine({
  stage,
  onSelectStage,
  className,
}: {
  stage: string | null
  onSelectStage?: (stage: string) => void
  className?: string
}) {
  const [hover, setHover] = useState<string | null>(null)

  const current = (stage ?? '').toUpperCase()
  const currentIdx = MAIN_STAGES.findIndex((item) => item.key === current)
  const interrupted = TERMINAL_NEGATIVE.has(current)

  /** 节点态：done（已走完）/ active（当前）/ pending（未到）/ aborted（异常中断后的主链） */
  const stateOf = (index: number, key: string): 'done' | 'active' | 'pending' | 'aborted' => {
    if (current === key) return 'active'
    if (interrupted) return index === 0 ? 'done' : 'aborted'
    if (currentIdx < 0) return 'pending'
    return index < currentIdx ? 'done' : 'pending'
  }

  const NODE_W = 88
  const NODE_H = 34
  const PITCH = 100
  const X0 = 16
  const Y = 62
  const WIDTH = X0 * 2 + PITCH * (MAIN_STAGES.length - 1) + NODE_W
  const HEIGHT = 214

  const fill = (state: string) =>
    state === 'active'
      ? 'bg-accent/15 text-accent'
      : state === 'done'
        ? 'bg-ok/10 text-ok'
        : state === 'aborted'
          ? 'bg-elevated text-idle'
          : 'bg-panel text-muted'

  return (
    <div className={className}>
      <div className="overflow-x-auto">
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="h-auto w-full min-w-[720px]" role="img" aria-label="流程状态机">
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
          </defs>

          {/* 主链连线 */}
          {MAIN_STAGES.slice(0, -1).map((item, index) => {
            const from = X0 + index * PITCH + NODE_W
            const to = X0 + (index + 1) * PITCH
            const lit = stateOf(index, item.key) === 'done' && !interrupted
            return (
              <line
                key={`link-${item.key}`}
                x1={from}
                y1={Y}
                x2={to - 2}
                y2={Y}
                strokeWidth={2}
                className={lit ? 'stroke-ok' : 'stroke-line'}
                markerEnd={lit ? 'url(#fsm-arrow-ok)' : 'url(#fsm-arrow)'}
              />
            )
          })}

          {/* 回炉重试环（TESTING → FIXING） */}
          <path
            d={`M ${X0 + 3 * PITCH + NODE_W / 2} ${Y - NODE_H / 2} C ${X0 + 3 * PITCH + NODE_W / 2} ${Y - 46},
                 ${X0 + 2 * PITCH + NODE_W / 2} ${Y - 46}, ${X0 + 2 * PITCH + NODE_W / 2} ${Y - NODE_H / 2}`}
            fill="none"
            strokeWidth={1.5}
            strokeDasharray="4 3"
            className="stroke-warn"
            markerEnd="url(#fsm-arrow)"
          />
          <text x={X0 + 2.5 * PITCH + NODE_W / 2} y={Y - 50} textAnchor="middle" className="fill-warn text-[10px]">
            失败回炉（限次）
          </text>

          {/* 主链节点 */}
          {MAIN_STAGES.map((item, index) => {
            const x = X0 + index * PITCH
            const state = stateOf(index, item.key)
            const isHover = hover === item.key
            return (
              <g
                key={item.key}
                className={`cursor-pointer ${fill(state)}`}
                onMouseEnter={() => setHover(item.key)}
                onMouseLeave={() => setHover(null)}
                onClick={() => onSelectStage?.(item.key)}
              >
                <rect
                  x={x}
                  y={Y - NODE_H / 2}
                  width={NODE_W}
                  height={NODE_H}
                  rx={8}
                  fill="currentColor"
                  className="opacity-90"
                  stroke="var(--color-line)"
                  strokeWidth={isHover ? 2 : 1}
                />
                <text
                  x={x + NODE_W / 2}
                  y={Y - 2}
                  textAnchor="middle"
                  className="fill-ink text-[11px]"
                  style={{ pointerEvents: 'none' }}
                >
                  {item.label}
                </text>
                <text
                  x={x + NODE_W / 2}
                  y={Y + 11}
                  textAnchor="middle"
                  className="fill-muted text-[8px]"
                  style={{ pointerEvents: 'none' }}
                >
                  {item.key}
                </text>
                {state === 'active' && (
                  <circle cx={x + NODE_W - 8} cy={Y - NODE_H / 2 + 8} r={3} className="fill-accent animate-pulse" />
                )}
              </g>
            )
          })}

          {/* 闸门标记（1/2/3） */}
          {[
            { index: 1, label: '闸门1 置信度', tone: 'fill-warn' },
            { index: 4, label: '闸门2 运维审批', tone: 'fill-danger' },
            { index: 5, label: '闸门3 用户公告', tone: 'fill-info' },
          ].map((gate) => {
            const cx = X0 + gate.index * PITCH + NODE_W / 2
            return (
              <g key={gate.label}>
                <path
                  d={`M ${cx} ${Y + NODE_H / 2 + 6} l 5 4 l -5 4 l -5 -4 z`}
                  className={gate.tone}
                />
                <text x={cx} y={Y + NODE_H / 2 + 28} textAnchor="middle" className={`${gate.tone} text-[9px]`}>
                  {gate.label}
                </text>
              </g>
            )
          })}

          {/* 异常出口 */}
          <g
            className={`cursor-pointer ${current === 'ESCALATED' ? 'text-danger' : 'text-idle'}`}
            onMouseEnter={() => setHover('ESCALATED')}
            onMouseLeave={() => setHover(null)}
            onClick={() => onSelectStage?.('ESCALATED')}
          >
            <rect x={120} y={172} width={104} height={28} rx={8} fill="currentColor" fillOpacity={0.12} stroke="currentColor" strokeWidth={current === 'ESCALATED' ? 2 : 1} />
            <text x={172} y={190} textAnchor="middle" className="fill-ink text-[11px]">转人工 ESCALATED</text>
          </g>
          <g
            className={`cursor-pointer ${current === 'CANCELLED' ? 'text-idle' : 'text-idle'}`}
            onMouseEnter={() => setHover('CANCELLED')}
            onMouseLeave={() => setHover(null)}
            onClick={() => onSelectStage?.('CANCELLED')}
          >
            <rect x={640} y={172} width={104} height={28} rx={8} fill="currentColor" fillOpacity={0.12} stroke="currentColor" strokeWidth={current === 'CANCELLED' ? 2 : 1} />
            <text x={692} y={190} textAnchor="middle" className="fill-ink text-[11px]">已取消 CANCELLED</text>
          </g>

          {/* 指向异常出口的虚线（完整状态机语义） */}
          {[
            { fromIdx: 1, to: [172, 172], label: '闸门1 / 测试回炉耗尽' },
            { fromIdx: 4, to: [172, 172], label: '' },
            { fromIdx: 6, to: [172, 172], label: '劣化回滚' },
          ].map((edge, i) => {
            const sx = X0 + edge.fromIdx * PITCH + NODE_W / 2
            return (
              <path
                key={`edge-${i}`}
                d={`M ${sx} ${Y + NODE_H / 2 + 34} C ${sx} ${Y + 78}, 172 ${Y + 70}, 172 170`}
                fill="none"
                strokeWidth={1}
                strokeDasharray="3 3"
                className="stroke-danger/40"
              />
            )
          })}
          <path
            d={`M ${X0 + 5 * PITCH + NODE_W / 2} ${Y + NODE_H / 2 + 34} C ${X0 + 5 * PITCH + NODE_W / 2} ${Y + 74}, 692 ${Y + 66}, 692 170`}
            fill="none"
            strokeWidth={1}
            strokeDasharray="3 3"
            className="stroke-idle/50"
          />
        </svg>
      </div>

      <div className="mt-2 min-h-[2.5rem] rounded-lg border border-line bg-canvas px-3 py-2 text-xs">
        {hover ? (
          <span>
            <span className="font-mono text-accent">{hover}</span>
            <span className="ml-2 text-muted">{STAGE_HINT[hover] ?? '—'}</span>
          </span>
        ) : (
          <span className="text-idle">
            悬停节点查看说明，点击节点可联动下方面板；虚线为异常出口（转人工 / 取消）。
          </span>
        )}
      </div>
    </div>
  )
}
