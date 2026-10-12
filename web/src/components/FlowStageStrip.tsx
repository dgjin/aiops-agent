/**
 * 行内简要流程图（流程列表点击行展开）：
 *
 * - 主线 9 阶段水位模型：已完成 ✅（绿）/ 当前进行中 ●（强调色，待审批·
 *   公告期沿用 warn 黄）/ 未到达 ○（灰空心）/ 中断点 ✕（转人工红 · 取消灰 ·
 *   影子档「仅建议」）。
 * - 终态流程携带 gate_events（BFF 由 result 透出），据此精确定位中断点与
 *   原因；运行中流程按 stage 线性推进（主线严格顺序，无跳段）。
 * - FAILED / 阶段缺失的流程无事件数据可定位，全部标灰 + 徽标诚实说明，
 *   不臆造断点；图例同时说明「当前进行中为强调色底」。
 */

import { AlertTriangle, Ban, Check, Minus, X } from 'lucide-react'
import { cn } from '../lib/format'
import { STAGE_META } from './StageBadge'
import type { FlowItem } from '../lib/types'

/** 主线阶段（与设计说明书状态机一致；顺序即推进顺序） */
const STAGES = [
  'DETECTED',
  'TRIAGING',
  'FIXING',
  'TESTING',
  'WAIT_APPROVAL',
  'NOTIFYING',
  'CANARY',
  'ROLLING_OUT',
  'DONE',
] as const

type Kind = 'running' | 'done' | 'escalated' | 'cancelled' | 'shadowed' | 'failed' | 'unknown'

/** 判别联合：kind 收窄后各字段可静态区分（running 才有 current） */
type StripState =
  | { kind: 'running'; current: number; stoppedAfter: null; doneCount: number; reason: null }
  | { kind: 'done'; current: null; stoppedAfter: null; doneCount: number; reason: null }
  | { kind: 'escalated' | 'cancelled' | 'shadowed'; current: null; stoppedAfter: number | null; doneCount: number; reason: string | null }
  | { kind: 'failed' | 'unknown'; current: null; stoppedAfter: null; doneCount: number; reason: string | null }

/** 闸门事件前缀 → 停驻阶段（取最后一条匹配 = 实际中断点；事件为时间序） */
const EVENT_STOP: [string, number][] = [
  ['gate1:', 1],
  ['tests:', 3],
  ['mr:', 3],
  ['gate2:', 4],
  ['gate2b:', 4],
  ['gate3:', 5],
  ['canary:', 6],
  ['shadow:', 3],
]

/** 从事件链反推中断点下标（从尾部向前扫描） */
function stopFromEvents(events: string[]): number | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const hit = EVENT_STOP.find(([prefix]) => events[i].startsWith(prefix))
    if (hit) return hit[1]
  }
  return null
}

/** 从事件链反推中断原因（从尾部向前扫描，命中即返回；无匹配给确定性兜底） */
function reasonFromEvents(kind: Kind, events: string[]): string | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const e = events[i]
    if (e.startsWith('canary:degraded')) return '金丝雀劣化并自动回滚，转人工'
    if (e.startsWith('gate2b:timeout')) return '二级审批超时，自动驳回'
    if (e.startsWith('gate2b:')) return '受保护目录二级审批驳回'
    if (e.startsWith('gate2:timeout')) return '审批超时自动驳回'
    if (e.startsWith('gate2:')) return '运维审批驳回'
    if (e.startsWith('tests:failed')) return '沙箱测试回炉重试耗尽，转人工'
    if (e.startsWith('gate1:blocked')) return `闸门1 拦截：${e.slice('gate1:blocked:'.length)}`
    if (e.startsWith('gate3:cancelled')) return '公告期操作人取消发布'
    if (e.startsWith('shadow:')) return '影子档：仅建议不执行'
  }
  if (kind === 'escalated') return '流程中止，转人工处理'
  if (kind === 'cancelled') return '流程被取消'
  if (kind === 'failed') return '执行硬失败或超时，无阶段事件数据可定位'
  if (kind === 'unknown') return '阶段信息缺失，无法定位进度'
  return null
}

/** 纯函数：stage + gate_events → 流程图水位状态（导出供单测/复用） */
export function computeFlowProgress(stage: string | null, events: string[] | undefined): StripState {
  const evs = events ?? []
  const running = (current: number): StripState => ({
    kind: 'running',
    stoppedAfter: null,
    current,
    doneCount: current,
    reason: null,
  })
  if (stage === 'DONE') {
    return { kind: 'done', stoppedAfter: null, current: null, doneCount: STAGES.length, reason: null }
  }
  if (stage === 'ESCALATED' || stage === 'CANCELLED' || stage === 'SHADOWED') {
    const kind: Kind = stage === 'ESCALATED' ? 'escalated' : stage === 'CANCELLED' ? 'cancelled' : 'shadowed'
    const stoppedAfter = stopFromEvents(evs)
    // SHADOWED 语义特殊：测试已通过、建议已产出，画满全部阶段
    const doneCount = stage === 'SHADOWED' ? STAGES.length : stoppedAfter ?? 0
    return { kind, stoppedAfter, current: null, doneCount, reason: reasonFromEvents(kind, evs) }
  }
  const idx = stage ? STAGES.indexOf(stage as (typeof STAGES)[number]) : -1
  if (idx >= 0) return running(idx)
  return { kind: 'failed', stoppedAfter: null, current: null, doneCount: 0, reason: reasonFromEvents('failed', evs) }
}

const CONNECTOR_DONE = 'border-ok/45'
const CONNECTOR_PENDING = 'border-line'

