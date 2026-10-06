/** 流程看板：计数卡 + 运行中卡片墙 + 近 7 日终态分布 + 运营度量（设计方案 7.2、P1-4）。 */

import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, describeError } from '../lib/api'
import { cn } from '../lib/format'
import { EmptyState } from '../components/EmptyState'
import { FlowCard } from '../components/FlowCard'
import { Donut } from '../components/charts/Donut'
import type { OpsMetricsResp, OverviewCounts } from '../lib/types'

const STAT_CARDS: Array<{ key: keyof OverviewCounts; label: string; tone: string }> = [
  { key: 'running', label: '运行中', tone: 'text-info' },
  { key: 'wait_approval', label: '待审批', tone: 'text-warn' },
  { key: 'notifying', label: '公告倒计时', tone: 'text-warn' },
  { key: 'today_done', label: '今日完成', tone: 'text-ok' },
  { key: 'today_escalated', label: '今日转人工', tone: 'text-danger' },
  { key: 'today_failed', label: '今日失败', tone: 'text-danger' },
]

/** 秒数说成人话（MTTR 展示）。 */
function fmtDuration(seconds: number | null): string {
  if (seconds == null) return '—'
  if (seconds < 60) return `${Math.round(seconds)}s`
  const minutes = seconds / 60
  if (minutes < 60) return `${minutes.toFixed(1)}m`
  return `${(minutes / 60).toFixed(1)}h`
}

function fmtRate(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`
}

function MetricCard({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: ReactNode
  hint: string
  tone: string
}) {
  return (
    <div className="rounded-xl border border-line bg-panel p-4">
      <div className="text-xs text-muted">{label}</div>
      <div className={cn('mt-2 font-mono text-2xl font-semibold tabular-nums', tone)}>{value}</div>
      <div className="mt-1 text-[11px] text-idle">{hint}</div>
    </div>
  )
}

/** 运营度量区块（P1-4）：MTTR / 自动修复率 / 人工干预率 / 闸门拦截率 + 拦截分档。 */
function OpsMetricsSection({ metrics }: { metrics: OpsMetricsResp }) {
  const { mttr } = metrics
  const activeGates = metrics.gate_breakdown.filter((row) => row.count > 0)
  return (
    <section className="mt-7">
      <h2 className="mb-3 text-sm font-medium text-muted">
        运营度量（近 {metrics.period_days} 日 · 共 {metrics.closed_total} 个终态流程）
      </h2>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricCard
          label="MTTR（DONE 均值）"
          value={fmtDuration(mttr.avg_seconds)}
          hint={`P95 ${fmtDuration(mttr.p95_seconds)} · 样本 ${mttr.sample}`}
          tone="text-info"
        />
        <MetricCard
          label="自动修复率"
          value={fmtRate(metrics.auto_fix_rate)}
          hint={`DONE ${metrics.auto_fix_sample}/${metrics.closed_total}`}
          tone="text-ok"
        />
        <MetricCard
          label="人工干预率"
          value={fmtRate(metrics.human_intervention_rate)}
          hint={`${metrics.human_intervention_sample}/${metrics.closed_total} 被人工写操作触及`}
          tone="text-warn"
        />
        <MetricCard
          label="闸门拦截率"
          value={fmtRate(metrics.gate_block_rate)}
          hint={`${metrics.gate_block_sample}/${metrics.closed_total} 被闸门拦下`}
          tone="text-danger"
        />
      </div>

      <div className="mt-3 rounded-xl border border-line bg-panel p-4">
        <div className="mb-2 text-xs text-muted">拦截分档（近 {metrics.period_days} 日）</div>
        {activeGates.length ? (
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-4">
            {activeGates.map((row) => (
              <div key={row.key} className="flex items-center justify-between gap-3 text-xs">
                <span className="text-muted">{row.label}</span>
                <span className="font-mono tabular-nums text-ink">{row.count}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-xs text-idle">窗口内无闸门拦截记录</div>
        )}
        {metrics.escalations && (
          <div className="mt-3 border-t border-line pt-2 text-[11px] text-idle">
            转人工待办：待指派 {metrics.escalations.open} · 已指派 {metrics.escalations.assigned}{' '}
            · 已关闭 {metrics.escalations.closed}（SLA {metrics.escalations.sla_minutes} 分钟超时再升级）
          </div>
        )}
        {metrics.degraded.length > 0 && (
          <div className="mt-2 text-[11px] text-warn">
            数据源降级：{metrics.degraded.join('、')}（对应分量为空窗口口径）
          </div>
        )}
      </div>
    </section>
  )
}

export function Dashboard() {
  const navigate = useNavigate()
  const { data, isError, error } = useQuery({
    queryKey: ['overview'],
    queryFn: api.overview,
    refetchInterval: 5000,
  })
  const metrics = useQuery({
    queryKey: ['ops-metrics'],
    queryFn: () => api.opsMetrics(7),
    refetchInterval: 30000,
  })

  if (isError) {
    return <EmptyState title="无法加载看板数据" hint={describeError(error)} />
  }
  if (!data) return <div className="text-sm text-muted">加载中…</div>

  return (
    <div>
      <h1 className="text-lg font-medium">流程看板</h1>
      <p className="mt-1 text-xs text-muted">自动运维全流程实时总览（5 秒自动刷新）</p>

      <div className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        {STAT_CARDS.map((card) => (
          <div key={card.key} className="rounded-xl border border-line bg-panel p-4">
            <div className="text-xs text-muted">{card.label}</div>
            <div className={cn('mt-2 font-mono text-2xl font-semibold tabular-nums', card.tone)}>
              {data.counts[card.key]}
            </div>
          </div>
        ))}
      </div>

      <section className="mt-7">
        <h2 className="mb-3 text-sm font-medium text-muted">运行中流程</h2>
        {data.running.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {data.running.map((item) => (
              <FlowCard key={item.wf_id} item={item} />
            ))}
          </div>
        ) : (
          <EmptyState title="当前没有运行中的流程" hint="新告警接入后会自动出现在这里" />
        )}
      </section>

      <section className="mt-7">
        <h2 className="mb-3 text-sm font-medium text-muted">近 7 日终态分布</h2>
        <div className="rounded-xl border border-line bg-panel p-4">
          <Donut
            centerLabel="共"
            segments={[
              { key: 'DONE', label: '已完成', value: data.terminal_dist_7d.DONE ?? 0, tone: 'text-ok' },
              { key: 'SHADOWED', label: '已建议（影子档）', value: data.terminal_dist_7d.SHADOWED ?? 0, tone: 'text-idle' },
              { key: 'ESCALATED', label: '已转人工', value: data.terminal_dist_7d.ESCALATED ?? 0, tone: 'text-danger' },
              { key: 'FAILED', label: '执行失败', value: data.terminal_dist_7d.FAILED ?? 0, tone: 'text-warn' },
              { key: 'CANCELLED', label: '已取消', value: data.terminal_dist_7d.CANCELLED ?? 0, tone: 'text-idle' },
            ]}
            onSelect={(key) => navigate(`/flows?stage=${encodeURIComponent(key)}`)}
          />
        </div>
      </section>

      {metrics.data && <OpsMetricsSection metrics={metrics.data} />}
    </div>
  )
}
