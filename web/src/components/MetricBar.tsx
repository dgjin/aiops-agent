import { EmptyState } from './EmptyState'

const SEGMENTS: Array<{ key: 'DONE' | 'ESCALATED' | 'CANCELLED'; label: string; bar: string; dot: string }> = [
  { key: 'DONE', label: '已完成', bar: 'bg-ok', dot: 'bg-ok' },
  { key: 'ESCALATED', label: '已转人工', bar: 'bg-danger', dot: 'bg-danger' },
  { key: 'CANCELLED', label: '已取消', bar: 'bg-idle', dot: 'bg-idle' },
]

/** 终态分布占比条（近 7 日）。 */
export function MetricBar({ dist }: { dist: Record<'DONE' | 'ESCALATED' | 'CANCELLED', number> }) {
  const total = SEGMENTS.reduce((sum, seg) => sum + (dist[seg.key] ?? 0), 0)
  if (!total) {
    return <EmptyState title="近 7 日暂无已完成流程" hint="完成、转人工或取消的流程会在这里汇总分布" />
  }
  return (
    <div>
      <div className="flex h-2 overflow-hidden rounded-full bg-line">
        {SEGMENTS.map(
          (seg) =>
            dist[seg.key] > 0 && (
              <div
                key={seg.key}
                className={seg.bar}
                style={{ width: `${((dist[seg.key] ?? 0) / total) * 100}%` }}
              />
            ),
        )}
      </div>
      <div className="mt-2.5 flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted">
        {SEGMENTS.map((seg) => (
          <span key={seg.key} className="inline-flex items-center gap-1.5">
            <span className={`h-1.5 w-1.5 rounded-full ${seg.dot}`} />
            {seg.label}
            <span className="font-mono text-ink">{dist[seg.key] ?? 0}</span>
          </span>
        ))}
        <span className="ml-auto text-idle">共 {total} 次</span>
      </div>
    </div>
  )
}
