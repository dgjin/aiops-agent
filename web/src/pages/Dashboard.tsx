/** 流程看板：计数卡 + 运行中卡片墙 + 近 7 日终态分布（设计方案 7.2）。 */

import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { cn } from '../lib/format'
import { EmptyState } from '../components/EmptyState'
import { FlowCard } from '../components/FlowCard'
import { MetricBar } from '../components/MetricBar'
import type { OverviewCounts } from '../lib/types'

const STAT_CARDS: Array<{ key: keyof OverviewCounts; label: string; tone: string }> = [
  { key: 'running', label: '运行中', tone: 'text-info' },
  { key: 'wait_approval', label: '待审批', tone: 'text-warn' },
  { key: 'notifying', label: '公告倒计时', tone: 'text-warn' },
  { key: 'today_done', label: '今日完成', tone: 'text-ok' },
  { key: 'today_escalated', label: '今日转人工', tone: 'text-danger' },
]

export function Dashboard() {
  const { data, isError, error } = useQuery({
    queryKey: ['overview'],
    queryFn: api.overview,
    refetchInterval: 5000,
  })

  if (isError) {
    return <EmptyState title="无法加载看板数据" hint={String(error)} />
  }
  if (!data) return <div className="text-sm text-muted">加载中…</div>

  return (
    <div>
      <h1 className="text-lg font-medium">流程看板</h1>
      <p className="mt-1 text-xs text-muted">自动运维全流程实时总览（5 秒自动刷新）</p>

      <div className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-5">
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
          <MetricBar dist={data.terminal_dist_7d} />
        </div>
      </section>
    </div>
  )
}