function NodeMark({ state, i }: { state: StripState; i: number }) {
  const base = 'flex size-7 shrink-0 items-center justify-center rounded-full border'
  if (state.kind === 'running') {
    if (i < state.current) {
      return (
        <span className={cn(base, 'border-ok/50 bg-ok/12 text-ok')}>
          <Check className="size-3.5" />
        </span>
      )
    }
    if (i === state.current) {
      // 待审批 / 公告倒计时沿用 StageBadge 的 warn 语义；暂停类阶段不带脉冲
      const tone = STAGE_META[STAGES[i]].tone
      const pulse = tone !== 'danger' && tone !== 'idle'
      return (
        <span
          className={cn(
            base,
            'border-accent/60 bg-accent/15 text-accent',
            tone === 'warn' && 'border-warn/60 bg-warn/15 text-warn',
            pulse && 'animate-pulse',
          )}
        >
          <span className="size-2 rounded-full bg-current" />
        </span>
      )
    }
    return <span className={cn(base, 'border-line bg-elevated/40 text-idle')}>
      <span className="size-1.5 rounded-full bg-current" />
    </span>
  }

  // 终态：完成段 → 绿勾
  if (i < state.doneCount) {
    return (
      <span className={cn(base, 'border-ok/50 bg-ok/12 text-ok')}>
        <Check className="size-3.5" />
      </span>
    )
  }
  // 中断点
  if (i === state.stoppedAfter) {
    if (state.kind === 'escalated') {
      return (
        <span className={cn(base, 'border-danger/60 bg-danger/12 text-danger')}>
          <X className="size-3.5" />
        </span>
      )
    }
    if (state.kind === 'cancelled') {
      return (
        <span className={cn(base, 'border-idle/60 bg-idle/12 text-idle')}>
          <Ban className="size-3.5" />
        </span>
      )
    }
    if (state.kind === 'shadowed') {
      return (
        <span className={cn(base, 'border-idle/60 bg-idle/12 text-idle')}>
          <Minus className="size-3.5" />
        </span>
      )
    }
  }
  // 硬失败（无定位数据）：末节点红警告示意
  if (state.kind === 'failed' && i === STAGES.length - 1) {
    return (
      <span className={cn(base, 'border-danger/60 bg-danger/12 text-danger')}>
        <AlertTriangle className="size-3.5" />
      </span>
    )
  }
  return (
    <span className={cn(base, 'border-line bg-elevated/40 text-idle')}>
      <span className="size-1.5 rounded-full bg-current" />
    </span>
  )
}

function NodeLabel({ state, i }: { state: StripState; i: number }) {
  const done = state.kind === 'running' ? i < state.current : i < state.doneCount
  const active = state.kind === 'running' && i === state.current
  const stopped = i === state.stoppedAfter
  const tone = active ? STAGE_META[STAGES[i]].tone : null
  return (
    <span
      className={cn(
        'whitespace-nowrap text-[11px] leading-4',
        done && 'text-ok',
        active && (tone === 'warn' ? 'animate-pulse font-medium text-warn' : 'animate-pulse font-medium text-accent'),
        stopped && state.kind === 'escalated' && 'font-medium text-danger',
        stopped && state.kind !== 'escalated' && 'text-idle',
        !done && !active && !stopped && 'text-idle',
      )}
    >
      {STAGE_META[STAGES[i]].label}
    </span>
  )
}

export function FlowStageStrip({ item, className }: { item: FlowItem; className?: string }) {
  const state = computeFlowProgress(item.stage, item.gate_events)

  const chipBase = 'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium'
  const chip =
    state.kind === 'running'
      ? cn(chipBase, 'border-accent/35 bg-accent/10 text-accent')
      : state.kind === 'done'
        ? cn(chipBase, 'border-ok/35 bg-ok/10 text-ok')
        : state.kind === 'escalated' || state.kind === 'failed'
          ? cn(chipBase, 'border-danger/35 bg-danger/10 text-danger')
          : cn(chipBase, 'border-idle/35 bg-idle/10 text-idle')
  const chipText =
    state.kind === 'running'
      ? `进行中 · 当前「${item.stage ? STAGE_META[item.stage]?.label ?? item.stage : '未知'}」`
      : state.kind === 'done'
        ? '已完成 · 全链路通过'
        : state.kind === 'escalated'
          ? '已转人工'
          : state.kind === 'cancelled'
            ? '已取消'
            : state.kind === 'shadowed'
              ? '影子档 · 仅建议不执行'
              : state.kind === 'failed'
                ? '执行失败'
                : '阶段未知'

  return (
    <div className={cn('px-1 py-0.5', className)}>
      <div className="flex items-center gap-1.5">
        {STAGES.map((s, i) => {
          const done = state.kind === 'running' ? i < state.current : i < state.doneCount
          return (
            <div key={s} className="flex items-center gap-1.5">
              {i > 0 && <span className={cn('h-0 w-6 border-t-2', done ? CONNECTOR_DONE : CONNECTOR_PENDING)} />}
              <div className="flex w-16 flex-col items-center gap-1.5">
                <NodeMark state={state} i={i} />
                <NodeLabel state={state} i={i} />
              </div>
            </div>
          )
        })}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 pl-1 text-[11px]">
        <span className={chip}>{chipText}</span>
        {state.reason && <span className="text-muted">{state.reason}</span>}
        <span className="ml-auto flex items-center gap-2.5 text-idle">
          <span className="flex items-center gap-1">
            <span className="size-2 rounded-full bg-ok" />
            已完成
          </span>
          <span className="flex items-center gap-1">
            <span className="size-2 rounded-full bg-accent" />
            进行中（待审批/公告期显示为黄色）
          </span>
          <span className="flex items-center gap-1">
            <span className="size-2 rounded-full border border-line bg-elevated" />
            未到达
          </span>
        </span>
      </div>
    </div>
  )
}
